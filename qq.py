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
import random
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
# 兜底用：整段只有动作描写时，把括号拆掉但保留里面的字，别让回复变成空字符串
BRACKETS_INNER = re.compile(r"[［\[（(]([^［］\[\]（）()]*)[］\]）)]")
# 台词引号：她会把每句话都拿引号圈起来，发到 QQ 像在念剧本
QUOTE_MARKS = str.maketrans("", "", "“”‘’「」『』")
MD_MARKS = re.compile(r"(\*\*|__|`|^#+\s*)", re.M)

# ------------------------------------------------------------ 表情包
# 模型在回复里写 <emoji name="baka"/> 就附带发一张图。名字必须在表里，
# 编一个不存在的名字不会发任何东西，避免静默出错。
EMOJI_TAG = re.compile(r"<emoji\s+name=[\"']?([a-z_]+)[\"']?\s*/?>", re.I)
EMOJI_DIR = os.path.join(BASE_DIR, "assets", "emoji")
EMOJI_FILES = {
    "kuku": "01_kuku.jpg",           # 库库库……（得意偷笑）
    "baka": "02_baka.jpg",           # 笨蛋！变态！
    "godplay": "03_godplay.jpg",     # 这是神之操作
    "curse_you": "04_curse_you.jpg", # 诅咒你喔！
    "disgust": "05_disgust.jpg",     # 恶心。
    "eat": "06_eat.jpg",             # 趁热吃吧。
    "surprise": "07_surprise.jpg",   # 诶！？！？
    "asexpected": "08_asexpected.jpg",  # 不愧是我。
    "destiny": "09_destiny.jpg",     # 命运的指引。
    "shoot": "10_shoot.jpg",         # 正在取材……
    "explode": "11_explode.jpg",     # 现充都爆炸吧！
    "accompany": "12_accompany.jpg",  # 那个…能陪我吗
    "elegant": "13_elegant.jpg",     # 优雅。
    "stayaway": "14_stayaway.jpg",   # 别过来。
    "ha": "15_ha.jpg",               # 哈——！
    "curse": "16_curse.jpg",         # 这是诅咒哦。
}
EMOJI_COOLDOWN = 40  # 同一会话两张表情之间至少隔这么久，别刷屏


# ------------------------------------------------------------ 语音触发词
# 语音不是每句话都发，只在「有必要」的时候发。三种情况：
#   1. 对方明确要：发语音 / 念出来 / 说来听听
#   2. 对方自己发语音：语音对语音
#   3. 特别开心：reply 的 mood 是开心/得意（只在私聊，群里不主动发）
# 另外认「别发语音」这类喊停，命中后该会话静默 30 分钟。
VOICE_REQUEST = re.compile(
    r"语音|念(?:出来|一下|给我|听|读)|说来听听|读出来|发个音|用声音|voice",
    re.I,
)
VOICE_MUTE = re.compile(r"(?:别|不要|不用|少|停止|关闭).{0,4}语音|关掉语音|别念了|静音", re.I)
HAPPY_MOODS = ("开心", "得意")
# 特别开心时只念短句；对方点名要语音时可以念长一点
VOICE_HAPPY_LIMIT = 120
VOICE_REQUEST_LIMIT = 300
VOICE_MUTE_SECONDS = 1800
# base64 兜底只处理不大的语音；再大就只走文件路径，免得一条消息几百 MB
VOICE_B64_LIMIT = 6 * 1024 * 1024
# 协议端自己所在主机的别名：只有这些说明它和我们在同一台机器上，file:// 才读得到
LOCAL_PROTOCOL_HOSTS = ("127.0.0.1", "localhost", "::1", "napcat", "catgirl-napcat")



