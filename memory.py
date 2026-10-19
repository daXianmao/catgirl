"""长期记忆库：SQLite 存储 + 向量余弦检索 + 重要性/时间衰减混合排序。

设计参考 Stanford Generative Agents 的记忆打分方式：
    score = 0.65 * 相似度 + 0.25 * 重要性 + 0.10 * 时间新鲜度

为什么不用纯向量检索：纯相似度会让"三个月前的一件小事"和"昨天说的生日"
同等重要，人格会显得没有积累感。加上重要性和时间衰减后，重要的记忆留得久，
琐事自然淡出，更像真的记忆。
"""
import array
import math
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,              -- fact / preference / event / summary
    content     TEXT    NOT NULL,
    importance  REAL    NOT NULL DEFAULT 3,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    source      TEXT    NOT NULL DEFAULT '',
    vec         BLOB
);
CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session    TEXT NOT NULL,
    role       TEXT NOT NULL,                  -- user / assistant
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(updated_at);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turns(session, id);
"""

# 检索权重：相似度为主，重要性和新鲜度做微调
W_SIMILARITY = 0.65
W_IMPORTANCE = 0.25
W_RECENCY = 0.10

# 时间衰减半衰期（天）：30 天后新鲜度衰减到约 0.37
RECENCY_HALF_LIFE_DAYS = 30.0

# 极性词表：nomic-embed-text 这类英文向量模型对中文否定几乎不敏感
# （实测「主人讨厌香菜」和「主人喜欢香菜」余弦相似度 = 1.0），
# 所以合并前必须做一次极性校验，否则会把相反的意思吞成一条。
POLARITY_TOKENS = ("不", "没", "别", "无", "讨厌", "喜欢", "爱", "恨", "怕", "会", "能", "想", "要", "千万")


class MemoryStore:
    def __init__(self, dbPath):
        os.makedirs(os.path.dirname(os.path.abspath(dbPath)), exist_ok=True)
        self.dbPath = dbPath
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(dbPath, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _pack(vec):
        return array.array("f", vec).tobytes()

    @staticmethod
    def _unpack(blob):
        if not blob:
            return None
        arr = array.array("f")
        arr.frombytes(blob)
        return list(arr)

    @staticmethod
    def _cosine(a, b):
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / math.sqrt(na * nb)

    @staticmethod
    def _recency(updatedAt, now):
        ageDays = max(0.0, (now - updatedAt) / 86400.0)
        return math.exp(-ageDays / RECENCY_HALF_LIFE_DAYS)

    # ------------------------------------------------------------ 记忆写入/合并
    def add_memory(self, kind, content, importance=3.0, source="", vec=None, mergeThreshold=0.90):
        """写入一条记忆；与已有记忆高度相似时合并，避免同一件事反复堆叠。

        返回 (memoryId, merged)。merged=True 表示并入了旧记忆。
        """
        content = (content or "").strip()
        if not content:
            return None, False
        now = time.time()
        importance = max(1.0, min(5.0, float(importance)))

        with self._lock:
            if vec:
                dup = self._find_similar_locked(vec, mergeThreshold)
                # 极性不一致（喜欢/讨厌、会不会）一律不合并，宁可留两条也不能记反
                if dup and self._polarity(dup["content"]) != self._polarity(content):
                    dup = None
                if dup:
                    mergedContent = self._merge_text(dup["content"], content, dup["similarity"])
                    self._conn.execute(
                        "UPDATE memories SET content=?, importance=MAX(importance,?), updated_at=? WHERE id=?",
                        (mergedContent, importance, now, dup["id"]),
                    )
                    self._conn.commit()
                    return dup["id"], True

            cur = self._conn.execute(
                "INSERT INTO memories(kind,content,importance,created_at,updated_at,source,vec) "
                "VALUES(?,?,?,?,?,?,?)",
                (kind, content, importance, now, now, source, self._pack(vec) if vec else None),
            )
            self._conn.commit()
            return cur.lastrowid, False

    @staticmethod
    def _polarity(text):
        """极性签名：否定/正负向词不同就认为不是同一件事。"""
        return tuple(sorted(token for token in POLARITY_TOKENS if token in text))

    @staticmethod
    def _normalize(text):
        """归一化后再比较，避免「主人叫林澈。」和「主人的名字是林澈」被当成两件事。"""
        return "".join(ch for ch in text.lower() if ch.isalnum())

    @staticmethod
    def _merge_text(oldText, newText, similarity=0.0):
        """合并两条相似记忆。

        相似度很高（>=0.95）基本是同一句话的不同说法，保留信息量更大的那条；
        中等相似（0.90~0.95）说明有补充信息，拼接起来。
        """
        if MemoryStore._normalize(newText) == MemoryStore._normalize(oldText):
            return oldText
        if similarity >= 0.95:
            return newText if len(newText) > len(oldText) else oldText
        if newText in oldText:
            return oldText
        if oldText in newText:
            return newText
        return f"{oldText.rstrip('。')}；{newText}"

    def _find_similar_locked(self, vec, threshold):
        rows = self._conn.execute("SELECT id, content, vec FROM memories WHERE vec IS NOT NULL").fetchall()
        best = None
        bestSim = threshold
        for row in rows:
            sim = self._cosine(vec, self._unpack(row["vec"]))
            if sim >= bestSim:
                best = {"id": row["id"], "content": row["content"], "similarity": sim}
                bestSim = sim
        return best

    # ------------------------------------------------------------------ 检索
    def search(self, queryVec, topK=8, now=None):
        """混合打分检索，返回按分数降序的记忆列表。"""
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, content, importance, updated_at, hits, vec FROM memories"
            ).fetchall()

        scored = []
        for row in rows:
            vec = self._unpack(row["vec"])
            sim = self._cosine(queryVec, vec) if (vec and queryVec) else 0.0
            # 没有向量的记忆（向量服务失败时的兜底）只能靠重要性和新鲜度参与
            score = (
                W_SIMILARITY * sim
                + W_IMPORTANCE * (row["importance"] / 5.0)
                + W_RECENCY * self._recency(row["updated_at"], now)
            )
            if not vec:
                score *= 0.6
            scored.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "content": row["content"],
                    "importance": row["importance"],
                    "updatedAt": row["updated_at"],
                    "hits": row["hits"],
                    "similarity": sim,
                    "score": score,
                }
            )

        scored.sort(key=lambda item: item["score"], reverse=True)
        picked = scored[:topK]
        self._touch([item["id"] for item in picked])
        return picked

    def recent_summaries(self, limit=2):
        """取最近的会话摘要，作为跨会话背景常驻上下文。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, content, importance, updated_at FROM memories "
                "WHERE kind='summary' ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _touch(self, ids):
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._conn.execute(
                f"UPDATE memories SET hits = hits + 1 WHERE id IN ({placeholders})", ids
            )
            self._conn.commit()

    def list_memories(self, limit=30, kind=None):
        sql = "SELECT id, kind, content, importance, updated_at, hits FROM memories"
        params = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def forget(self, memoryId):
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id=?", (memoryId,))
            self._conn.commit()
            return cur.rowcount > 0

    def prune(self, maxCount=400):
        """超过容量时淘汰最不值钱的记忆：重要度低、旧、从没被想起过的先走。

        这是"遗忘"而不是"删除"——真正重要的事（importance 5）几乎不会被淘汰，
        因为打分里重要度占大头。
        """
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, importance, updated_at, hits FROM memories"
            ).fetchall()
            if len(rows) <= maxCount:
                return 0

            def score(row):
                return (
                    0.60 * (row["importance"] / 5.0)
                    + 0.30 * self._recency(row["updated_at"], now)
                    + 0.10 * min(row["hits"] / 10.0, 1.0)
                )

            ranked = sorted(rows, key=score)
            victims = [row["id"] for row in ranked[: len(rows) - maxCount]]
            placeholders = ",".join("?" for _ in victims)
            self._conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", victims)
            self._conn.commit()
            return len(victims)

    # -------------------------------------------------------------- 对话历史
    def add_turn(self, session, role, content):
        with self._lock:
            self._conn.execute(
                "INSERT INTO turns(session,role,content,created_at) VALUES(?,?,?,?)",
                (session, role, content, time.time()),
            )
            self._conn.commit()

    def recent_turns(self, session, limit=12):
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM turns WHERE session=? ORDER BY id DESC LIMIT ?",
                (session, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def turns_after_id(self, session, turnId, limit=40):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content FROM turns WHERE session=? AND id>? ORDER BY id ASC LIMIT ?",
                (session, turnId, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def max_turn_id(self, session):
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(id) AS mid FROM turns WHERE session=?", (session,)
            ).fetchone()
        return row["mid"] or 0

    def count_turns(self, session):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM turns WHERE session=?", (session,)
            ).fetchone()
        return row["n"]

    # ---------------------------------------------------------------- 键值对
    def get_kv(self, key, default=None):
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_kv(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)),
            )
            self._conn.commit()

    def get_kv_float(self, key, default=0.0):
        raw = self.get_kv(key)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def stats(self):
        with self._lock:
            memCount = self._conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
            turnCount = self._conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
        sizeKb = os.path.getsize(self.dbPath) / 1024 if os.path.exists(self.dbPath) else 0
        return {"memories": memCount, "turns": turnCount, "dbKb": round(sizeKb, 1)}
