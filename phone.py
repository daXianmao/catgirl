"""手机桥接：把一台安卓真机上的 QQ 接进猫娘。

为什么要有这个：qq.py 走的是 NapCat 这类非官方协议端，在云服务器 IP 上
（数据中心 IP + 非官方客户端特征）会被腾讯天天风控。真机方案里手机的
QQ 是官方客户端、设备是真实手机、网络是家宽，腾讯侧看不到任何异常。

架构：
    安卓手机（官方 QQ 客户端 + 通知监听脚本 deploy/phone_autojs.js）
        │  手机主动外连（手机在 NAT 后面，服务器连不过去）
        ▼
    本模块（WebSocket 服务端，默认 127.0.0.1:8765）
        │  转成和 qq.py 一样的事件
        ▼
    core.respond()（人格、记忆、模型全部复用，一行都不用改）

能力边界（通知能给什么，就只能拿什么）：
  - 文字：能收能发，最稳
  - 图片：通知里只有「［图片］」，拿不到原图 → 识图用不了
  - 语音：通知里只有「［语音］」，也发不出去

协议（JSON，一行一条）：
    手机 → 服务器
      {"type":"hello","token":"...","device":"Pixel 5","qq":"123456"}
      {"type":"message","id":"唯一串","chat":"private:123|group:456",
       "chatName":"群名","sender":"昵称","senderId":"123","text":"正文"}
      {"type":"ping"}
    服务器 → 手机
      {"type":"welcome","name":"铃洛"}
      {"type":"reply","chat":"private:123","chunks":["第一段","第二段"],"id":"...","ts":1.0}
      {"type":"error","msg":"..."}

启动：
    python phone.py             # 读 config.json / 环境变量
    python phone.py --check     # 只打印配置，不起服务
"""
import argparse
import asyncio
import json
import os
import time

import websockets

try:  # websockets >= 13 的新服务端
    from websockets.asyncio.server import serve as wsServe
except ImportError:  # 老版本兜底
    from websockets import serve as wsServe

from config import describe, load_config
from core import CatgirlCore
from qq import forQQ, split_text


