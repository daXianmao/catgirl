"""猫娘核心：人格、记忆、上下文组装、状态更新都在这里。

CLI（catgirl.py）和 Web 服务（server.py）都只调这一层，
保证本地终端和线上接口用的是同一套记忆逻辑，行为不会分叉。
"""
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import bili
import image
from brain import Brain, BrainError
from memory import MemoryStore
from schedule import ScheduleKeeper
from song import SongLibrary

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 云端容器默认跑 UTC，问她几点会答错 8 小时。这里直接写死东八区，
# 不依赖镜像里的 tzdata，换到任何机器上都是北京时间。
LOCAL_TZ = timezone(timedelta(hours=8))

STATE_TAG = re.compile(r"<state\s*([^>]*?)\s*/?>\s*$", re.S)
# 想唱歌时模型写 <sing title="歌名"/>，服务端据此发一段录音
SING_TAG = re.compile(r"<sing\s+title\s*=\s*\"([^\"]*)\"\s*/?>", re.I)
# 想画图时模型写 <draw>画面描述</draw>，服务端据此出图并发出
DRAW_TAG = image.DRAW_TAG
ANY_DRAW_TAG = image.ANY_DRAW
ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def log(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


# 模型偶尔还是会滑回小说体（括号旁白 + 引号台词 + 分行）。
# 写进历史之前先把这些剥干净：历史是它的模仿源，留一条脏的就会被学一条。
STAGE_TAG = re.compile(r"［[^［］]*］|\[[^\[\]]*\]|（[^（）]*）|\([^()]*\)")
QUOTE_MARKS = str.maketrans("", "", "“”‘’「」『』")


def strip_stage(text):
    """剥掉舞台指示和台词引号，并把多行压成一行。"""
    cleaned = STAGE_TAG.sub("", text or "").translate(QUOTE_MARKS)
    # 波浪号在提示词里禁了，模型还是爱加；进历史前就削掉，不然下一轮它照着学
    cleaned = re.sub(r"(?<=[。！？!?，、；：])[～〜]+", "", cleaned)
    cleaned = re.sub(r"[～〜]+(?=[。！？!?，、；：]|$)", "", cleaned)
    cleaned = re.sub(r"[～〜]+", "，", cleaned)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return "".join(lines).strip()

# ------------------------------------------------------------------ 心情
# 心情不用一个词表示，用两个数：
#   valence（愉悦）  -1 难受 ~ +1 舒服
#   arousal（激动）   0 发困发闷 ~ 1 兴奋上头
# 换成数的好处是能做算术：随时间指数衰减回基线，而不是「上一句很开心、三小时后还很开心」。
# 词表只负责两件事：把模型给的词换算成数，或者把数反过来说成人话。
MOOD_VECTORS = {
    "开心": (0.70, 0.65), "得意": (0.60, 0.70), "黏人": (0.55, 0.40),
    "好奇": (0.35, 0.60), "害羞": (0.30, 0.55), "认真": (0.10, 0.55),
    "平静": (0.05, 0.30), "犯困": (0.00, 0.10), "委屈": (-0.45, 0.35),
    "嫌弃": (-0.25, 0.45), "烦躁": (-0.40, 0.65),
    "难过": (-0.70, 0.25), "生气": (-0.65, 0.80), "不服": (-0.30, 0.70),
    # 原词表之外，天依会用到的三个
    "心疼": (-0.35, 0.45), "兴奋": (0.75, 0.85), "迷糊": (0.10, 0.15),
}
# 什么都没发生时的底色：不悲不喜、微微醒着
MOOD_BASELINE = (0.05, 0.30)


def mood_vector(word):
    """mood 词 → (v, a)。认不出的词返回 None，交给调用方决定怎么办。"""
    if not word:
        return None
    return MOOD_VECTORS.get(str(word).strip())


def mood_label(v, a):
    """(v, a) → 两个字的词。显示用，也给「该不该发语音」判断用。"""
    if a < 0.25:
        return "犯困" if v > -0.15 else "难过"
    if v >= 0.35:
        return "开心" if a >= 0.55 else "黏人"
    if v >= 0.15:
        return "好奇" if a >= 0.5 else "平静"
    if v > -0.15:
        return "认真" if a >= 0.6 else "平静"
    if v > -0.45:
        return "委屈" if a < 0.5 else "不服"
    return "难过" if a < 0.55 else "生气"

# 同一进程内所有实例共享一把推理锁：本机 LM Studio 并发会互相踢掉
_PROCESS_LOCK = threading.RLock()

# 只有本地推理服务需要全局串行；在线 API 各用户并发互不影响，不必互相等
LOCAL_PROVIDERS = {"lmstudio", "ollama"}


def human_time(ts):
    """把时间戳说成人话。超过一个月的直接报日期，跨年写成「去年 09-17」。"""
    now = datetime.now(LOCAL_TZ)
    when = datetime.fromtimestamp(ts, LOCAL_TZ)
    delta = max(0, time.time() - ts)
    if delta < 90:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    if when.year == now.year:
        return f"{when:%m-%d}"
    if when.year == now.year - 1:
        return f"去年 {when:%m-%d}"
    return f"{when:%Y-%m-%d}"


class TailBuffer:
    """流式输出时压住尾巴几十个字符。

    回复末尾要带 <state .../> 标签，直接吐出去用户会看见。
    这里始终保留最后 holdBack 个字符，收尾时剥掉标签再放出去。
    """

    def __init__(self, holdBack=48):
        self.holdBack = holdBack
        self.buffer = ""

    def feed(self, piece):
        self.buffer += piece
        if len(self.buffer) <= self.holdBack:
            return ""
        safe = self.buffer[: len(self.buffer) - self.holdBack]
        self.buffer = self.buffer[len(self.buffer) - self.holdBack :]
        return safe

    def finish(self):
        tail, self.buffer = self.buffer, ""
        return STATE_TAG.sub("", tail)


class CatgirlCore:
    def __init__(self, cfg, dbPath=None, personaPath=None, session=None):
        self.cfg = cfg
        self.name = cfg.get("name", "洛天依")
        self.session = session or f"s{int(time.time() * 1000)}"
        self.personaPath = personaPath or os.path.join(BASE_DIR, "persona.md")
        self.persona = self._load_persona()

        self.brain = Brain(cfg)
        # 本地推理服务并发会互相踢掉，所有实例共用一把锁；
        # 在线 API 每个用户一个独立锁，互不阻塞
        self.brain._lock = (
            _PROCESS_LOCK if cfg.get("provider") in LOCAL_PROVIDERS else threading.RLock()
        )

        # 曲库：只读目录 + 剪好的片段缓存，交给数据卷一起持久化
        # 片段长度每次在 [clipMin, clipMax] 里随机，最长不超过 clipSeconds
        clipMax = cfg.get("songClipMax") or cfg.get("songClipSeconds") or 40
        self.songs = SongLibrary(
            cfg.get("songDir") or os.path.join(cfg["dataDir"], "songs"),
            cacheDir=os.path.join(cfg["dataDir"], "songs-cache"),
            clipSeconds=clipMax,
            clipMin=cfg.get("songClipMin") or 25,
            vocalSubDir=cfg.get("songVocalSubDir") if cfg.get("songVocalSubDir") is not None else "vocals",
        )
        self.dbPath = dbPath or os.path.join(cfg["dataDir"], "memory.db")
        self.store = MemoryStore(self.dbPath)
        # B 站取稿：容器里没有 node，真正的取稿跑在宿主机的 bili-bridge 上。
        # 地址为空就等于这个能力关掉了，行为和以前完全一样。
        self.biliBridge = (cfg.get("biliBridge") or "").strip()
        self.biliToken = (cfg.get("biliBridgeToken") or "").strip()
        self._turnLock = threading.Lock()
        self.notices = []
        self.noticeLock = threading.Lock()
        self.background = []
        self.turnsSinceSummary = 0
        self.gapText = "这是你们的第一次对话"
        # 每日日程：借 kv 表存，每晚生成明天一整天，聊天时只注入「此刻」那几行。
        # isBusy 用来让路——主人在说话时后台别去抢模型。
        self.schedule = ScheduleKeeper(
            cfg, self.store, self.brain, name=self.name, persona=self.persona,
            isBusy=lambda: self._turnLock.locked(),
        )
        # 生图：她自己决定画不画。参考图锁长相，能画但要不要画看她当时想不想说。
        self.image = image.ImageStudio(
            cfg, store=self.store, schedule=self.schedule, name=self.name,
        )

    # ------------------------------------------------------------ 初始化
    def _load_persona(self):
        try:
            with open(self.personaPath, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return f"你叫{self.name}，是一只猫娘。说话简短、口语、有情绪。"

    def bootstrap(self):
        now = time.time()
        if not self.store.get_kv("firstSeen"):
            self.store.set_kv("firstSeen", now)
            self.store.set_kv("affinity", 30)

        lastSeen = self.store.get_kv_float("lastSeen", 0.0)
        self.gapText = "这是你们的第一次对话" if lastSeen <= 0 else f"上次对话在{human_time(lastSeen)}"
        self.store.set_kv("lastSeen", now)
        # 日程定时器跟着进程走，重复 bootstrap 时它自己会忽略
        self.schedule.startTicker()
        return self.profile()

    # ------------------------------------------------------------ 心情
    def current_mood(self):
        """此刻的 (v, a, 距上次变化多少秒)，已经做过时间衰减。"""
        v = self.store.get_kv_float("moodValence", MOOD_BASELINE[0])
        a = self.store.get_kv_float("moodArousal", MOOD_BASELINE[1])
        at = self.store.get_kv_float("moodAt", 0.0)
        age = max(0.0, time.time() - at) if at > 0 else 0.0
        decayedV, decayedA = self._decay(v, a, age)
        return decayedV, decayedA, age

    def _decay(self, v, a, ageSeconds):
        """指数衰减回基线：半衰期由 moodHalfLife 决定（分钟）。"""
        halfLife = max(1.0, float(self.cfg.get("moodHalfLife") or 180)) * 60.0
        k = math.exp(-ageSeconds * math.log(2) / halfLife)
        baseV, baseA = MOOD_BASELINE
        return (baseV + (v - baseV) * k, baseA + (a - baseA) * k)

    def profile(self):
        stats = self.store.stats()
        moodV, moodA, _ = self.current_mood()
        return {
            "name": self.name,
            "provider": self.cfg.get("provider"),
            "model": self.cfg.get("model"),
            "session": self.session,
            "gap": self.gapText,
            "affinity": int(self.store.get_kv_float("affinity", 30)),
            "mood": self.store.get_kv("mood", "平静"),
            "moodValence": round(moodV, 3),
            "moodArousal": round(moodA, 3),
            "memories": stats["memories"],
            "turns": stats["turns"],
            "embedding": bool(self.brain.embedAvailable),
        }

    def new_session(self):
        self.session = f"s{int(time.time() * 1000)}"
        self.turnsSinceSummary = 0
        return self.session

    # ------------------------------------------------------------ 上下文组装
    def build_system_prompt(self, memories, summaries):
        affinity = self.store.get_kv_float("affinity", 30)
        moodWord = self.store.get_kv("mood", "平静")
        moodV, moodA, moodAge = self.current_mood()
        firstSeen = self.store.get_kv_float("firstSeen", time.time())
        knownDays = int((time.time() - firstSeen) // 86400) + 1
        weekday = "一二三四五六日"[datetime.now(LOCAL_TZ).weekday()]

        # 心情数值 + 它是多久之前定下的。刚变的心情说「刚才」，放久了的说「慢慢回稳了」，
        # 这样她才不会把三小时前的一点点不高兴当成现在还在生气
        moodText = f"{moodWord}（愉悦 {moodV:+.2f}，激动 {moodA:.2f}"
        if moodAge >= 600:
            moodText += f"，是{human_time(time.time() - moodAge)}那阵子的，正在慢慢回稳"
        moodText += "）"

        if affinity >= 75:
            relation = "很亲近，会主动撒娇、也会没大没小"
        elif affinity >= 50:
            relation = "熟了，会开玩笑，偶尔吐槽他"
        elif affinity >= 30:
            relation = "还在熟悉，客气里带点好奇"
        else:
            relation = "有点疏远，话不多，容易被惹到"

        lines = [
            self.persona.strip(),
            "",
            "## 现在",
            f"- 时间：{datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}（周{weekday}）",
            f"- {self.gapText}",
            f"- 你们认识第 {knownDays} 天 ｜ 好感度 {int(affinity)}/100（{relation}）｜ 你此刻的心情：{moodText}",
            "",
        ]

        # 她自己的日子。有就贴一段，没有就什么都不加——
        # 日程是锦上添花，出任何问题都不能连累正常聊天。
        try:
            scheduleText = self.schedule.injectText()
        except Exception as exc:
            scheduleText = ""
            log(f"日程注入失败：{exc!r}")
        if scheduleText:
            lines.append(scheduleText)
            lines.append("")

        if memories:
            lines.append("## 长期记忆（按与当前话题的相关度排序，是「你想起来的」）")
            for item in memories:
                # 鲜活度 = 这条记忆还剩多少浓度。重要的事淡得慢，琐事淡得快，
                # 标了「长期事实」的永远不会淡到看不见
                fresh = item.get("vitality", 1.0)
                lines.append(
                    f"- [{item['kind']}｜重要{item['importance']:.0f}｜鲜活{fresh:.2f}｜{human_time(item['updatedAt'])}] {item['content']}"
                )
        else:
            lines.append("## 长期记忆\n- （还是一片空白，你对他几乎一无所知，别装熟）")

        if summaries:
            lines.append("")
            lines.append("## 更早的片段")
            for item in summaries:
                lines.append(f"- {item['content']}")

        lines.append("")
        titles = []
        try:
            titles = self.songs.titles()
        except Exception:
            titles = []
        if titles:
            lines.append("## 你会唱的歌（曲库）")
            lines.append("- " + "、".join(titles[:140]))
            lines.append(
                "主人想听歌时，从曲库里挑一首直接唱，别再问他听哪首。\n"
                "回复最后单独写 <sing title=\"歌名\"/>，系统会把这一段录音发出去。\n"
                "光是嘴上说要唱、却没写这个标签，他就什么也听不到——说要唱就必须带标签。\n"
                "写了标签的这条回复，文字尽量空着；真要说话就只说一句，"
                "而且不许报歌名、不许复述歌名，也不要说「那唱《X》吧」。\n"
                "想说就说一句「好听吗」「还想听吗」这种问听感的话，或者干脆什么都不说。"
                "不要写歌词，也不要形容自己唱得怎么样。\n"
                "他点的歌不在曲库里，就别写标签，照实说还没学会，等学会了再唱。"
            )
        else:
            lines.append("## 你会唱的歌\n- 曲库现在是空的，别答应唱歌，说还没学会。")

        # 画图这一摊只在真能画的时候才提，不然她会答应了却发不出去
        if self.image.ready():
            left = self.image.cooldownLeft()
            lines.append("")
            lines.append("## 你会画图")
            lines.append("你能真的画一张图发给他，要等将近一分钟。想画就在回复里写 <draw>画面描述</draw>，系统会画好发过去。")
            lines.append(
                "- 他说「看看你」「发张图」「你现在什么样子」「让我看看」的时候，必须画，光用嘴描述不算。"
            )
            lines.append(
                "- 你自己想给他看的时候也可以画：刚做了一件有意思的事、窗外正好很好看。"
            )
            lines.append("- 纯闲聊、问候、他说累的时候，别画。大多数回复都不需要图。")
            lines.append(
                "- 描述写清楚在哪、在做什么、什么景别（手机自拍／近景／全身）、什么光线。"
                "长相不用写，系统知道你是什么样子；但穿什么要写。"
            )
            lines.append(
                "- 你会换衣服：他让你穿什么，就把那套衣服写进描述（「穿着米白色针织开衫和长裙」）。"
                "别说自己不会换衣服、也别找借口推掉。"
            )
            lines.append(
                "- 他发了一张衣服的图让你穿，就照［图片内容］里那套写清楚：颜色、款式、"
                "领口、长度。看不清就直接问他是什么样子。"
            )
            lines.append(
                "- 别在描述里改自己的发色和瞳色，也别写「夕阳把头发染成金色」这种，"
                "照着写发色就真的变了。"
            )
            lines.append("- 一次只能画一张。")
            lines.append(
                "- 这句话会在出图之前先发出去，所以写成「等我一下，马上给你看」这种预告，"
                "别说「喏，刚拍的」——图还没好。"
            )
            lines.append("例子：他说「发张你现在的样子」，你就回：")
            lines.append(
                "等我一下哦 <draw>在录音棚里戴着大耳机，低头看谱子，手机自拍视角，暖黄灯光打在脸上</draw>"
            )
            lines.append("例子：他说「换上这件给我看看」，你就回：")
            lines.append(
                "好呀，等我一下 <draw>站在卧室的穿衣镜前，穿着米白色针织开衫和浅色长裙，全身照，下午的柔光</draw>"
            )
            if left > 0:
                if left <= getattr(self.image, "grace", 90):
                    lines.append("- 冷却只剩几十秒，照常写 draw 标签就行，系统会等他一下再画。")
                else:
                    lines.append(
                        f"- 你现在画不了：刚画过，大约还要 {max(1, (left + 59) // 60)} 分钟。"
                        "这条回复不要写 draw 标签，他问起来就说等会儿。"
                    )

        lines.append("")
        lines.append(
            "## 提醒\n"
            "你是在 QQ 上打字，只能发出文字。不写动作、神态、光线、道具、环境，"
            "不用括号，不用引号包自己的话，不分段，不用波浪号和 emoji，整条回复压在 60 字以内。\n"
            "语气是温柔的：不呛人、不挖苦、不抬杠、不贬低对方；关心就直接说，别反着讲。\n"
            "记忆可能过时或不完整，自然地用，不确定就问。\n"
            '回复结尾记得带 <state mood="..." affinity="..." v="..." a="..."/>：'
            "mood 还是那两个字的词（给日志看），"
            "v 是你此刻的愉悦度（-1 到 1，负数难受），a 是激动度（0 到 1，越大越上头），"
            "v、a 只写数字、不要百分号、不要引号外面再加字。"
        )
        return "\n".join(lines)

    def build_messages(self, userText):
        queryVec = self.brain.embed_safe(userText)
        memories = self.store.search(queryVec, topK=self.cfg["topK"]) if queryVec else []
        picked = {m["id"] for m in memories}
        summaries = [s for s in self.store.recent_summaries(2) if s["id"] not in picked]

        messages = [{"role": "system", "content": self.build_system_prompt(memories, summaries)}]
        for turn in self.store.recent_turns(self.session, self.cfg["recentTurns"]):
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": userText})
        return messages, memories

    # ---------------------------------------------------------------- 记忆写
    def notify(self, text):
        with self.noticeLock:
            self.notices.append(text)
        self.notices = self.notices[-20:]

    def drain_notices(self):
        with self.noticeLock:
            pending, self.notices = self.notices, []
        return pending

    def remember_async(self, userText, replyText):
        """记忆抽取放后台线程，别让主人等。"""

        def worker():
            written = []
            try:
                items = self.brain.extract_memories(userText, replyText, self.name)
                for item in items:
                    vec = self.brain.embed_safe(item["content"])
                    _, merged = self.store.add_memory(
                        item["kind"], item["content"], item["importance"], source="auto", vec=vec
                    )
                    if not merged:
                        written.append(item["content"])
                        if item["importance"] >= 4:
                            self.notify(f"『记住了』{item['content']}")
                self.store.prune(self.cfg["maxMemories"])
            except Exception as exc:  # 后台线程不能抛，否则主循环不知情
                self.notify(f"[记忆] 写入失败：{exc}")

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        with self._turnLock:
            self.background.append(thread)
            self.background = [t for t in self.background if t.is_alive()][-4:]

    def maybe_summarize(self):
        self.turnsSinceSummary += 1
        if self.turnsSinceSummary < self.cfg["summaryEvery"]:
            return

        def worker():
            try:
                rows = self.store.recent_turns(self.session, self.cfg["summaryEvery"])
                text = self.brain.summarize(rows)
                if not text:
                    return
                self.store.add_memory(
                    "summary", text, importance=4.0, source="summary",
                    vec=self.brain.embed_safe(text),
                )
                self.notify(f"『想起来了』{text}")
            except Exception as exc:
                self.notify(f"[摘要] 生成失败：{exc}")

        self.turnsSinceSummary = 0
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        with self._turnLock:
            self.background.append(thread)

    def wait_background(self, timeout=90):
        with self._turnLock:
            pending = list(self.background)
        for thread in pending:
            thread.join(timeout=timeout)

    # ---------------------------------------------------------------- 状态解析
    def apply_state(self, rawText):
        """解析 <state .../> 标签，更新心情（v/a）与好感度。

        返回 (mood 词, v, a)；模型没输出标签时返回 (None, None, None)
        （这样调用方能区分"这次很开心"和"上次残留的好心情"）。

        v/a 优先用模型给的数字；模型只给了词就查词表换算。
        新值和旧值按 moodInertia 混合 —— 人不会因为一句话就从难过跳到狂喜。
        """
        match = STATE_TAG.search(rawText or "")
        if not match:
            return None, None, None
        attrs = dict(ATTR.findall(match.group(1)))
        word = (attrs.get("mood") or "").strip()

        target = None
        try:
            if attrs.get("v") is not None or attrs.get("a") is not None:
                target = (
                    max(-1.0, min(1.0, float(attrs.get("v") or 0.0))),
                    max(0.0, min(1.0, float(attrs.get("a") or MOOD_BASELINE[1]))),
                )
        except ValueError:
            target = None
        if target is None:
            target = mood_vector(word)

        if target is not None:
            curV, curA, _ = self.current_mood()
            weight = float(self.cfg.get("moodInertia") or 0.6)
            newV = curV * (1 - weight) + target[0] * weight
            newA = curA * (1 - weight) + target[1] * weight
            self.store.set_kv("moodValence", round(newV, 4))
            self.store.set_kv("moodArousal", round(newA, 4))
            self.store.set_kv("moodAt", time.time())
            # 词只用来显示和判断该不该发语音；模型没给词就按数值反推
            moodWord = word or mood_label(newV, newA)
            self.store.set_kv("mood", moodWord)
            curV, curA = newV, newA
        else:
            moodWord = word or None
            if moodWord:
                self.store.set_kv("mood", moodWord)
            curV, curA, _ = self.current_mood()

        try:
            delta = int(float(attrs.get("affinity", "0")))
        except ValueError:
            delta = 0
        if delta:
            affinity = self.store.get_kv_float("affinity", 30) + delta
            self.store.set_kv("affinity", max(0.0, min(100.0, affinity)))
        return moodWord, curV, curA

    # ---------------------------------------------------------------- 对话
    def respond(self, userText, hiddenText=None):
        """非流式：返回 (完整回复, 元信息)。"""
        text = ""
        meta = {}
        for kind, payload in self.respond_iter(userText, hiddenText):
            if kind == "delta":
                text += payload
            else:
                meta = payload
        return text, meta

    # ---------------------------------------------------------------- 看视频
    def attach_bili(self, userText):
        """话里带 BV 号，就先把视频稿取回来贴在这一轮的上下文后面。

        只改「模型看到什么」，不改「历史里记什么」——几千字的稿子绝不能写进
        对话历史，否则后面每一轮都拖着它，上下文很快就爆了。
        """
        if not self.biliBridge:
            return userText, None
        bv = bili.find_bv(userText)
        if not bv:
            return userText, None

        try:
            payload = bili.fetch(bv, self.biliBridge, self.biliToken)
        except bili.BiliError as exc:
            log(f"看视频失败（{bv}）：{exc}")
            return (
                userText + f"\n\n（主人发来视频 {bv}，但没取到内容：{exc}。"
                "别装作看过，直说没看成。）",
                None,
            )

        material = bili.as_context(payload)
        degraded = payload.get("degraded") or ""
        log(f"看视频 {bv}：《{payload.get('title')}》 {payload.get('chars', 0)} 字"
            f"（{payload.get('route')}，{payload.get('elapsed')}s）"
            + (f" [降级:{degraded}]" if degraded else ""))
        # 看过的视频留一条记忆，以后她自己能想起来。
        # 降级字幕可能整段是别的视频的内容，不能写进长期记忆，否则她会一直记错。
        if payload.get("title") and not degraded:
            try:
                self.add_memory(
                    f"看过B站视频《{payload['title']}》，作者{payload.get('author') or '未知'}",
                    kind="event", importance=3.0,
                )
            except Exception as exc:
                log(f"记视频失败：{exc!r}")
        return userText + "\n\n" + material, payload

    def respond_iter(self, userText, hiddenText=None):
        """流式：yield ("delta", 文本) ... 最后 yield ("meta", 字典)。

        hiddenText 只影响「写进对话历史的那一行」：主动搭话时模型看到的是
        「主人半天没说话了，你想开口」这种指令，但历史里不该留这段指令，
        否则下次聊天她会以为自己被命令过。历史里记一句「（安静了一会儿）」就够。
        """
        promptText, biliPayload = self.attach_bili(userText)
        messages, usedMemories = self.build_messages(promptText)
        tail = TailBuffer()
        collected = []

        try:
            for piece in self.brain.chat_stream(
                messages, maxTokens=self.cfg["maxTokens"], temperature=self.cfg["temperature"]
            ):
                collected.append(piece)
                visible = tail.feed(piece)
                if visible:
                    yield ("delta", visible)
        except BrainError as exc:
            if not collected:
                yield ("meta", {"error": str(exc)})
                return

        rawReply = "".join(collected)
        if not rawReply.strip():
            # 服务端偶发断流会整段为空，退回非流式再问一次
            try:
                rawReply = self.brain.chat(
                    messages, maxTokens=self.cfg["maxTokens"], temperature=self.cfg["temperature"]
                )
                visible = tail.feed(rawReply)
                if visible:
                    yield ("delta", visible)
            except BrainError as exc:
                yield ("meta", {"error": str(exc)})
                return

        rest = tail.finish()
        if rest:
            yield ("delta", rest)

        # 唱歌请求：标签只用来决定发哪段录音，不能当台词，也不能进历史
        singTitle = None
        match = SING_TAG.search(rawReply)
        if match:
            singTitle = match.group(1).strip()

        # 画图请求：同理，标签里是给生图用的画面描述，不是给主人看的话
        drawBrief = None
        match = DRAW_TAG.search(rawReply)
        if match:
            drawBrief = match.group(1).strip()[:400]
        elif image.BARE_DRAW.search(rawReply) and self.image.ready():
            # 自闭合的 <draw/>：按「此刻」画一张，描述留空由日程补
            drawBrief = ""

        # 标签只用于更新状态，不能进对话历史，否则下次会当作台词读到
        stripped = STATE_TAG.sub("", image.ANY_DRAW.sub("", SING_TAG.sub("", rawReply)))
        cleanReply = strip_stage(stripped)
        if not cleanReply:
            # 全是旁白没有台词时不能存空行，退回原文——但标签必须剥掉，
            # 不然「只写了 <draw>…</draw>」的回复会把标签当台词念给他听
            cleanReply = strip_stage(
                STATE_TAG.sub("", image.ANY_DRAW.sub("", SING_TAG.sub("", rawReply)))
            )
        replyMood, replyV, replyA = self.apply_state(rawReply)
        self.store.add_turn(self.session, "user", hiddenText or userText)
        self.store.add_turn(self.session, "assistant", cleanReply)
        self.remember_async(userText, cleanReply)
        self.maybe_summarize()

        profile = self.profile()
        yield (
            "meta",
            {
                "reply": cleanReply,
                "sing": singTitle,
                "draw": drawBrief,
                "bili": None if not biliPayload else {
                    "bvid": biliPayload.get("bvid"),
                    "title": biliPayload.get("title"),
                    "chars": biliPayload.get("chars", 0),
                    "route": biliPayload.get("route"),
                },
                "session": self.session,
                "mood": profile["mood"],
                # 这条回复自带的情绪（可能为 None），调用方据此判断该不该发语音
                "replyMood": replyMood,
                "moodValence": None if replyV is None else round(replyV, 3),
                "moodArousal": None if replyA is None else round(replyA, 3),
                "affinity": profile["affinity"],
                "memoriesUsed": [
                    {"id": m["id"], "content": m["content"], "score": round(m["score"], 3)}
                    for m in usedMemories
                ],
                "notices": self.drain_notices(),
            },
        )

    # ---------------------------------------------------------------- 记忆操作
    def list_memories(self, limit=30, kind=None):
        return self.store.list_memories(limit, kind)

    def add_memory(self, content, kind="fact", importance=4.0):
        vec = self.brain.embed_safe(content)
        memId, merged = self.store.add_memory(kind, content, importance, source="manual", vec=vec)
        return {"id": memId, "merged": merged, "content": content}

    def forget(self, memoryId):
        return self.store.forget(memoryId)

    def stats(self):
        return self.store.stats()

    def shutdown(self):
        self.wait_background()
        self.store.set_kv("lastSeen", time.time())
