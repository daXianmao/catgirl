"""QQ 官方机器人适配器（QQ 开放平台 OpenAPI v2）。

为什么要这个：NapCat 这类非官方协议端是逆向实现的，腾讯能识别出「非官方客户端」，
再叠上云服务器 IP，小号就会被天天风控。走官方接口则完全合规：
机器人有自己的 AppID，腾讯知道你是谁、在干什么，不存在「被判成外挂」这回事。

和 qq.py 的分工：
  qq.py    —— 非官方协议端（NapCat / Lagrange），能力全，风控高
  qqbot.py —— 官方 OpenAPI v2，无风控，但受平台限制

官方接口的三条硬限制（设计上必须接受）：
  1. 群里只有被 @ 才会收到消息（GROUP_AT_MESSAGE_CREATE），做不到「自然接话」
  2. 只能「被动回复」：必须在收到消息后 5 分钟内、对同一个 msg_id 最多回 5 条；
     主动消息需要额外权限，且用户可以在客户端关掉「允许主动发送」
  3. 频控：个人认证整体 60 条/分钟、单个会话 20 条/分钟

配置（环境变量，详见 .env.example）：
  CATGIRL_QQBOT_APP_ID      / CATGIRL_QQBOT_SECRET
  CATGIRL_QQBOT_SANDBOX     true = 先跑沙箱环境（机器人未上线时只能这样）
  CATGIRL_OWNER_OPENID      主人的 openid（不是 QQ 号！第一次对话时从日志里抄）

启动：
    python qqbot.py             # 连官方 WebSocket，收消息就走 core
    python qqbot.py --check     # 只打印配置与 token 获取结果，不连
"""
import argparse
import asyncio
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

import websockets

try:  # websockets >= 13 的新客户端
    from websockets.asyncio.client import connect as wsConnect
except ImportError:  # 老版本兜底
    from websockets import connect as wsConnect

_WS_MAJOR = int(websockets.__version__.split(".")[0])
_SUPPORTS_PROXY_ARG = _WS_MAJOR >= 15

from config import describe, load_config
from core import CatgirlCore
from qq import forQQ, split_text

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
SANDBOX_BASE = "https://sandbox.api.sgroup.qq.com"
# 1 << 25 = GROUP_AND_C2C_EVENT：群 @ 消息 + 单聊消息，正好是我们要的两个场景
INTENTS = 1 << 25

# 官方 content 里会残留 @ 标记和表情占位，要清掉才算正常文本
AT_TAG = re.compile(r"<@!?[0-9A-Za-z]+>")
EMOJI_TAG = re.compile(r"<emoji:\d+>")
# 被动回复对一个 msg_id 最多 5 条，超了平台直接拒
MAX_REPLY_CHUNKS = 5


