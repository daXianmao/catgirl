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
  - 群聊：同样按「说话人」建库（users/qq<QQ号>/），与私聊记忆统一到同一个人；
    短期会话仍按群区分（session=g<群号>），避免群里的话污染私聊上下文
群消息会带上「说话人昵称：」，这样她能分清是谁在说。
"""
import argparse
import asyncio
import base64
import json
import os
import re
import time
import urllib.request

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
# 模型偶尔会把「［图片］」「（歪头）」这类括号包着的内容当台词复述出来：
# 方括号是系统占位符，圆括号是动作/心理描写，都不该出现在 QQ 回复里。出口统一清掉。
BRACKETS = re.compile(r"［[^［］]*］|\[[^\[\]]*\]|（[^（）]*）|\([^()]*\)")
MD_MARKS = re.compile(r"(\*\*|__|`|^#+\s*)", re.M)


def log(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def forQQ(text):
    """把模型回复整理成 QQ 里看着正常的样子：去状态标签、去括号内容、去 Markdown 记号。"""
    text = STATE_TAG.sub("", text or "")
    text = BRACKETS.sub("", text)
    text = MD_MARKS.sub("", text)
    text = re.sub(r"[ \t　]{2,}", " ", text)
    text = re.sub(r"\n[ \t　]*\n+", "\n", text)  # 括号删除后可能留下空行
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


# QQ 原生表情（face）ID → 中文名。覆盖日常会用到的部分，其余统一显示「表情」。
# 有了它，猫娘才知道对方是笑了、哭了还是在鄙视她，而不是看到一片空白。
FACE_NAMES = {
    "0": "惊讶", "1": "撇嘴", "2": "色", "3": "发呆", "4": "得意",
    "5": "流泪", "6": "害羞", "7": "闭嘴", "8": "睡", "9": "大哭",
    "10": "尴尬", "11": "发怒", "12": "调皮", "13": "呲牙", "14": "微笑",
    "15": "难过", "16": "酷", "17": "抓狂", "18": "吐", "19": "偷笑",
    "20": "可爱", "21": "白眼", "22": "傲慢", "23": "饥饿", "24": "困",
    "25": "惊恐", "26": "流汗", "27": "憨笑", "28": "大兵", "29": "奋斗",
    "30": "咒骂", "31": "疑问", "32": "嘘", "33": "晕", "34": "折磨",
    "35": "衰", "36": "骷髅", "37": "敲打", "38": "再见", "39": "擦汗",
    "40": "抠鼻", "41": "鼓掌", "42": "糗大了", "43": "坏笑", "44": "左哼哼",
    "45": "右哼哼", "46": "哈欠", "47": "鄙视", "48": "委屈", "49": "快哭了",
    "50": "阴险", "51": "亲亲", "52": "吓", "53": "可怜", "54": "菜刀",
    "55": "西瓜", "56": "啤酒", "57": "篮球", "58": "乒乓", "59": "咖啡",
    "60": "饭", "61": "猪头", "62": "玫瑰", "63": "凋谢", "64": "示爱",
    "65": "爱心", "66": "心碎", "67": "蛋糕", "68": "闪电", "69": "炸弹",
    "70": "刀", "71": "足球", "72": "瓢虫", "73": "便便", "74": "月亮",
    "75": "太阳", "76": "礼物", "77": "拥抱", "78": "强", "79": "弱",
    "80": "握手", "81": "胜利", "82": "抱拳", "83": "勾引", "84": "拳头",
    "85": "差劲", "86": "爱你", "87": "NO", "88": "OK", "89": "爱情",
    "90": "飞吻", "91": "跳跳", "92": "发抖", "93": "怄火", "94": "转圈",
    "95": "磕头", "96": "回头", "97": "跳绳", "98": "挥手", "99": "激动",
    "100": "街舞", "101": "献吻", "102": "左太极", "103": "右太极", "104": "开心",
    "105": "双喜", "106": "鞭炮", "107": "灯笼", "108": "发财", "109": "K歌",
    "110": "购物", "111": "邮件", "112": "帅", "113": "喝彩", "114": "祈祷",
    "115": "爆筋", "116": "棒棒糖", "117": "喝奶", "118": "面条", "119": "香蕉",
    "120": "飞机", "121": "开车", "122": "高铁", "123": "左车头", "124": "右车头",
    "125": "多云", "126": "下雨", "127": "钞票", "128": "熊猫", "129": "灯泡",
    "130": "风车", "131": "闹钟", "132": "打伞", "133": "彩球", "134": "钻戒",
    "135": "沙发", "136": "纸巾", "137": "药", "138": "手枪", "139": "青蛙",
    "140": "无奈", "141": "摸着", "142": "迷惑", "143": "赞",
}


def extract_text(message, selfId):
    """从 OneBot 消息段里取出纯文本、是否 @ 了自己、图片地址、语音标识。

    图片/语音只记占位符和引用，要不要真的「看/听」由上层决定（群里刷图刷语音不该张张都烧额度）。
    """
    if isinstance(message, str):  # 少数实现只给字符串
        return message.strip(), f"[CQ:at,qq={selfId}]" in message, [], []

    texts, mentioned, images, records = [], False, [], []
    for seg in message or []:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            texts.append(data.get("text", ""))
        elif kind == "at" and str(data.get("qq")) == str(selfId):
            mentioned = True
        elif kind == "image":
            url = data.get("url") or ""
            if url:
                images.append(url)
            texts.append("［图片］")
        elif kind == "face":
            name = FACE_NAMES.get(str(data.get("id")))
            texts.append(f"［表情：{name}］" if name else "［表情］")
        elif kind == "mface":
            title = (data.get("summary") or data.get("title") or "").strip("[]")
            texts.append(f"［表情包：{title}］" if title else "［表情包］")
        elif kind in ("record", "voice"):
            records.append(data)
            texts.append("［语音］")
        elif kind == "video":
            texts.append("［视频］")
    return "".join(texts).strip(), mentioned, images, records


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
        self.ownerQq = str(cfg.get("ownerQq") or "").strip()
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
        """私聊、群聊都按「说话人」建库，记忆统一到个人。
        短期会话仍区分私聊/群聊（session 不同），避免群里的话污染私聊上下文。"""
        if kind == "group":
            sender = str(event.get("user_id") or "").strip()
            userId = f"qq{sender}" if sender else f"qqg{chatId}"
            sessionId = f"g{chatId}"
        else:
            userId = f"qq{chatId}"
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
        session = QQSession(core, userId)
        self.sessions[userId] = session
        return session

    # ------------------------------------------------------------ 图片识别
    @staticmethod
    def _fetch_image(url, timeout=15, limit=5 * 1024 * 1024):
        """下载图片；超过 5MB 直接放弃，免得大图把内存和额度一起吃光。"""
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = resp.read(limit + 1)
        return data if len(data) <= limit else b""

    async def read_image(self, session, url):
        """下载图片交给多模态模型，返回一句描述；失败返回空串，不影响主流程。"""
        brain = getattr(session.core, "brain", None)
        if not brain or not getattr(brain, "visionModel", ""):
            return ""
        try:
            raw = await asyncio.to_thread(self._fetch_image, url)
        except Exception as exc:
            log(f"图片下载失败：{exc}")
            return ""
        if not raw:
            return ""
        # 按文件头猜 MIME：模型对 data URI 里声明的类型比较敏感
        mime = "image/jpeg"
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            mime = "image/webp"
        dataUri = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        try:
            return (await asyncio.to_thread(brain.describeImage, dataUri) or "").strip()
        except Exception as exc:
            log(f"图片识别失败：{exc}")
            return ""

    async def read_voice(self, session, data):
        """把一条 QQ 语音转成文字：get_record 拿 wav base64 → qwen3-asr 转写。

        失败返回空串，不影响主流程（退回「［语音］」占位符）。
        """
        brain = getattr(session.core, "brain", None)
        if not brain or not getattr(brain, "asrModel", ""):
            return ""
        ref = data.get("file") or data.get("file_id") or data.get("url") or ""
        if not ref:
            return ""
        try:
            result = await self.call("get_record", {"file": ref, "out_format": "wav"})
        except Exception as exc:
            log(f"取语音失败：{exc}")
            return ""
        payload = (result or {}).get("data") or {}
        b64 = payload.get("base64") or ""
        if not b64:
            # 有的实现只回本地路径，读文件兜底
            path = payload.get("file") or ""
            try:
                if path and os.path.exists(path):
                    with open(path, "rb") as handle:
                        b64 = base64.b64encode(handle.read()).decode("ascii")
            except OSError:
                b64 = ""
        if not b64:
            log("取语音失败：协议端没返回音频数据")
            return ""
        dataUri = "data:audio/wav;base64," + b64
        try:
            return (await asyncio.to_thread(brain.transcribe, dataUri) or "").strip()
        except Exception as exc:
            log(f"语音识别失败：{exc}")
            return ""

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
        """把回复合成语音发出去。优先给协议端本地路径（同一卷），
        失败再退 base64（协议端多半支持 base64://，代价是消息变大）。"""
        try:
            engine = TTS(self.cfg, os.path.join(self.cfg["dataDir"], "users", userId, "voice"))
            result = await asyncio.to_thread(engine.synthesize, text)
        except (TTSError, Exception) as exc:
            log(f"语音合成失败，已只发文字：{exc}")
            return

        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"

        # 协议端能读到的绝对路径（compose 里 catgirl 与 napcat 挂同一个 /data 卷）
        fileUri = "file://" + os.path.abspath(result["path"]).replace("\\", "/")
        sent = await self.call(action, {key: chatId, "message": [{"type": "record", "data": {"file": fileUri}}]})
        if sent and sent.get("status") not in ("failed", "error"):
            return

        # 本地路径这招不行就改 base64，彻底摆脱路径/挂载/权限问题
        try:
            with open(result["path"], "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            log(f"读语音文件失败：{exc}")
            return
        b64Uri = "base64://" + base64.b64encode(raw).decode("ascii")
        sent = await self.call(action, {key: chatId, "message": [{"type": "record", "data": {"file": b64Uri}}]})
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
        text, mentioned, images, records = extract_text(event.get("message"), selfId)
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

        # 图片识别：只处理明显针对她的（私聊，或群里 @ 了她），群里别人刷图不跟着烧额度。
        # 识别失败就退回「［图片］」占位符，不影响正常聊天
        if images:
            limit = int(self.cfg.get("visionMaxImages") or 0)
            if limit > 0 and (kind == "private" or mentioned):
                descs = []
                for url in images[:limit]:
                    desc = await self.read_image(session, url)
                    if desc:
                        descs.append(desc)
                if descs:
                    rest = text.replace("［图片］", "").strip()
                    text = "［图片内容：" + "；".join(descs) + "］" + rest

        # 语音识别：私聊或群里 @ 了她才转写，避免群里随便一条语音都烧额度。
        # 失败就退回「［语音］」占位符，不影响正常聊天
        if records and (kind == "private" or mentioned):
            transcripts = []
            for data in records[:3]:
                transcript = await self.read_voice(session, data)
                if transcript:
                    transcripts.append(transcript)
            if transcripts:
                rest = text.replace("［语音］", "").strip()
                text = "［语音内容：" + "；".join(transcripts) + "］" + rest

        # 简易指令：重置上下文（长期记忆保留）
        if text.strip() in ("/reset", "重置", "新会话"):
            session.core.new_session()
            await self.send(kind, chatId, "（甩了甩尾巴）好，刚才聊的忘掉啦，记忆还留着。", session)
            return

        if self.showName and kind == "group":
            sender = sender_name(event)
            if self.ownerQq and uin == self.ownerQq:
                userText = f"主人（{sender}）：{text}"
            else:
                userText = f"{sender}：{text}"
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
