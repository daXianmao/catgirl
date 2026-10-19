"""QQ 适配器：用 OneBot v11 协议把猫娘接到 QQ 上。

前置：需要一个「协议端」在跑，二选一——
  - NapCat（推荐，Docker 一行起）：开放 WebSocket 服务端，默认 ws://127.0.0.1:3001
  - Lagrange.Core：同上，OneBot 11 兼容

本文件是客户端：连上协议端，收消息 → 走 core 的同一套记忆逻辑 → 回消息。
启动：
    python qq.py                 # 读 config.json / 环境变量
    python qq.py --check         # 只打印配置，不连
    python qq.py --debug         # 打印每条事件

记忆隔离策略：
  - 私聊：每人一个记忆库（users/qq<QQ号>/），她记得和这个人说过什么
  - 群聊：每群一个记忆库（users/qqg<群号>/），她记得这个群的氛围
群消息会带上「说话人昵称：」，这样她能分清是谁在说。
"""
import argparse
import asyncio
import json
import os
import re
import time

try:  # websockets >= 13 的新客户端
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # 老版本兜底
    from websockets import connect as ws_connect

import websockets

# websockets 15+ 会自动读 HTTP_PROXY/HTTPS_PROXY，连本机协议端会被代理拦掉，
# 所以显式传 proxy=None；老版本没有这个参数，只能按版本判断
_WS_MAJOR = int(websockets.__version__.split(".")[0])
_SUPPORTS_PROXY_ARG = _WS_MAJOR >= 15

from brain import BrainError
from config import describe, load_config
from core import CatgirlCore
from tts import TTS, TTSError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_TAG = re.compile(r"<state\s[^>]*/?>", re.S)
MD_MARKS = re.compile(r"(\*\*|__|`|^#+\s*)", re.M)