def log(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def clean_content(text):
    text = AT_TAG.sub("", text or "")
    text = EMOJI_TAG.sub("", text)
    return text.strip()


class OfficialSession:
    """一个聊天对象的上下文：独立 core、独立锁、独立发送节流。"""

    def __init__(self, core, key):
        self.core = core
        self.key = key
        self.lock = asyncio.Lock()
        self.lastSent = 0.0
        self.lastUsed = time.time()


class OfficialBot:

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.appId = str(cfg.get("qqbotAppId") or "").strip()
        self.secret = str(cfg.get("qqbotSecret") or "").strip()
        self.sandbox = bool(cfg.get("qqbotSandbox"))
        self.ownerOpenid = str(cfg.get("qqbotOwnerOpenid") or "").strip()
        self.rate = float(cfg.get("qqbotRate") or 1.2)
        self.maxChars = int(cfg.get("qqbotMaxChars") or 400)
        self.visionLimit = int(cfg.get("visionMaxImages") or 0)

        self.sessions = {}
        self.seen = {}
        self.seq = {}
        self.semaphore = asyncio.Semaphore(4)
        self.stats = {"收到": 0, "回复": 0, "忽略": 0, "失败": 0}

        self.ws = None
        self.running = True
        self._token = ""
        self._tokenExp = 0.0
        self._seq = 0  # WebSocket 心跳用的 s

    # ------------------------------------------------------------ HTTP
    @staticmethod
    def _request(method, url, body=None, headers=None, timeout=20):
        """同步 HTTP，调用方丢线程池。返回 (状态码, 文本)。"""
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # 平台把业务错误放在响应体里，状态码本身信息量不大
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:
            return 0, f"{type(exc).__name__}: {exc}"

    @property
    def base(self):
        return SANDBOX_BASE if self.sandbox else API_BASE

    async def access_token(self):
        """AppID + AppSecret 换 access_token，有效期 7200 秒，提前 60 秒续期。"""
        if self._token and time.time() < self._tokenExp - 60:
            return self._token
        if not self.appId or not self.secret:
            raise RuntimeError("没配 CATGIRL_QQBOT_APP_ID / CATGIRL_QQBOT_SECRET")
        status, raw = await asyncio.to_thread(
            self._request, "POST", TOKEN_URL,
            {"appId": self.appId, "clientSecret": self.secret},
        )
        try:
            data = json.loads(raw)
        except Exception:
            raise RuntimeError(f"取 token 返回的不是 JSON（{status}）：{raw[:200]}")
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError(f"取 token 失败（{status}）：{raw[:200]}")
        self._token = token
        self._tokenExp = time.time() + int(data.get("expires_in") or 7200)
        log(f"access_token 已更新，{int(self._tokenExp - time.time())} 秒后过期")
        return token

    async def api(self, method, path, body=None):
        token = await self.access_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.appId,
        }
        return await asyncio.to_thread(
            self._request, method, f"{self.base}{path}", body, headers
        )

    async def gateway(self):
        status, raw = await self.api("GET", "/gateway")
        try:
            url = json.loads(raw).get("url") or ""
        except Exception:
            url = ""
        if not url:
            raise RuntimeError(f"取网关失败（{status}）：{raw[:200]}")
        return url

    # ------------------------------------------------------------ 会话
    def session_for(self, kind, targetId, memberId):
        """私聊按 user_openid 建库；群聊按「群 + 成员」建库。

        注意：官方给的 openid 是分组隔离的——同一个人在 A 群和 B 群拿到的是
        不同 openid，所以群里的记忆没法跨群合并到同一个人。这是平台限制。
        """
        if kind == "group":
            userId = f"of{targetId[:16]}_{memberId[:16]}"
            sessionId = f"g{targetId[:16]}"
        else:
            userId = f"of{targetId[:16]}"
            sessionId = f"p{targetId[:16]}"

        existing = self.sessions.get(userId)
        if existing:
            existing.lastUsed = time.time()
            return existing

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
        session = OfficialSession(core, userId)
        self.sessions[userId] = session
        return session

    # ------------------------------------------------------------ 图片
    @staticmethod
    def fetch_data_uri(url, timeout=15, limit=5 * 1024 * 1024):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(limit + 1)
            if len(raw) > limit:
                return ""
            ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0]
        return f"data:{ctype};base64," + base64.b64encode(raw).decode("ascii")

    async def describe_images(self, session, attachments):
        """官方消息的图片是 attachments 里的 URL，可以下载下来喂视觉模型。
        这一点比真机方案强：真机只能拿到「［图片］」四个字。"""
        if self.visionLimit <= 0 or not attachments:
            return ""
        urls = []
        for item in attachments:
            ctype = str(item.get("content_type") or "")
            if ctype.startswith("image/"):
                url = item.get("url") or ""
                if url:
                    urls.append(url)
        descs = []
        for url in urls[: self.visionLimit]:
            try:
                dataUri = await asyncio.to_thread(self.fetch_data_uri, url)
                if not dataUri:
                    continue
                desc = await asyncio.to_thread(session.core.brain.describeImage, dataUri)
                if desc:
                    descs.append(desc.strip())
            except Exception as exc:
                log(f"图片识别失败：{exc}")
        return "；".join(descs)

    # ------------------------------------------------------------ 发送
    def begin_reply(self, msgId):
        """同一个 msg_id 累计最多 5 条被动回复，用掉几条就只剩几条可用。

        不能只按本次分条数算——同一条消息可能先回过一次（比如 /reset），
        再按 1..5 发就会跟前面的 msg_seq 撞号，平台直接拒。
        """
        used = self.seq.get(msgId, 0)
        if len(self.seq) > 300:
            for key in list(self.seq.keys())[:100]:
                self.seq.pop(key, None)
        return max(0, MAX_REPLY_CHUNKS - used)

    def take_seq(self, msgId):
        current = self.seq.get(msgId, 0) + 1
        self.seq[msgId] = current
        return current

    async def send(self, kind, targetId, text, msgId, session=None):
        """被动回复。超长自动分条，但同一条消息累计最多 5 条（平台限制）。"""
        left = self.begin_reply(msgId)
        if left <= 0:
            log(f"{msgId} 已经回过 {MAX_REPLY_CHUNKS} 条，放弃本次发送")
            return

        chunks = split_text(text, self.maxChars)
        if not chunks:
            return
        if len(chunks) > left:
            chunks = chunks[:left]
            chunks[-1] = chunks[-1] + "…"
        path = (
            f"/v2/groups/{targetId}/messages"
            if kind == "group"
            else f"/v2/users/{targetId}/messages"
        )
        for index, chunk in enumerate(chunks):
            if session:
                gap = time.time() - session.lastSent
                if gap < self.rate:
                    await asyncio.sleep(self.rate - gap)
                session.lastSent = time.time()
            body = {
                "content": chunk,
                "msg_type": 0,
                "msg_id": msgId,
                "msg_seq": self.take_seq(msgId),
            }
            status, raw = await self.api("POST", path, body)
            if status >= 300:
                self.stats["失败"] += 1
                log(f"发送失败（{status}）：{raw[:200]}")
                # 22009 = 触发频控，再发也是白费
                if "22009" in raw:
                    await asyncio.sleep(3)
                return
            if index < len(chunks) - 1:
                await asyncio.sleep(0.6)

    async def reply_text(self, session, userText):
        async with self.semaphore:
            try:
                text, meta = await asyncio.to_thread(session.core.respond, userText)
            except Exception as exc:
                self.stats["失败"] += 1
                log(f"模型出错：{exc!r}")
                return "", {}
        if meta.get("error"):
            self.stats["失败"] += 1
            log(f"模型返回错误：{str(meta['error'])[:200]}")
            return "", meta
        self.stats["回复"] += 1
        return forQQ(text), meta

    # ------------------------------------------------------------ 收消息
    async def on_message(self, kind, data):
        """kind = group（被 @ 的群消息）或 private（单聊）。"""
        msgId = str(data.get("id") or "")
        if not msgId:
            return

        # 去重：断线重连后平台可能补推同一批事件
        now = time.time()
        if msgId in self.seen:
            return
        self.seen[msgId] = now
        if len(self.seen) > 500:
            for key, ts in list(self.seen.items()):
                if now - ts > 600:
                    self.seen.pop(key, None)

        author = data.get("author") or {}
        if kind == "group":
            targetId = str(data.get("group_openid") or "")
            memberId = str(author.get("member_openid") or author.get("id") or "")
        else:
            targetId = str(author.get("user_openid") or author.get("id") or "")
            memberId = targetId
        if not targetId:
            self.stats["忽略"] += 1
            return

        text = clean_content(data.get("content"))
        attachments = data.get("attachments") or []
        hasVoice = any(
            "audio" in str(item.get("content_type") or "")
            or str(item.get("filename") or "").endswith((".silk", ".amr"))
            for item in attachments
        )
        if hasVoice:
            text = (text + "［语音］").strip()
        if not text and not attachments:
            self.stats["忽略"] += 1
            return

        self.stats["收到"] += 1
        session = self.session_for(kind, targetId, memberId)

        # openid 是分组隔离的随机串，看不出是谁。第一次对话时打出来，
        # 主人抄进 CATGIRL_OWNER_OPENID 之后就能被认成「主人」了
        if not self.ownerOpenid:
            log(f"发现一个 openid：{memberId}（如果是主人自己，抄进 CATGIRL_OWNER_OPENID）")
        isOwner = bool(self.ownerOpenid) and memberId == self.ownerOpenid

        # 图片交给视觉模型看一眼，失败就留占位符
        if attachments:
            desc = await self.describe_images(session, attachments)
            rest = text.replace("［图片］", "").strip()
            text = ("［图片内容：" + desc + "］" + rest) if desc else (text or "［图片］")
        if not text:
            self.stats["忽略"] += 1
            return

        if isOwner:
            userText = f"主人：{text}"
        elif kind == "group":
            userText = f"{author.get('nickname') or '群友'}：{text}"
        else:
            userText = text

        if text.strip() in ("/reset", "重置", "新会话"):
            session.core.new_session()
            await self.send(kind, targetId, "好，刚才聊的忘掉啦，记忆还留着。", msgId, session)
            return

        async with session.lock:
            reply, meta = await self.reply_text(session, userText)
            if not reply:
                return  # 出错时不打扰对方，日志里已经记了
            await self.send(kind, targetId, reply, msgId, session)

    # ------------------------------------------------------------ WebSocket
    def connect_kwargs(self):
        kwargs = {"ping_interval": 30, "ping_timeout": 25, "max_size": 8 * 1024 * 1024}
        if _SUPPORTS_PROXY_ARG:
            kwargs["proxy"] = None  # 官方接口默认直连，别被服务器上的代理环境变量带跑
        return kwargs

    async def send_json(self, ws, payload):
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def heartbeat(self, ws, interval):
        """定期带上最后一个 s，平台用语 s 判重，不带会收到要求重连。"""
        while self.running:
            await asyncio.sleep(interval / 1000.0)
            try:
                await self.send_json(ws, {"op": 1, "d": self._seq or None})
            except Exception:
                return

    async def pump(self, ws):
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            op = payload.get("op")
            if payload.get("s"):
                self._seq = payload["s"]

            if op == 10:  # Hello：给心跳间隔
                interval = int((payload.get("d") or {}).get("heartbeat_interval") or 30000)
                log(f"已连接，心跳 {interval}ms，开始鉴权")
                token = await self.access_token()
                await self.send_json(ws, {
                    "op": 2,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": INTENTS,
                        "shard": [0, 1],
                        "properties": {},
                    },
                })
                asyncio.create_task(self.heartbeat(ws, interval))
            elif op == 0:  # 事件推送
                name = payload.get("t") or ""
                data = payload.get("d") or {}
                if name == "READY":
                    user = (data.get("user") or {})
                    log(f"鉴权成功：{user.get('username') or self.name}（{user.get('id') or '?'}）")
                elif name == "GROUP_AT_MESSAGE_CREATE":
                    asyncio.create_task(self.guard(self.on_message, "group", data))
                elif name == "C2C_MESSAGE_CREATE":
                    asyncio.create_task(self.guard(self.on_message, "private", data))
            elif op == 11:  # 心跳回执
                pass
            elif op == 7:  # 要求重连
                log("平台要求重连")
                await ws.close()
            elif op == 9:  # 鉴权失败（token 错 / intents 没权限）
                log(f"鉴权失败：{payload.get('d')}，检查 AppID/Secret 和 IP 白名单")
                await ws.close()

    async def guard(self, handler, *args):
        try:
            await handler(*args)
        except Exception as exc:
            self.stats["失败"] += 1
            log(f"事件处理异常：{exc!r}")

    async def run(self):
        delay = 3
        while self.running:
            try:
                url = await self.gateway()
                async with wsConnect(url, **self.connect_kwargs()) as ws:
                    self.ws = ws
                    delay = 3
                    log(f"已连上官方网关（{'沙箱' if self.sandbox else '正式'}）")
                    await self.pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"连接断开（{type(exc).__name__}: {exc}），{delay}s 后重连")
            finally:
                self.ws = None

            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)  # 别在鉴权失败时疯狂重试，会被平台拉黑