def log(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def tidy_punctuation(text):
    """收拾模型爱用的那堆符号。

    "……知道了""——真的吗""！！！"这种挂符号的写法是模板腔的典型特征，
    真人打字不这样。提示词已经禁过一遍，这里再拦一道，保证出口干净。
    """
    # 省略号：整段删掉（"我……不知道"→"我不知道"）
    text = re.sub(r"\.{3,}|…+", "", text)
    # 破折号拖长音：换成逗号
    text = re.sub(r"[—–]{2,}|--{1,}", "，", text)
    # 连续感叹/疑问/波浪号：压成一个
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"！{2,}", "！", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r"？{2,}", "？", text)
    text = re.sub(r"~{2,}", "~", text)
    text = re.sub(r"～{2,}", "～", text)
    # 重复句号
    text = re.sub(r"。{2,}", "。", text)
    # 中文标点前不留空格
    text = re.sub(r"[ \t　]+([，。！？；：、])", r"\1", text)
    # 标点叠用：逗号紧挨着句号问号感叹号时，逗号多余（"真的吗，？"→"真的吗？"）
    text = re.sub(r"，(?=[。！？!?])", "", text)
    text = re.sub(r"(?<=[。！？!?])，", "", text)
    # 结尾标点最多留一个
    text = re.sub(r"([，。！？])\1+$", r"\1", text.strip())
    # 剥掉状态标签后常在末尾留下一个孤零零的逗号顿号，直接去掉
    text = re.sub(r"[，、；：,;:]+\s*$", "", text.strip())
    # 句号后面的空格去掉（中文里不用空格）
    text = re.sub(r"。\s+", "。", text)
    return text.strip()


def _tidy_lines(text):
    """按行收一遍：去掉只有标点的空壳行，合并多余空格。"""
    lines = []
    for line in text.split("\n"):
        line = line.strip(" \t　")
        # 括号/省略号被清掉后，行首常剩下孤零零的标点，直接剃掉
        line = re.sub(r"^[，、；：,;:—–…\.]+", "", line).strip(" \t　")
        if line:
            lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"[ \t　]{2,}", " ", text).strip()