def log(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


class PhoneSession:
    """一个聊天对象的上下文：独立 core、独立锁、独立发送节流。"""

    def __init__(self, core, key):
        self.core = core
        self.key = key
        self.lock = asyncio.Lock()
        self.lastSent = 0.0
        self.lastUsed = time.time()


class PhoneBridge:

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.token = str(cfg.get("phoneToken") or "").strip()
        self.privateOn = bool(cfg.get("phonePrivate"))
        # 群消息的通知里没有 @ 信息，只能靠「文本里提到她的名字」判断在叫她
        self.nameTrigger = bool(cfg.get("phoneNameTrigger"))
        self.rate = float(cfg.get("phoneRate") or 1.5)
        self.maxChars = int(cfg.get("phoneMaxChars") or 400)
        self.ownerQq = str(cfg.get("ownerQq") or "").strip()

        rawGroups = str(cfg.get("phoneGroups") or "").replace("，", ",")
        self.groups = {g.strip() for g in rawGroups.split(",") if g.strip()}

        self.sessions = {}
        self.seen = {}
        self.clients = set()
        self.queue = []  # 手机离线时攒下的回复，重连后补发（有上限，防止无限涨）
        self.semaphore = asyncio.Semaphore(2)  # 云服务器小，别把上游打到限流
        self.stats = {"收到": 0, "回复": 0, "忽略": 0, "失败": 0}

    # ------------------------------------------------------------ 会话与模型
    def session_for(self, kind, chatId, senderId):
        """私聊按对方 QQ 建库；群聊按说话人建库，群友换个群还是同一个人。"""
        if kind == "group":
            userId = f"ph{senderId}" if senderId else f"phg{chatId}"
            sessionId = f"g{chatId}"
        else:
            userId = f"ph{chatId}"
            sessionId = f"p{chatId}"

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
        session = PhoneSession(core, userId)
        self.sessions[userId] = session
        return session

    # ------------------------------------------------------------ 消息处理
    def should_answer_group(self, gid, text, senderId):
        """群消息要不要回。白名单空 = 不接任何群；写 * = 接所有群。"""
        if not self.groups:
            return False
        if "*" not in self.groups and gid not in self.groups:
            return False
        if self.nameTrigger:
            return self.name in text or f"@{self.name}" in text
        return True

    async def process(self, data):
        """把手机上报的一条消息走完整条链路，回复推回手机。"""
        chat = str(data.get("chat") or "").strip()
        if ":" not in chat:
            log(f"忽略：chat 格式不对 {chat!r}（应为 private:123 / group:456）")
            return
        kind, chatId = chat.split(":", 1)
        if kind not in ("private", "group") or not chatId:
            log(f"忽略：chat 格式不对 {chat!r}")
            return

        senderId = str(data.get("senderId") or "").strip()
        sender = str(data.get("sender") or data.get("chatName") or "某人").strip()
        text = str(data.get("text") or "").strip()

        # 手机拿不到原图，只能告诉模型「这有条图片」——别让它假装看见了
        if data.get("hasImage"):
            text = (text + "［图片］").strip()
        if not text:
            self.stats["忽略"] += 1
            return

        if kind == "group":
            if not self.should_answer_group(chatId, text, senderId):
                self.stats["忽略"] += 1
                return
        elif not self.privateOn:
            self.stats["忽略"] += 1
            return

        self.stats["收到"] += 1
        session = self.session_for(kind, chatId, senderId)

        # 和 qq.py 保持一致的称呼：群聊带「说话人：」，主人单独标出来
        if kind == "group":
            if self.ownerQq and senderId == self.ownerQq:
                userText = f"主人（{sender}）：{text}"
            else:
                userText = f"{sender}：{text}"
        else:
            userText = f"主人（{sender}）：{text}" if (self.ownerQq and senderId == self.ownerQq) else text

        # 简易指令：重置上下文（长期记忆保留）
        if text in ("/reset", "重置", "新会话"):
            session.core.new_session()
            await self.deliver(chat, ["好，刚才聊的忘掉啦，记忆还留着。"])
            return

        async with self.semaphore:
            async with session.lock:
                try:
                    reply, meta = await asyncio.to_thread(session.core.respond, userText)
                except Exception as exc:
                    self.stats["失败"] += 1
                    log(f"回复失败：{exc!r}")
                    await self.deliver(chat, ["脑袋突然卡住了，等下再说好不好。"])
                    return

        if meta.get("error"):
            self.stats["失败"] += 1
            log(f"模型返回错误：{str(meta['error'])[:200]}")
            await self.deliver(chat, ["脑袋突然卡住了，等下再说好不好。"])
            return

        cleaned = forQQ(reply)
        if not cleaned:
            self.stats["忽略"] += 1
            return

        chunks = split_text(cleaned, self.maxChars)
        self.stats["回复"] += 1
        session.lastSent = time.time()
        await self.deliver(chat, chunks)

    # ------------------------------------------------------------ 下发
    async def deliver(self, chat, chunks):
        """把回复推给手机；手机不在线就先攒着，重连后补发。"""
        payload = {
            "type": "reply",
            "chat": chat,
            "chunks": list(chunks),
            "id": f"{int(time.time() * 1000)}",
            "ts": time.time(),
        }
        if not self.clients:
            if len(self.queue) < 50:
                self.queue.append(payload)
            log(f"手机不在线，回复已攒着（队列 {len(self.queue)}）")
            return

        dead = []
        for ws in self.clients:
            try:
                await ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as exc:
                log(f"推送失败：{exc!r}")
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def flush(self, ws):
        """手机重连后把攒下的回复补发出去。"""
        pending, self.queue = self.queue, []
        for payload in pending:
            try:
                await ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self.queue.append(payload)
                break
        if pending:
            log(f"补发了 {len(pending) - len(self.queue)} 条离线期间的回复")

    # ------------------------------------------------------------ 连接
    async def handle_client(self, ws):
        peer = ws.remote_address
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = json.loads(raw)
        except Exception:
            log(f"握手失败（对端没在 10 秒内发 hello）：{peer}")
            await ws.close(4000, "hello timeout")
            return

        if hello.get("type") != "hello":
            await ws.close(4002, "expect hello")
            return
        if self.token and str(hello.get("token") or "") != self.token:
            log(f"拒绝连接：token 不对（{peer}）")
            await ws.close(4001, "bad token")
            return

        self.clients.add(ws)
        device = hello.get("device") or "未知设备"
        log(f"手机已连接：{device}（{peer}）｜当前在线 {len(self.clients)} 台")
        try:
            await ws.send(json.dumps({"type": "welcome", "name": self.name}, ensure_ascii=False))
            await self.flush(ws)
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                kind = data.get("type")
                if kind == "message":
                    # 去重：手机重连可能补发，同一条消息不能回两遍
                    mid = str(data.get("id") or "")
                    now = time.time()
                    if mid:
                        if mid in self.seen:
                            continue
                        self.seen[mid] = now
                        if len(self.seen) > 500:
                            for key, ts in list(self.seen.items()):
                                if now - ts > 600:
                                    self.seen.pop(key, None)
                    asyncio.create_task(self.guard(self.process, data))
                elif kind == "ping":
                    await ws.send(json.dumps({"type": "pong"}, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            log(f"手机断开（{device}）｜当前在线 {len(self.clients)} 台")

    async def guard(self, handler, data):
        """一条消息出错不能带崩整个进程。"""
        try:
            await handler(data)
        except Exception as exc:
            self.stats["失败"] += 1
            log(f"处理消息出错：{exc!r}")

    async def run(self, host, port):
        async with wsServe(
            self.handle_client,
            host,
            port,
            ping_interval=20,   # 手机在移动网络里容易静默掉线，靠心跳探测
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ):
            log(f"手机桥接就绪，监听 ws://{host}:{port}")
            if not self.token:
                log("警告：没设 CATGIRL_PHONE_TOKEN，任何人连上来都能冒充手机")
            await asyncio.Future()  # 一直跑


def main():
    parser = argparse.ArgumentParser(description="把一台安卓真机上的 QQ 接进猫娘")
    parser.add_argument("--check", action="store_true", help="只打印配置，不起服务")
    args = parser.parse_args()

    cfg = load_config()
    if args.check:
        import json as _json
        info = describe(cfg)
        print("provider :", info["provider"], "/", info["model"])
        print("phone    :", _json.dumps(info["phone"], ensure_ascii=False))
        print("dataDir  :", cfg["dataDir"])
        return

    if not cfg.get("phoneEnabled"):
        log("手机桥接未启用：设 CATGIRL_PHONE_ENABLED=true（这条只是提醒，不影响本次启动）")

    bridge = PhoneBridge(cfg)
    host = str(cfg.get("phoneHost") or "127.0.0.1")
    port = int(cfg.get("phonePort") or 8765)
    log(f"模型 {cfg['provider']} / {cfg['model']}｜记忆目录 {cfg['dataDir']}｜名字 {cfg['name']}")
    try:
        asyncio.run(bridge.run(host, port))
    except KeyboardInterrupt:
        log(f"收到中断，退出｜统计 {bridge.stats}")


if __name__ == "__main__":
    main()