def explain_failure(text):
    """把平台的错误串翻成人话。这几条是实测和社区里出现频率最高的。"""
    low = (text or "").lower()
    if "白名单" in (text or "") or "whitelist" in low:
        return "→ IP 白名单：开放平台「开发设置 → 服务器 IP 白名单」加上这台服务器的公网 IP"
    if "火星" in (text or ""):
        return "→ 「机器人去火星了」八成也是 IP 白名单没填对，先查这一项"
    if "appid" in low or "app id" in low or "100016" in (text or "") or "invalid app" in low:
        return "→ AppID 或 AppSecret 填错了；Secret 只在创建时显示一次，填错只能重置"
    if "401" in (text or "") or "unauthorized" in low:
        return "→ 凭证无效：Secret 可能少复制了字符，或者这个机器人已经被删除"
    return ""


async def check_bot(cfg):
    """只做连通性自检，不连网关。逐层验证，坏在哪一层就说哪一层。"""
    bot = OfficialBot(cfg)

    # 第一层：AppID + Secret 能不能换到 token
    try:
        token = await bot.access_token()
    except Exception as exc:
        log(f"取 token 失败：{exc}")
        hint = explain_failure(str(exc))
        if hint:
            log(hint)
        return
    log(f"取 token 成功（{token[:8]}…）")

    # 第二层：调一次 OpenAPI，这一层才会真正校验 IP 白名单
    status, raw = await bot.api("GET", "/users/@me")
    if status != 200:
        log(f"调用 OpenAPI 失败（{status}）：{raw[:200]}")
        hint = explain_failure(raw)
        if hint:
            log(hint)
        return
    try:
        info = json.loads(raw)
    except Exception:
        info = {}
    log(f"身份确认：{info.get('username') or info.get('nick') or '?'}"
        f"（机器人 id {info.get('id') or '?'}）")

    # 第三层：网关地址能不能取到，决定 WebSocket 连不连得上
    try:
        url = await bot.gateway()
    except Exception as exc:
        log(f"取网关失败：{exc}")
        return
    log(f"网关地址：{url}")

    log("自检通过。如果连上却收不到消息，按顺序查这三条：")
    log("  1) 开发设置 → 事件订阅与回调地址，必须是 WebSocket（配了 Webhook 回调地址，WS 就废了）")
    log("  2) 机器人要先被加为好友 / 拉进群；群里必须 @ 它才会有事件")
    log("  3) 个人主体：只能拉进自己是群主或管理员的群；给他人用要在「内部体验号码」里加")


def main():

    parser = argparse.ArgumentParser(description="用官方接口把猫娘接到 QQ")
    parser.add_argument("--check", action="store_true", help="只检查配置与 token，不连网关")
    args = parser.parse_args()

    cfg = load_config()
    info = describe(cfg)
    log(f"模型 {info['provider']} / {info['model']}｜记忆目录 {cfg['dataDir']}｜名字 {cfg['name']}")

    if args.check:
        log(f"官方机器人：AppID {cfg.get('qqbotAppId') or '（未配）'}｜"
            f"环境 {'沙箱' if cfg.get('qqbotSandbox') else '正式'}")
        if not (cfg.get("qqbotAppId") and cfg.get("qqbotSecret")):
            log("还没配 AppID/Secret：去 q.qq.com 创建机器人后填进 .env")
            return
        asyncio.run(check_bot(cfg))
        return

    if not cfg.get("qqbotEnabled"):
        log("官方机器人未启用：设 CATGIRL_QQBOT_ENABLED=true（这条只是提醒，不影响本次启动）")

    bot = OfficialBot(cfg)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log(f"收到中断，退出｜统计 {bot.stats}")


if __name__ == "__main__":
    main()
