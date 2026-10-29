"""曲库：挑一首歌，剪出 30 秒左右的一整段发出去。

剪之前先用 ffmpeg 探测静音，把整首歌切成「一句一句」，再从某个句头开始
累加整句，落进 28~36 秒这个区间，最后在两句之间的静音里下刀。
这样发出去的片段一定是完整的句子，不会出现唱半句就被掐掉的情况。

曲库目录默认 /data/songs（跟着数据卷，主人自己往里丢文件就行）：
  - 支持 mp3 / wav / flac / m4a / ogg，歌名默认取文件名
  - 想要别名（比如「小星星」指向「星愿」），在该目录放 index.json：
        {"星愿.mp3": {"title": "星愿", "aliases": ["小星星", "星"]}}
"""

import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile

AUDIO_EXT = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus")

# 静音判定：低于 -33dB 且持续 0.3 秒以上算句子之间的间隙
SILENCE_NOISE = "-33dB"
SILENCE_MIN = 0.30

# 太短的「有声段」多半是呼吸或换气声，不算一句
MIN_PHRASE = 0.60


class SongError(Exception):
    """曲库或剪辑出错，调用方应该退回文字。"""


def _norm(text):
    """比对歌名用：去空白、去书名号、去标点，只留字和字母。"""
    return re.sub(r"[\s《》〈〉“”\"'「」『』·、，,.!！?？\-—_]+", "", str(text or "")).lower()