def forQQ(text):
    """把模型回复整理成 QQ 里看着正常的样子：去状态标签、去括号内容、去 Markdown 记号、清符号。

    模型爱写「（动作描写）\\n台词」这种剧本格式。括号删掉后每行都会剩一个空壳，
    还带着行首的「，」「——」这类孤儿标点，看着像句子被从中间截断。这里按行收一遍，
    只留真正的台词。

    万一整条回复只有动作描写（例如「（金铃震动）……契约回响。」被清完），不能返回空串——
    空串会被上层当成"没有可念的内容"直接吞掉，主人那边就什么也收不到。这时退一步，
    把括号拆掉但保留里面的字，至少把话说明白。
    """
    raw = STATE_TAG.sub("", text or "").translate(QUOTE_MARKS)
    cleaned = _tidy_lines(tidy_punctuation(MD_MARKS.sub("", BRACKETS.sub("", raw))))
    if not cleaned:
        cleaned = _tidy_lines(tidy_punctuation(MD_MARKS.sub("", BRACKETS_INNER.sub(r"\1", raw))))
    # 一条回复只发一条消息：模型爱把四五句台词分行写，QQ 里会被拆成好几条，
    # 看着像刷屏。中文句末标点自带停顿，连接起来不影响阅读。
    merged = re.sub(r"[ \t　]*\n+[ \t　]*", "", cleaned).strip()
    # 合并之后行首的「——」「，」会紧贴上一句的句号，出现「。，」这种鬼东西
    merged = re.sub(r"([。！？!?])[，、；：]", r"\1", merged).strip()
    # 波浪号在提示词里禁了但它还是爱加（「想转圈圈～」）。分三种处理：
    # 挨着标点的直接去掉，剩下句中的换成逗号，别把两句话粘在一起
    merged = re.sub(r"(?<=[。！？!?，、；：])[～〜]+", "", merged)
    merged = re.sub(r"[～〜]+(?=[。！？!?，、；：]|$)", "", merged)
    merged = re.sub(r"[～〜]+", "，", merged)
    return merged.strip()


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
        self.voiceMuteUntil = 0.0  # 对方喊停后，到这个时间点前都不发语音
        self.lastSpokeUp = 0.0     # 上次主动搭话的时间
        self.speakUpStreak = 0     # 连续几次主动搭话对方没接话（用来退避）


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
        # always = 旧行为，每条都发；smart = 只在必要的时候发（默认）
        self.voiceMode = str(cfg.get("qqVoiceMode") or "smart").strip().lower()
        if self.voiceMode not in ("always", "smart"):
            self.voiceMode = "smart"
        self.showName = bool(cfg.get("qqNickname"))
        self.ownerQq = str(cfg.get("ownerQq") or "").strip()
        self.rate = float(cfg.get("qqRate") or 1.5)
        self.maxChars = int(cfg.get("qqMaxChars") or 400)

        # 主动搭话：她自己掷骰子决定要不要先开口。掷骰子是本地随机，不花 token；
        # 只有掷中之后那一轮才真的走模型
        self.speakUpOn = bool(cfg.get("qqSpeakUp"))
        self.speakUpChance = float(cfg.get("qqSpeakUpChance") or 0.15)
        self.speakUpEvery = int(cfg.get("qqSpeakUpEvery") or 1800)
        self.speakUpIdle = int(cfg.get("qqSpeakUpIdle") or 3600)
        self.speakUpDaily = int(cfg.get("qqSpeakUpDaily") or 4)
        self.speakUpQuiet = str(cfg.get("qqSpeakUpQuiet") or "1-8")
        self.speakUpTarget = str(cfg.get("qqSpeakUpTarget") or "").strip() or self.ownerQq
        self.speakUpTask = None

        rawGroups = str(cfg.get("qqGroups") or "").replace("，", ",")
        self.groups = {g.strip() for g in rawGroups.split(",") if g.strip()}

        self.sessions = {}
        self.seen = {}
        self.pending = {}
        self.seq = 0
        self.ws = None
        self.running = True
        self.semaphore = asyncio.Semaphore(4)  # 2G 小机器，别把上游打到限流
        self.stats = {"收到": 0, "回复": 0, "忽略": 0, "失败": 0, "搭话": 0}

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
        return self._get_session(userId, sessionId)

    def _get_session(self, userId, sessionId):
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
        session.lastSpokeUp = time.time()  # 进程刚起来时先别急着开口
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

    async def reply_text(self, session, userText, hiddenText=None):
        """同步 core 丢进线程池，别堵死事件循环。

        返回 (回复文本, 元信息)；元信息里带这条回复的 mood，用来判断该不该发语音。
        hiddenText 见 core.respond_iter：只改「历史里记什么」，不改模型看到的内容。
        """
        async with self.semaphore:
            try:
                text, meta = await asyncio.to_thread(session.core.respond, userText, hiddenText)
            except BrainError as exc:
                self.stats["失败"] += 1
                log(f"模型出错：{exc}")
                return "（脑袋突然卡住了…等下再说好不好）", {}
            except Exception as exc:  # 兜底，进程不能因为一条消息挂掉
                self.stats["失败"] += 1
                log(f"回复失败：{exc!r}")
                return "（出错了喵…）", {}

        if meta.get("error"):
            self.stats["失败"] += 1
            log(f"模型返回错误：{meta['error'][:200]}")
            return "（脑袋突然卡住了…等下再说好不好）", meta

        self.stats["回复"] += 1
        return forQQ(text), meta

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

    def take_emoji(self, reply, session):
        """把 <emoji name="x"/> 从回复里摘出来，返回 (干净文本, 表情名)。

        模型偶尔会写错名字、或者一口气写好几个，这里只认表里的第一个，
        再按会话控制间隔，免得每句都甩图。
        """
        if not reply:
            return reply, None
        name = None
        for match in EMOJI_TAG.finditer(reply):
            candidate = match.group(1).lower()
            if candidate in EMOJI_FILES:
                name = candidate
                break
        cleaned = EMOJI_TAG.sub("", reply)
        # 标签单独占一行时，删掉会留下空行，顺手收掉
        cleaned = "\n".join(line for line in cleaned.split("\n") if line.strip()).strip()
        if not name:
            return cleaned, None
        now = time.time()
        if now - getattr(session, "lastEmoji", 0.0) < EMOJI_COOLDOWN:
            return cleaned, None
        session.lastEmoji = now
        return cleaned, name

    async def send_image(self, kind, chatId, name):
        """发一张表情包。走 base64，不依赖协议端能不能读到容器里的路径。"""
        fileName = EMOJI_FILES.get(name)
        if not fileName:
            return False
        path = os.path.join(EMOJI_DIR, fileName)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            log(f"读表情失败（{name}）：{exc}")
            return False
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"
        uri = "base64://" + base64.b64encode(raw).decode("ascii")
        result = await self.call(
            action, {key: chatId, "message": [{"type": "image", "data": {"file": uri}}]}
        )
        if result and result.get("status") in ("failed", "error"):
            log(f"表情发送失败：{str(result)[:140]}")
            return False
        log(f"发表情：{name}")
        return True

    def voice_decision(self, kind, rawText, reply, meta, session, hasVoiceMsg=False):
        """这一轮要不要额外发一条语音？返回 (要发, 原因)。

        smart 模式（默认）只在三种情况下发：
          1. 对方点名要语音（"发个语音""念出来"）
          2. 对方自己发的是语音 → 语音回语音
          3. 特别开心（这条回复的 mood 是开心/得意）——只在私聊，群里不主动发
        另外对方说"别发语音"就静默 30 分钟。
        """
        if not self.voiceOn:
            return False, "语音功能未开"
        if not reply:
            return False, "没有可念的内容"

        now = time.time()
        # 喊停优先判断：静音词里也含"语音"，必须先判
        if VOICE_MUTE.search(rawText or ""):
            session.voiceMuteUntil = now + VOICE_MUTE_SECONDS
            log(f"对方要求别发语音，静默 {VOICE_MUTE_SECONDS // 60} 分钟")
            return False, "对方要求安静"
        if session.voiceMuteUntil > now:
            return False, "静默中"

        if self.voiceMode == "always":
            return len(reply) <= VOICE_HAPPY_LIMIT, "always 模式"

        if VOICE_REQUEST.search(rawText or ""):
            return True, "对方点名要语音"
        if hasVoiceMsg:
            return True, "语音对语音"
        if kind == "private" and (meta.get("replyMood") or "") in HAPPY_MOODS:
            return len(reply) <= VOICE_HAPPY_LIMIT, "特别开心"
        return False, ""

    def preferBase64(self):
        """协议端和机器人不在同一台机器时，容器里的 wav 路径它读不到（会报
        「Failed to open input: 没有那个文件或目录」），所以优先发 base64。

        CATGIRL_QQ_VOICE_URI 可以强制：base64 / file / auto（默认按 WS 地址判断）。
        """
        mode = str(self.cfg.get("qqVoiceUri") or "auto").strip().lower()
        if mode in ("base64", "b64"):
            return True
        if mode in ("file", "path", "file://"):
            return False
        host = re.sub(r"^[a-zA-Z]+://", "", self.wsUrl or "").split("/")[0]
        host = host.split("@")[-1]
        if host.startswith("["):
            host = host[1:].split("]")[0]
        else:
            host = host.split(":")[0]
        return host.lower() not in LOCAL_PROTOCOL_HOSTS

    async def send_voice(self, kind, chatId, text, userId):
        """把回复合成语音发出去。

        优先给协议端本地路径（同一卷、最快）；路径读不到就改 base64（协议端能自带数据）。
        两者顺序由 preferBase64() 决定，默认按协议端位置自动选。

        返回是否真的发出去了：成功了上层就不再发文字，免得同一句话听到两遍。
        """
        # 对方点名要语音时允许念长一点，但别把整篇小作文都塞进一条语音
        limit = VOICE_REQUEST_LIMIT
        if len(text) > limit:
            cut = text[:limit]
            for mark in ("。", "！", "？", "，", " "):
                pos = cut.rfind(mark)
                if pos > limit * 0.5:
                    cut = cut[: pos + 1]
                    break
            text = cut.strip()
        if not text:
            return False
        try:
            engine = TTS(self.cfg, os.path.join(self.cfg["dataDir"], "users", userId, "voice"))
            result = await asyncio.to_thread(engine.synthesize, text)
        except (TTSError, Exception) as exc:
            log(f"语音合成失败，改发文字：{exc}")
            return False

        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"

        path = result["path"]
        fileUri = "file://" + os.path.abspath(path).replace("\\", "/")

        # base64 数据：协议端读不到我们的文件系统时（NapCat 跑在另一台机器上）用它
        b64Uri = None
        try:
            size = os.path.getsize(path)
            if size <= VOICE_B64_LIMIT:
                with open(path, "rb") as handle:
                    b64Uri = "base64://" + base64.b64encode(handle.read()).decode("ascii")
            else:
                log(f"语音文件 {size // 1024}KB 偏大，不做 base64 兜底")
        except OSError as exc:
            log(f"读语音文件失败：{exc}")

        if self.preferBase64():
            attempts = [("base64", b64Uri), ("文件路径", fileUri)]
        else:
            attempts = [("文件路径", fileUri), ("base64", b64Uri)]

        for label, uri in attempts:
            if not uri:
                continue
            sent = await self.call(action, {key: chatId,
                                            "message": [{"type": "record", "data": {"file": uri}}]})
            if sent and sent.get("status") not in ("failed", "error"):
                return True
            log(f"语音发送失败（{label}），换另一种方式")

        log("语音发送失败（协议端可能不支持该格式），改发文字")
        return False

    # ------------------------------------------------------------ 主动搭话
    @staticmethod
    def _quiet_range(spec):
        """把 "1-8" / "23-8" 解析成 (起, 止) 小时；解析不出来就当没设静默时段。"""
        m = re.match(r"^\s*(\d{1,2})\s*[-~]\s*(\d{1,2})\s*$", str(spec or ""))
        if not m:
            return None
        start, end = int(m.group(1)) % 24, int(m.group(2)) % 24
        return None if start == end else (start, end)

    def in_quiet_hours(self, hour=None):
        """现在是不是她该闭嘴的时段（别半夜三点发消息）。支持跨午夜，如 23-8。"""
        span = self._quiet_range(self.speakUpQuiet)
        if not span:
            return False
        hour = time.localtime().tm_hour if hour is None else hour
        start, end = span
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    @staticmethod
    def _part_of_day(hour):
        if 5 <= hour < 8:
            return "清早"
        if 8 <= hour < 11:
            return "上午"
        if 11 <= hour < 13:
            return "中午"
        if 13 <= hour < 17:
            return "下午"
        if 17 <= hour < 19:
            return "傍晚"
        if 19 <= hour < 23:
            return "晚上"
        return "深夜"

    @staticmethod
    def _span_text(seconds):
        """把秒数说成「43 分钟」「3 小时」，写进日志和提示词里。"""
        minutes = int(seconds // 60)
        if minutes < 90:
            return f"{minutes} 分钟"
        hours = minutes / 60
        if hours < 24:
            return f"{hours:.0f} 小时"
        return f"{hours / 24:.0f} 天"

    @staticmethod
    def _speak_up_used(session):
        """今天已经主动开口几次。计数存在她自己的记忆库里，进程重启也不会清零。"""
        raw = str(session.core.store.get_kv("speakUpQuota", "") or "")
        day, _, count = raw.partition(":")
        if day != time.strftime("%Y-%m-%d"):
            return 0
        try:
            return int(count)
        except ValueError:
            return 0

    @staticmethod
    def _bump_speak_up(session):
        used = QQBot._speak_up_used(session) + 1
        session.core.store.set_kv("speakUpQuota", f"{time.strftime('%Y-%m-%d')}:{used}")
        return used

    async def maybe_speak_up(self):
        """定时器到点调一次：先过四道闸，最后才掷骰子，掷中了才花 token。

        四道闸按代价从低到高排，任何一道不过就直接返回，一个 token 都不花：
          1. 没开 / 没连上 / 不知道该对谁说话
          2. 现在是她该安静的时段
          3. 安静得还不够久（连续被晾着时，这个「够久」会翻倍）
          4. 今天已经说够次数了
        """
        if not self.speakUpOn or self.ws is None or not self.speakUpTarget:
            return
        if self.in_quiet_hours():
            return

        target = self.speakUpTarget
        session = self.sessions.get(f"qq{target}") or self._get_session(f"qq{target}", f"p{target}")

        idle = time.time() - max(session.lastUsed, session.lastSpokeUp)
        # 被晾过几次就等更久再开口：1 次翻倍、2 次四倍，最多八倍
        needIdle = self.speakUpIdle * (2 ** min(session.speakUpStreak, 3))
        if idle < needIdle:
            return
        if self._speak_up_used(session) >= self.speakUpDaily:
            return

        if random.random() > self.speakUpChance:
            log(f"主动搭话：骰子没中（已安静 {self._span_text(idle)}）")
            return

        hour = time.localtime().tm_hour
        hint = (
            f"（{self._part_of_day(hour)}，主人已经 {self._span_text(idle)}没说话了，"
            "聊天窗口一直安静着。你自己想找他说句话。"
            "别提这条提示，别说「主动搭话」这四个字，别问「在吗」「在干嘛」，"
            "直接说一句你此刻心里冒出来的话，两三句以内。）"
        )
        try:
            async with session.lock:
                reply, meta = await self.reply_text(
                    session, hint, hiddenText="（安静了一会儿，你先开的口）"
                )
                # 出错时 reply_text 会回一句「（脑袋卡住了…）」，那种话不能主动发出去
                if not reply or meta.get("error") or reply.startswith("（"):
                    return
                await self.send("private", target, reply, session)
        except Exception as exc:
            log(f"主动搭话失败：{exc!r}")
            return

        session.lastSpokeUp = time.time()
        session.speakUpStreak += 1  # 先按「他没接话」记账，主人一说话就清零
        used = self._bump_speak_up(session)
        self.stats["搭话"] += 1
        log(
            f"主动搭话：已发给 {target}（今天第 {used}/{self.speakUpDaily} 次，"
            f"安静了 {self._span_text(idle)}，连续第 {session.speakUpStreak} 次）"
        )

    async def speak_up_loop(self):
        """后台定时器。先静一会儿再开始，别刚重启就开口。"""
        await asyncio.sleep(min(300, self.speakUpEvery))
        while self.running:
            try:
                await self.maybe_speak_up()
            except Exception as exc:  # 这条后台线不能死，死了就永远不开口了
                log(f"主动搭话异常：{exc!r}")
            await asyncio.sleep(self.speakUpEvery)

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
        # 原文留一份：后面图片/语音会被替换成系统说明，
        # 但"对方要不要语音"得按他真正说的话来判断
        rawText = text

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
        session.speakUpStreak = 0  # 他说话了，主动搭话的退避重来

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
            reply, meta = await self.reply_text(session, userText)
            reply, emojiName = self.take_emoji(reply, session)
            if not reply.strip():
                # 出口清洗把整条回复清空了（模型只给了动作描写或只发了表情），
                # 空串会被 send() 当没有内容静默丢掉，主人这边就是「没反应」。
                log("模型没给出可发的台词，用兜底一句顶上")
                reply = "……嗯。"
            wantVoice, why = self.voice_decision(
                kind, rawText, reply, meta, session, bool(records)
            )
            if wantVoice:
                log(f"发语音（{why}）")
                # 语音发成功就不再补一条文字，否则同一句话会被听到、又被看到
                if await self.send_voice(kind, chatId, reply, session.key):
                    session.lastSent = time.time()
                    if emojiName:
                        await self.send_image(kind, chatId, emojiName)
                    return
            await self.send(kind, chatId, reply, session)
            if emojiName:
                await self.send_image(kind, chatId, emojiName)

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
        # 主动搭话是独立的一条后台线，跟 WS 连接的重连逻辑互不干扰：
        # WS 断了它照样在跑，只是 maybe_speak_up 里发现 self.ws is None 就跳过
        if self.speakUpOn and self.speakUpTarget:
            self.speakUpTask = asyncio.create_task(self.speak_up_loop())
            log(
                f"主动搭话已开：每 {self.speakUpEvery // 60} 分钟掷一次骰子"
                f"（{self.speakUpChance:.0%}），每天最多 {self.speakUpDaily} 次，"
                f"要安静 {self.speakUpIdle // 60} 分钟以上，静默时段 {self.speakUpQuiet} 点"
            )
        elif self.speakUpOn:
            log("主动搭话已开但不知道对谁开口：设 CATGIRL_OWNER_QQ 或 CATGIRL_QQ_SPEAK_UP_TARGET")

        delay = 2
        try:
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
        finally:
            if self.speakUpTask:
                self.speakUpTask.cancel()


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
