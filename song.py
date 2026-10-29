"""曲库：挑一首歌，剪出 30 秒左右的一整段发出去。

下刀位置决定会不会「唱半句」。做法是两步：
  1. ffmpeg silencedetect 找真正的静音；
  2. 再把音频降采样算能量包络，取局部极小值当换气口。
广播级流行歌几乎没有真静音，只靠第 1 步会整首只认出一句，所以第 2 步是主力。
起点和终点都取换气口，再在两端加 100~400ms 淡入淡出，听起来就是完整的一段。

曲库目录默认 /data/songs（跟着数据卷，主人自己往里丢文件就行）：
  - 支持 mp3 / wav / flac / m4a / ogg，歌名默认取文件名
  - 想要别名（比如「小星星」指向「星愿」），在该目录放 index.json：
        {"星愿.mp3": {"title": "星愿", "aliases": ["小星星", "星"]}}
"""

import array
import difflib
import hashlib
import json
import math
import os
import re
import subprocess

try:  # audioop 在 Python 3.13 被移除，3.12 仍自带（算能量快得多）
    import audioop
except ImportError:  # pragma: no cover
    audioop = None

AUDIO_EXT = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus")

# 静音判定：低于 -33dB 且持续 0.3 秒以上算句子之间的间隙
SILENCE_NOISE = "-33dB"
SILENCE_MIN = 0.30

# 广播级流行歌几乎没有真正的静音，光靠 silencedetect 会整首只切出一句。
# 所以再算一层能量包络：把音频降采样成 8kHz 单声道，按 50ms 一帧求 RMS，
# 局部极小值就是换气口，在那里下刀不会切到字中间。
ENV_RATE = 8000
ENV_FRAME = 400          # 400 采样 = 50ms
ENV_WINDOW = 6           # 极小值判定窗口（±6 帧 = ±300ms）
ENV_DIP = 0.65           # 低于中位能量 65% 才算谷底


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
        """按歌名/别名找歌；找不到返回 None。

        匹配要收着点：点歌文本里常带「唱首」「来一段」这些词，用宽松包含会把
        「不存在的一首歌」误判成曲库里那首〈歌〉。所以单字标题只认完全一致，
        标题出现在点歌文本里时还要求它至少占一半长度。
        """
        key = _norm(query)
        if not key:
            return None
        items = self.entries()

        def namesOf(item):
            return [_norm(item["title"])] + [_norm(a) for a in item["aliases"]]

        for item in items:  # 完全一致
            if key in [n for n in namesOf(item) if n]:
                return item
        for item in items:  # 点歌文本是标题的一部分：「春庭」->「春庭雪」
            if any(len(n) >= 2 and n.startswith(key) for n in namesOf(item) if n):
                return item
        for item in items:  # 标题出现在点歌文本里：「唱个追光使者」->「追光使者」
            if any(len(n) >= 2 and n in key and len(n) * 2 >= len(key) for n in namesOf(item) if n):
                return item
        # 最后按相似度兜底，容忍错别字（短名字容易误配，门槛抬高）
        best, bestScore = None, 0.0
        for item in items:
            for n in namesOf(item):
                if len(n) < 2:
                    continue
                score = difflib.SequenceMatcher(None, key, n).ratio()
                if score > bestScore:
                    best, bestScore = item, score
        return best if bestScore >= 0.75 else None

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

    def _run_raw(self, args):
        """要拿二进制输出（PCM）时用这个，不能走 text=True。"""
        try:
            return subprocess.run(args, capture_output=True, timeout=180)
        except FileNotFoundError as exc:
            raise SongError("镜像里没有 ffmpeg，唱歌功能不可用") from exc
        except subprocess.TimeoutExpired as exc:
            raise SongError("ffmpeg 超时") from exc

    def _envelope(self, src):
        """把整首降成 50ms 一帧的能量，返回 [(时刻, 相对能量)]。"""
        proc = self._run_raw([
            "ffmpeg", "-v", "error", "-i", src,
            "-ac", "1", "-ar", str(ENV_RATE), "-f", "s16le", "-",
        ])
        if proc.returncode != 0 or not proc.stdout:
            return []
        samples = array.array("h")
        samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
        frames = []
        step = ENV_FRAME
        raw = memoryview(proc.stdout)
        for offset in range(0, len(samples) - step + 1, step):
            if audioop is not None:
                level = audioop.rms(raw[offset * 2: (offset + step) * 2], 2)
            else:  # 没有 audioop 时退回纯 Python，慢但结果一样
                level = math.sqrt(sum(v * v for v in samples[offset: offset + step]) / step)
            frames.append((offset / ENV_RATE, level))
        return frames

    def _cut_points(self, src, duration):
        """所有适合下刀的时刻：真静音 + 能量谷底，去重后按时间排序。"""
        points = []
        for begin, stop in self._silences(src):
            points.append((begin + stop) / 2)
        frames = self._envelope(src)
        if frames:
            levels = sorted(f[1] for f in frames)
            median = levels[len(levels) // 2] or 1.0
            for index in range(len(frames)):
                lo = max(0, index - ENV_WINDOW)
                hi = min(len(frames), index + ENV_WINDOW + 1)
                value = frames[index][1]
                if value <= min(f[1] for f in frames[lo:hi]) and value < median * ENV_DIP:
                    points.append(frames[index][0])
        points.sort()
        merged = []
        for point in points:  # 400ms 内的合成一个，取最早那个（谷底起点）
            if merged and point - merged[-1] < 0.4:
                continue
            merged.append(point)
        return [p for p in merged if 0.5 < p < duration - 0.5]

    def _window(self, src, duration, target):
        """挑一段窗口：起点和终点都落在换气口，长度贴近 target。"""
        if duration <= target + 1:
            return 0.0, duration  # 本来就短，整首放

        cuts = self._cut_points(src, duration)
        if not cuts:
            return 0.0, min(target, duration)

        # 起点避开前奏：优先 20 秒附近，歌短就按比例往前挪
        preferred = min(20.0, duration * 0.2)
        start = min(cuts, key=lambda t: abs(t - preferred))

        # 终点取离 start+target 最近的换气口，长度允许 27~38 秒
        tail = [t for t in cuts if t > start]
        if tail:
            best = min(tail, key=lambda t: abs((t - start) - target))
            length = best - start
            if target - 3 <= length <= target + 8:
                return start, length
            if length > target + 8:  # 换气口太稀，退一步硬切但也落在谷底
                return start, target

        # 起点太靠后（歌尾没有换气口了），退回从头找
        if start > 10 and duration - start < target + 3:
            return 0.0, min(target, duration)
        return start, min(target, duration - start)

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