class SongLibrary:
    def __init__(self, dirs, cacheDir=None, clipSeconds=30):
        if isinstance(dirs, str):
            dirs = [dirs]
        self.dirs = [d for d in dirs if d]
        self.cacheDir = cacheDir or os.path.join(self.dirs[0] if self.dirs else "/tmp", ".cache")
        self.clipSeconds = float(clipSeconds or 30)
        self._entries = None

    # -------------------------------------------------------------- 目录
    def entries(self):
        if self._entries is None:
            self._entries = self._scan()
        return self._entries

    def refresh(self):
        self._entries = None
        return self.entries()

    def _scan(self):
        out = []
        for root in self.dirs:
            if not os.path.isdir(root):
                continue
            index = {}
            idxPath = os.path.join(root, "index.json")
            if os.path.exists(idxPath):
                try:
                    with open(idxPath, "r", encoding="utf-8") as handle:
                        index = json.load(handle) or {}
                except (OSError, ValueError):
                    index = {}
            for name in sorted(os.listdir(root)):
                if not name.lower().endswith(AUDIO_EXT):
                    continue
                path = os.path.join(root, name)
                meta = index.get(name) or {}
                title = meta.get("title") or os.path.splitext(name)[0].replace("_", " ").strip()
                aliases = [str(a) for a in (meta.get("aliases") or []) if str(a).strip()]
                out.append({"path": path, "file": name, "title": title, "aliases": aliases})
        return out

    def titles(self):
        return [item["title"] for item in self.entries()]

    def find(self, query):
        """按歌名/别名找歌；找不到返回 None。"""
        key = _norm(query)
        if not key:
            return None
        items = self.entries()
        for item in items:  # 完全一致
            if _norm(item["title"]) == key or key in [_norm(a) for a in item["aliases"]]:
                return item
        for item in items:  # 包含关系（「唱个星愿」能对上「星愿」）
            names = [_norm(item["title"])] + [_norm(a) for a in item["aliases"]]
            if any(key and (key in n or n in key) for n in names if n):
                return item
        # 最后按相似度兜底，容忍错别字
        best, bestScore = None, 0.0
        for item in items:
            for n in [_norm(item["title"])] + [_norm(a) for a in item["aliases"]]:
                if not n:
                    continue
                score = difflib.SequenceMatcher(None, key, n).ratio()
                if score > bestScore:
                    best, bestScore = item, score
        return best if bestScore >= 0.62 else None

    def pick(self):
        """没点歌时随机挑一首，优先没唱过的由调用方决定，这里只给第一首。"""
        items = self.entries()
        return items[0] if items else None

    # -------------------------------------------------------------- 剪辑
    def clip(self, song, target=None):
        """剪出完整句的一段，返回 mp3 路径（同参数走缓存）。"""
        target = float(target or self.clipSeconds)
        src = song["path"] if isinstance(song, dict) else str(song)
        if not os.path.exists(src):
            raise SongError(f"音频不存在：{src}")
        os.makedirs(self.cacheDir, exist_ok=True)

        stamp = f"{src}|{os.path.getmtime(src)}|{os.path.getsize(src)}|{target}"
        key = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:20]
        out = os.path.join(self.cacheDir, f"{key}.mp3")
        if os.path.exists(out) and os.path.getsize(out) > 2048:
            return out

        duration = self._duration(src)
        start, length = self._window(src, duration, target)
        self._cut(src, out, start, length)
        if not os.path.exists(out) or os.path.getsize(out) < 2048:
            raise SongError("裁剪失败")
        return out

    def _run(self, args):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=180)
        except FileNotFoundError as exc:
            raise SongError("镜像里没有 ffmpeg，唱歌功能不可用") from exc
        except subprocess.TimeoutExpired as exc:
            raise SongError("ffmpeg 超时") from exc

    def _duration(self, src):
        proc = self._run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", src,
        ])
        try:
            return float((proc.stdout or "").strip().splitlines()[0])
        except (ValueError, IndexError):
            raise SongError("读不出音频时长")

    def _silences(self, src):
        """返回 [(开始, 结束)] 的静音区间。"""
        proc = self._run([
            "ffmpeg", "-v", "info", "-nostats", "-i", src,
            "-af", f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_MIN}",
            "-f", "null", "-",
        ])
        text = (proc.stderr or "") + (proc.stdout or "")
        starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[\d.]+(?:e[+-]?\d+)?)", text)]
        ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[\d.]+(?:e[+-]?\d+)?)", text)]
        pairs = []
        for index, begin in enumerate(starts):
            stop = ends[index] if index < len(ends) else None
            if stop is None or stop <= begin:
                continue
            pairs.append((max(0.0, begin), stop))
        return pairs

    def _phrases(self, src, duration):
        """把有声段切成一句一句：静音之间的部分就是整句。"""
        phrases, cursor = [], 0.0
        for begin, stop in self._silences(src):
            if begin - cursor >= MIN_PHRASE:
                phrases.append((cursor, begin))
            cursor = max(cursor, stop)
        if duration - cursor >= MIN_PHRASE:
            phrases.append((cursor, duration))
        return phrases

    def _window(self, src, duration, target):
        """挑一段「整句」窗口：长度贴近 target，尽量不从最开头切。"""
        if duration <= target + 1:
            return 0.0, duration  # 本来就短，整首放

        phrases = self._phrases(src, duration)
        best = None
        for i, (start, _stop) in enumerate(phrases):
            for j in range(i, len(phrases)):
                length = phrases[j][1] - start
                if length < target - 2:  # 还不够长，再多加一句
                    continue
                if length > target + 6:  # 太长，这一组不合适
                    break
                # 从最开头起唱会连着前奏，扣一点分；其它按与目标的接近程度排
                score = abs(length - target) + (3.0 if start < 6 else 0.0)
                if best is None or score < best[0]:
                    best = (score, start, length)
                break

        if best:
            return best[1], best[2]

        # 探测不到静音（连续伴奏、纯电子曲）：退到在附近找最长静音下刀
        silences = self._silences(src)
        if silences:
            point = min(silences, key=lambda p: abs((p[0] + p[1]) / 2 - target))
            start = 0.0 if point[0] < 1.0 else max(0.0, point[0] - target)
            length = point[0] - start
            if length >= target - 4:
                return start, length
        return 0.0, min(target, duration)

    def _cut(self, src, out, start, length):
        fadeOut = max(0.2, min(0.6, length * 0.05))
        fadeAt = max(0.0, length - fadeOut)
        filters = (
            f"afade=t=in:st=0:d=0.12,"
            f"afade=t=out:st={fadeAt:.3f}:d={fadeOut:.3f}"
        )
        proc = self._run([
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src,
            "-af", filters, "-ac", "1", "-ar", "24000",
            "-codec:a", "libmp3lame", "-b:a", "64k", out,
        ])
        if proc.returncode != 0:
            # libmp3lame 缺失时退回系统默认编码器
            fallback = list(proc.args)
            idx = fallback.index("libmp3lame")
            fallback[idx] = "mp3"
            proc = self._run(fallback)
            if proc.returncode != 0:
                raise SongError((proc.stderr or "ffmpeg 裁剪失败").strip()[:200])
        return out
