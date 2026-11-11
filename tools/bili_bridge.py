#!/usr/bin/env python3
"""B 站取稿桥：把 dsh-bili-humanism 的 CLI 包成一个小 HTTP 接口。

为什么需要这层：
  插件是 Node 程序，跑在宿主机上；猫娘是 Docker 容器，镜像里没有 node。
  与其把 node 塞进镜像，不如在宿主开个只读小门，容器通过
  host.docker.internal 调它——镜像保持干净，插件也能单独升级。

接口只有一个：
  POST /read   {"bv": "BV1xxxxxxxxx"}  ->  {"ok": true, "title": ..., "author": ..., "text": ...}
  GET  /health                         ->  {"ok": true, "credential": true/false}

安全：默认只监听 0.0.0.0（容器要能访问），用 token 挡住外人；
      只接受 BV 号，参数不拼进 shell（走 argv），超时 + 单并发。

用法：
  python3 bili_bridge.py [--port 8791] [--token xxx]
  systemd 单元见 deploy/bili-bridge.service
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 配置
NODE_BIN = os.environ.get("BILI_NODE", "/home/ubuntu/opt/node24/bin/node")
PLUGIN_DIR = os.environ.get("BILI_PLUGIN_DIR", "/home/ubuntu/bili-humanism")
DATA_DIR = os.environ.get("BILI_DATA_DIR", "/home/ubuntu/bili-humanism-data")
CRED_FILE = os.environ.get("BILI_CRED_FILE", os.path.join(DATA_DIR, "credential.json"))
TOKEN = os.environ.get("BILI_BRIDGE_TOKEN", "")
READ_TIMEOUT = int(os.environ.get("BILI_READ_TIMEOUT", "180"))
MAX_TEXT = int(os.environ.get("BILI_MAX_TEXT", "8000"))
# 两次取稿之间至少隔这么久。请求打太密，B 站会把字幕降级成别的视频的自动字幕。
MIN_INTERVAL = float(os.environ.get("BILI_MIN_INTERVAL", "6"))

# BV 号固定 12 位（BV + 10 位），只认这个形态，别的一律拒
BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

# node 一次只跑一个：4G 内存的机器经不起并发解码
_readLock = threading.Lock()
_lastReadAt = 0.0


def _throttle():
    """按最小间隔限速，调用方必须已经持有 _readLock。"""
    global _lastReadAt
    wait = MIN_INTERVAL - (time.time() - _lastReadAt)
    if wait > 0:
        time.sleep(wait)
    _lastReadAt = time.time()


def has_credential():
    """凭据文件存在且 sessdata 非空，才认为登录态可用。"""
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return bool(str(data.get("sessdata") or "").strip()) and data.get("disabled") is not True
    except (OSError, ValueError):
        return False


def run_read(bv):
    """调插件 CLI 取一份稿，返回精简后的字典。"""
    if not BV_RE.match(bv):
        return {"ok": False, "error": "BV 号格式不对"}

    env = dict(os.environ)
    env["BILI_HUMANISM_HOME"] = DATA_DIR
    # 凭据文件不存在时绝不能传这个变量：插件会判定 CREDENTIAL_INVALID
    # 并把整个服务持久暂停，之后要人工 resume 才恢复。
    if os.path.exists(CRED_FILE):
        env["BILI_HUMANISM_CREDENTIAL_FILE"] = CRED_FILE
    else:
        env.pop("BILI_HUMANISM_CREDENTIAL_FILE", None)

    args = [NODE_BIN, "bin/cli.mjs", "read", bv]
    started = time.time()
    with _readLock:
        _throttle()
        try:
            proc = subprocess.run(
                args, cwd=PLUGIN_DIR, env=env, timeout=READ_TIMEOUT,
                capture_output=True, text=True, errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"取稿超时（{READ_TIMEOUT}s）"}
        except OSError as exc:
            return {"ok": False, "error": f"起不来 node：{exc}"}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        return {"ok": False, "error": f"CLI 退出码 {proc.returncode}：{detail}"}

    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return {"ok": False, "error": "CLI 输出不是 JSON：" + (proc.stdout or "")[:200]}

    result = payload.get("result") or {}
    segments = result.get("segments") or []
    # segments 里每条形如 {start, end, text}，拼成带时间轴的正文
    lines = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        mark = seg.get("start")
        if isinstance(mark, (int, float)):
            lines.append(f"[{int(mark) // 60:02d}:{int(mark) % 60:02d}] {text}")
        else:
            lines.append(text)
    body = "\n".join(lines)

    coverage = result.get("coverage") or {}
    return {
        "ok": True,
        "bvid": result.get("bvid") or bv,
        "title": result.get("title") or "",
        "author": result.get("author") or "",
        "duration": result.get("duration") or 0,
        "url": result.get("url") or f"https://www.bilibili.com/video/{bv}",
        "text": body[:MAX_TEXT],
        "chars": len(body),
        "route": result.get("route") or "none",
        "coverage": coverage.get("state") or "",
        "attempts": result.get("attempts") or [],
        # 非空表示只拿到平台自动字幕，内容可能与视频对不上，上层要降权
        "degraded": result.get("degraded") or "",
        "elapsed": round(time.time() - started, 1),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "bili-bridge/1.0"

    def log_message(self, fmt, *args):  # 别把 access log 刷进 journal
        pass

    def _reply(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        if not TOKEN:
            return True
        return self.headers.get("X-Bridge-Token", "") == TOKEN

    def do_GET(self):
        if self.path.startswith("/health"):
            self._reply(200, {"ok": True, "credential": has_credential(),
                              "plugin": os.path.isdir(PLUGIN_DIR)})
            return
        self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._reply(403, {"ok": False, "error": "token 不对"})
            return
        if not self.path.startswith("/read"):
            self._reply(404, {"ok": False, "error": "not found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._reply(400, {"ok": False, "error": "请求体不是 JSON"})
            return

        bv = str(body.get("bv") or body.get("url") or "").strip()
        match = re.search(r"(BV[0-9A-Za-z]{10})", bv)
        if not match:
            self._reply(400, {"ok": False, "error": "没找到 BV 号"})
            return

        outcome = run_read(match.group(1))
        self._reply(200 if outcome.get("ok") else 502, outcome)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("BILI_BRIDGE_PORT", "8791")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--token", default=None)
    opts = parser.parse_args()

    global TOKEN
    if opts.token is not None:
        TOKEN = opts.token

    if not os.path.exists(NODE_BIN):
        print(f"找不到 node：{NODE_BIN}", file=sys.stderr)
        return 1
    if not os.path.isdir(PLUGIN_DIR):
        print(f"找不到插件目录：{PLUGIN_DIR}", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((opts.host, opts.port), Handler)
    print(f"bili-bridge 监听 {opts.host}:{opts.port}｜凭据 {'已配置' if has_credential() else '未配置'}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
