"""猫娘核心：人格、记忆、上下文组装、状态更新都在这里。

CLI（catgirl.py）和 Web 服务（server.py）都只调这一层，
保证本地终端和线上接口用的是同一套记忆逻辑，行为不会分叉。
"""
import os
import re
import threading
import time
from datetime import datetime

from brain import Brain, BrainError
from memory import MemoryStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_TAG = re.compile(r"<state\s*([^>]*?)\s*/?>\s*$", re.S)
ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

# 同一进程内所有实例共享一把推理锁：本机 LM Studio 并发会互相踢掉
_PROCESS_LOCK = threading.RLock()

# 只有本地推理服务需要全局串行；在线 API 各用户并发互不影响，不必互相等
LOCAL_PROVIDERS = {"lmstudio", "ollama"}


def human_time(ts):
    delta = max(0, time.time() - ts)
    if delta < 90:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    return f"{int(delta // 86400 // 30)} 个月前"


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
        self.name = cfg.get("name", "铃铛")
        self.session = session or f"s{int(time.time() * 1000)}"
        self.personaPath = personaPath or os.path.join(BASE_DIR, "persona.md")
        self.persona = self._load_persona()

        self.brain = Brain(cfg)
        # 本地推理服务并发会互相踢掉，所有实例共用一把锁；
        # 在线 API 每个用户一个独立锁，互不阻塞
        self.brain._lock = (
            _PROCESS_LOCK if cfg.get("provider") in LOCAL_PROVIDERS else threading.RLock()
        )

        self.dbPath = dbPath or os.path.join(cfg["dataDir"], "memory.db")
        self.store = MemoryStore(self.dbPath)

        self._turnLock = threading.Lock()
        self.notices = []
        self.noticeLock = threading.Lock()
        self.background = []
        self.turnsSinceSummary = 0
        self.gapText = "这是你们的第一次对话"

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
        return self.profile()

    def profile(self):
        stats = self.store.stats()
        return {
            "name": self.name,
            "provider": self.cfg.get("provider"),
            "model": self.cfg.get("model"),
            "session": self.session,
            "gap": self.gapText,
            "affinity": int(self.store.get_kv_float("affinity", 30)),
            "mood": self.store.get_kv("mood", "平静"),
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
        mood = self.store.get_kv("mood", "平静")
        firstSeen = self.store.get_kv_float("firstSeen", time.time())
        knownDays = int((time.time() - firstSeen) // 86400) + 1
        weekday = "一二三四五六日"[datetime.now().weekday()]

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
            f"- 时间：{datetime.now():%Y-%m-%d %H:%M}（周{weekday}）",
            f"- {self.gapText}",
            f"- 你们认识第 {knownDays} 天 ｜ 好感度 {int(affinity)}/100（{relation}）｜ 你此刻的心情：{mood}",
            "",
        ]

        if memories:
            lines.append("## 长期记忆（按与当前话题的相关度排序，是「你想起来的」）")
            for item in memories:
                lines.append(
                    f"- [{item['kind']}｜重要{item['importance']:.0f}｜{human_time(item['updatedAt'])}] {item['content']}"
                )
        else:
            lines.append("## 长期记忆\n- （还是一片空白，你对他几乎一无所知，别装熟）")

        if summaries:
            lines.append("")
            lines.append("## 更早的片段")
            for item in summaries:
                lines.append(f"- {item['content']}")

        lines.append("")
        lines.append(
            "## 提醒\n"
            "记忆可能过时或不完整，自然地用，不确定就问。"
            '回复结尾记得带 <state mood="..." affinity="..."/>。'
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
        match = STATE_TAG.search(rawText or "")
        if not match:
            return
        attrs = dict(ATTR.findall(match.group(1)))
        mood = attrs.get("mood")
        if mood:
            self.store.set_kv("mood", mood)
        try:
            delta = int(float(attrs.get("affinity", "0")))
        except ValueError:
            delta = 0
        if delta:
            affinity = self.store.get_kv_float("affinity", 30) + delta
            self.store.set_kv("affinity", max(0.0, min(100.0, affinity)))

    # ---------------------------------------------------------------- 对话
    def respond(self, userText):
        """非流式：返回 (完整回复, 元信息)。"""
        text = ""
        meta = {}
        for kind, payload in self.respond_iter(userText):
            if kind == "delta":
                text += payload
            else:
                meta = payload
        return text, meta

    def respond_iter(self, userText):
        """流式：yield ("delta", 文本) ... 最后 yield ("meta", 字典)。"""
        messages, usedMemories = self.build_messages(userText)
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

        # 标签只用于更新状态，不能进对话历史，否则下次会当作台词读到
        cleanReply = STATE_TAG.sub("", rawReply).strip()
        self.apply_state(rawReply)
        self.store.add_turn(self.session, "user", userText)
        self.store.add_turn(self.session, "assistant", cleanReply)
        self.remember_async(userText, cleanReply)
        self.maybe_summarize()

        profile = self.profile()
        yield (
            "meta",
            {
                "reply": cleanReply,
                "session": self.session,
                "mood": profile["mood"],
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