def log(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def forQQ(text):
    """把模型回复整理成 QQ 里看着正常的样子：去状态标签、去 Markdown 记号。"""
    text = STATE_TAG.sub("", text or "")
    text = MD_MARKS.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text, limit=400):
    """按句切分，避免一条消息几千字。单句超长则硬切。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts, buf = [], ""
    for piece in re.split(r"(?<=[。！？!?\n])", text):
        while len(piece) > limit:
            head, piece = piece[:limit], piece[limit:]
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(head)
        if len(buf) + len(piece) > limit:
            parts.append(buf)
            buf = piece
        else:
            buf += piece
    if buf:
        parts.append(buf)
    return [p for p in (p.strip() for p in parts) if p]


def extract_text(message, selfId):
    """从 OneBot 消息段里取出纯文本，并判断有没有 @ 自己。"""
    if isinstance(message, str):  # 少数实现只给字符串
        return message.strip(), f"[CQ:at,qq={selfId}]" in message

    texts, mentioned = [], False
    for seg in message or []:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            texts.append(data.get("text", ""))
        elif kind == "at" and str(data.get("qq")) == str(selfId):
            mentioned = True
        elif kind == "image":
            texts.append("［图片］")
        elif kind == "face":
            continue
    return "".join(texts).strip(), mentioned


def sender_name(event):
    sender = event.get("sender") or {}
    return (
        sender.get("card")
        or sender.get("nickname")
        or str(event.get("user_id", ""))
    ).strip()


class QQSession:
    """一个聊天对象的上下文：独立 core、独立锁、独立发送节流。"""

    def __init__(self, core, key):
        self.core = core
        self.key = key
        self.lock = asyncio.Lock()
        self.lastSent = 0.0
        self.lastUsed = time.time()


class QQBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.wsUrl = cfg["qqWsUrl"]
        self.token = cfg.get("qqToken") or ""
        self.selfId = str(cfg.get("qqSelfId") or "")
        self.privateOn = bool(cfg.get("qqPrivate"))
        self.atOnly = bool(cfg.get("qqAtOnly"))
        self.voiceOn = bool(cfg.get("qqVoice"))
        self.showName = bool(cfg.get("qqNickname"))
        self.rate = float(cfg.get("qqRate") or 1.5)
        self.maxChars = int(cfg.get("qqMaxChars") or 400)

        rawGroups = str(cfg.get("qqGroups") or "").replace("，", ",")
        self.groups = {g.strip() for g in rawGroups.split(",") if g.strip()}

        self.sessions = {}
        self.seen = {}
        self.pending = {}
        self.seq = 0
        self.ws = None
        self.running = True
        self.semaphore = asyncio.Semaphore(4)  # 2G 小机器，别把上游打到限流
        self.stats = {"收到": 0, "回复": 0, "忽略": 0, "失败": 0}

    # ------------------------------------------------------------ 会话与模型
    def session_for(self, kind, chatId, event):
        """私聊按人、群聊按群，各自一个独立记忆库。"""
        if kind == "group":
            userId, sessionId = f"qqg{chatId}", f"g{chatId}"
        else:
            userId, sessionId = f"qq{chatId}", f"p{chatId}"

        existing = self.sessions.get(userId)
        if existing:
            existing.lastUsed = time.time()
            return existing

        # 每个会话一个 SQLite 连接，群多的号会越堆越多，半小时没用就回收
        if len(self.sessions) >= 300:
            idle = [s for s in self.sessions.values() if time.time() - s.lastUsed > 1800]
            for session in sorted(idle, key=lambda s: s.lastUsed)[:20]:
                self.sessions.pop(session.key, None)
                try:
                    session.core.store._conn.close()
                except Exception:
                    pass

        dbPath = os.path.join(self.cfg["dataDir"], "users", userId, "memory.db")
        os.makedirs(os.path.dirname(dbPath), exist_ok=True)
        core = CatgirlCore(self.cfg, dbPath=dbPath, session=sessionId)
        core.bootstrap()
        session = QQSession(core, userId)
        self.sessions[userId] = session
        return session

    async def reply_text(self, session, userText):
        """同步 core 丢进线程池，别堵死事件循环。"""
        async with self.semaphore:
            try:
                text, meta = await asyncio.to_thread(session.core.respond, userText)
            except BrainError as exc:
                self.stats["失败"] += 1
                log(f"模型出错：{exc}")
                return "（脑袋突然卡住了…等下再说好不好）"
            except Exception as exc:  # 兜底，进程不能因为一条消息挂掉
                self.stats["失败"] += 1
                log(f"回复失败：{exc!r}")
                return "（出错了喵…）"

        if meta.get("error"):
            self.stats["失败"] += 1
            log(f"模型返回错误：{meta['error'][:200]}")
            return "（脑袋突然卡住了…等下再说好不好）"

        self.stats["回复"] += 1
        return forQQ(text)

    # ------------------------------------------------------------ 发送
    async def call(self, action, params, timeout=15):
        if self.ws is None:
            raise RuntimeError("还没连上协议端")
        self.seq += 1
        echo = f"cg-{int(time.time() * 1000)}-{self.seq}"
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        payload = {"action": action, "params": params, "echo": echo}
        await self.ws.send(json.dumps(payload, ensure_ascii=False))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending.pop(echo, None)

    async def send(self, kind, chatId, text, session=None):
        """发文本；超长自动分条，并按 qqRate 控制节奏。"""
        chunks = split_text(text, self.maxChars)
        for index, chunk in enumerate(chunks):
            if session:
                gap = time.time() - session.lastSent
                if gap < self.rate:
                    await asyncio.sleep(self.rate - gap)
                session.lastSent = time.time()
            action = "send_group_msg" if kind == "group" else "send_private_msg"
            key = "group_id" if kind == "group" else "user_id"
            result = await self.call(action, {key: chatId, "message": [{"type": "text", "data": {"text": chunk}}]})
            if result and result.get("status") in ("failed", "error"):
                log(f"发送失败：{str(result)[:160]}")
                self.stats["失败"] += 1
                return
            if index < len(chunks) - 1:
                await asyncio.sleep(0.4)

    async def send_voice(self, kind, chatId, text, userId):
        """发语音。需要协议端能读到同一个绝对路径（Docker 里要挂同一卷）。"""
        try:
            engine = TTS(self.cfg, os.path.join(self.cfg["dataDir"], "users", userId, "voice"))
            result = await asyncio.to_thread(engine.synthesize, text)
        except (TTSError, Exception) as exc:
            log(f"语音合成失败，已只发文字：{exc}")
            return

        fileUri = "file:///" + os.path.abspath(result["path"]).replace("\\", "/").lstrip("/")
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"
        sent = await self.call(action, {key: chatId, "message": [{"type": "record", "data": {"file": fileUri}}]})
        if not sent or sent.get("status") in ("failed", "error"):
            log("语音发送失败（协议端可能不支持该格式），文字已发出")

    # ------------------------------------------------------------ 事件处理
    async def on_event(self, event):
        if event.get("post_type") != "message":
            return

        selfId = str(event.get("self_id") or self.selfId or "")
        if selfId:
            self.selfId = selfId

        kind = "group" if event.get("message_type") == "group" else "private"
        uin = str(event.get("user_id") or "")
        if not uin or uin == selfId:
            return

        messageId = event.get("message_id")
        if messageId is not None:
            now = time.time()
            if messageId in self.seen:
                return
            self.seen[messageId] = now
            if len(self.seen) > 500:  # 只留最近，防内存泄漏
                for key, ts in list(self.seen.items()):
                    if now - ts > 600:
                        self.seen.pop(key, None)

        self.stats["收到"] += 1
        text, mentioned = extract_text(event.get("message"), selfId)
        text = text.strip()

        if kind == "group":
            gid = str(event.get("group_id") or "")
            if gid not in self.groups:
                self.stats["忽略"] += 1
                return  # 白名单为空 = 不接任何群，安全默认
            if self.atOnly and not mentioned:
                self.stats["忽略"] += 1
                return
            chatId = gid
        else:
            if not self.privateOn:
                self.stats["忽略"] += 1
                return
            chatId = uin

        if not text:
            self.stats["忽略"] += 1
            return

        session = self.session_for(kind, chatId, event)

        # 简易指令：重置上下文（长期记忆保留）
        if text.strip() in ("/reset", "重置", "新会话"):
            session.core.new_session()
            await self.send(kind, chatId, "（甩了甩尾巴）好，刚才聊的忘掉啦，记忆还留着。", session)
            return

        if self.showName and kind == "group":
            userText = f"{sender_name(event)}：{text}"
        else:
            userText = text

        async with session.lock:
            reply = await self.reply_text(session, userText)
            await self.send(kind, chatId, reply, session)
            if self.voiceOn and len(reply) <= 120:
                await self.send_voice(kind, chatId, reply, session.key)

    # ------------------------------------------------------------ 连接
    async def pump(self, ws):
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if "echo" in data and data["echo"] in self.pending:
                future = self.pending[data["echo"]]
                if not future.done():
                    future.set_result(data)
                continue

            if data.get("post_type") == "message":
                asyncio.create_task(self.guard(self.on_event, data))

    async def guard(self, handler, event):
        try:
            await handler(event)
        except Exception as exc:
            log(f"事件处理异常：{exc!r}")

    def connect_kwargs(self):
        """连本机协议端必须绕开系统代理，否则会 502。"""
        kwargs = {"ping_interval": 30, "ping_timeout": 20, "max_size": 8 * 1024 * 1024}
        if _SUPPORTS_PROXY_ARG:
            kwargs["proxy"] = self.cfg.get("qqProxy") or None
        return kwargs

    def url(self):
        if not self.token:
            return self.wsUrl
        sep = "&" if "?" in self.wsUrl else "?"
        return f"{self.wsUrl}{sep}access_token={self.token}"

    async def run(self):
        delay = 2
        while self.running:
            try:
                log(f"连接协议端 {self.wsUrl}")
                async with ws_connect(self.url(), **self.connect_kwargs()) as ws:
                    self.ws = ws
                    delay = 2
                    # 必须先起接收循环，否则 call() 等应答时没人读 socket，只能等到超时
                    pumpTask = asyncio.create_task(self.pump(ws))
                    try:
                        info = await self.call("get_login_info", {}, timeout=10)
                        if info and info.get("data"):
                            self.selfId = str(info["data"].get("user_id") or self.selfId)
                            log(f"已登录：{info['data'].get('nickname')}（{self.selfId}）")
                        log(
                            f"就绪｜私聊 {'开' if self.privateOn else '关'}｜"
                            f"群白名单 {sorted(self.groups) or '空（不接群）'}｜需@{'是' if self.atOnly else '否'}"
                        )
                        await pumpTask
                    finally:
                        pumpTask.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"连接断开（{type(exc).__name__}: {exc}），{delay}s 后重连")
            finally:
                self.ws = None

            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def main():
    parser = argparse.ArgumentParser(description="把猫娘接到 QQ（OneBot v11）")
    parser.add_argument("--check", action="store_true", help="只打印配置，不连接")
    parser.add_argument("--debug", action="store_true", help="打印统计")
    args = parser.parse_args()

    cfg = load_config()
    info = describe(cfg)
    log(f"模型 {info['provider']} / {info['model']}｜记忆目录 {cfg['dataDir']}｜名字 {cfg['name']}")

    if args.check:
        log(f"QQ 配置：{json.dumps(info['qq'], ensure_ascii=False)}")
        log(f"语音：{info['tts']['provider']}（语音消息 {'开' if cfg.get('qqVoice') else '关'}）")
        return

    if not cfg.get("qqEnabled"):
        log("QQ 未启用：设 CATGIRL_QQ_ENABLED=true 再跑（这条只是提醒，不影响本次启动）")

    bot = QQBot(cfg)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        if args.debug:
            log(f"统计：{bot.stats}")


if __name__ == "__main__":
    main()
