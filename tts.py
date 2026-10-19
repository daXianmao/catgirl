"""TTS 层：预设音色 + 参考音频克隆。

四种后端，一套接口：

    gptsovits    本地 GPT-SoVITS api_v2 常驻服务，参考音频克隆，完全离线（本机首选）
    edge         微软 Edge 在线音色，免费、零部署，只能选音色不能克隆
    openai       任意 OpenAI 兼容的 /audio/speech（OpenAI、Fish、本地网关等）
    siliconflow  硅基流动 CosyVoice2，支持上传参考音频做零样本克隆（需实名）

音色配置分级：全局 config / 环境变量 < 用户目录下的 profile.json。
这样线上多用户可以各自换声音，而不用改服务端配置。

参考音频的硬性要求（各家都一样）：干净人声、无背景音乐、5~15 秒、
内容与你填的参考文本逐字一致。音质差一倍，克隆效果差十倍。
"""
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 只挑适合猫娘的几种，完整列表用 `edge-tts --list-voices` 看
EDGE_VOICES = {
    "zh-CN-XiaoyiNeural": "晓伊 · 少女感，活泼，猫娘首选",
    "zh-CN-XiaoxiaoNeural": "晓晓 · 温柔自然，最通用",
    "zh-CN-liaoning-XiaobeiNeural": "晓北 · 东北腔，接地气",
    "zh-CN-shaanxi-XiaoniNeural": "晓妮 · 陕西腔",
    "zh-TW-HsiaoChenNeural": "曉臻 · 台湾腔，软",
    "zh-HK-HiuMaanNeural": "曉曼 · 粤语女声",
    "zh-CN-YunxiNeural": "云希 · 少年音男声",
    "ja-JP-NanamiNeural": "七海 · 日语女声",
}

EDGE_ALIASES = {
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "xiaobei": "zh-CN-liaoning-XiaobeiNeural",
    "xiaoni": "zh-CN-shaanxi-XiaoniNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "nanami": "ja-JP-NanamiNeural",
}

# 硅基流动 CosyVoice2 的系统音色（前缀会自动补模型名）
SILICON_VOICES = {
    "claire": "温柔女声",
    "diana": "欢快女声",
    "anna": "沉稳女声",
    "bella": "激情女声",
    "alex": "沉稳男声",
    "benjamin": "低沉男声",
    "charles": "磁性男声",
    "david": "欢快男声",
}

OPENAI_VOICES = {
    "nova": "女声，清亮（推荐）",
    "shimmer": "女声，柔和",
    "coral": "女声，亲切",
    "sage": "女声，沉静",
    "alloy": "中性，均衡",
    "ash": "中性，偏低",
    "echo": "男声，稳重",
    "onyx": "男声，低沉",
}

TTS_PROVIDERS = {
    "edge": {
        "baseUrl": "",
        "model": "",
        "voice": "zh-CN-XiaoyiNeural",
        "format": "mp3",
        "speed": 1.0,
        "apiKeyEnv": "",
    },
    "siliconflow": {
        "baseUrl": "https://api.siliconflow.cn/v1",
        "model": "FunAudioLLM/CosyVoice2-0.5B",
        "voice": "FunAudioLLM/CosyVoice2-0.5B:claire",
        "format": "mp3",
        "speed": 1.0,
        "apiKeyEnv": "SILICONFLOW_API_KEY",
    },
    "openai": {
        "baseUrl": "https://api.openai.com/v1",
        "model": "gpt-4o-mini-tts",
        "voice": "nova",
        "format": "mp3",
        "speed": 1.0,
        "apiKeyEnv": "OPENAI_API_KEY",
    },
    "gptsovits": {
        "baseUrl": "http://127.0.0.1:9880",
        "model": "",
        "voice": "",
        "format": "wav",
        "speed": 1.0,
        "apiKeyEnv": "",
    },
    "none": {
        "baseUrl": "",
        "model": "",
        "voice": "",
        "format": "mp3",
        "speed": 1.0,
        "apiKeyEnv": "",
    },
}

MIME_BY_FORMAT = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
}

# 给前端和 CLI 看的说明，不含任何敏感信息
TTS_PROVIDER_INFO = {
    "edge": {
        "label": "微软 Edge 音色",
        "hint": "免费、零部署，能选音色但不能克隆",
        "clone": False,
        "needsKey": False,
        "offline": False,
    },
    "siliconflow": {
        "label": "硅基流动 CosyVoice2",
        "hint": "云端克隆，10 秒录音就够，需 API Key 与实名认证",
        "clone": True,
        "needsKey": True,
        "offline": False,
    },
    "gptsovits": {
        "label": "本地 GPT-SoVITS",
        "hint": "离线克隆，需要本机跑 api_v2.py，7~8GB 模型",
        "clone": True,
        "needsKey": False,
        "offline": True,
    },
    "openai": {
        "label": "OpenAI 兼容 TTS",
        "hint": "GPT-4o mini TTS 等，只有固定音色",
        "clone": False,
        "needsKey": True,
        "offline": False,
    },
    "none": {
        "label": "关闭语音",
        "hint": "只返回文字",
        "clone": False,
        "needsKey": False,
        "offline": True,
    },
}

MAX_CHARS = 3000


class TTSError(Exception):
    """TTS 相关的可读错误，上层直接展示给用户。"""


# ------------------------------------------------------------------ 文本清洗
_STATE_TAG = re.compile(r"<state\b[^>]*/?>")
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_MARKDOWN = re.compile(r"(\*\*|__|~~|`|^#{1,6}\s*|^>\s*)", re.M)
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D]"
)
_PAREN = re.compile(r"[（(][^）)]{0,20}[）)]")
_SPACES = re.compile(r"[ \t\u3000]+")


def clean_text(text):
    """把模型回复变成适合朗读的文本：去标签、去 markdown、去表情和舞台提示。"""
    text = _STATE_TAG.sub("", text or "")
    text = _CODE_BLOCK.sub("", text)
    text = _URL.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = _PAREN.sub("", text)
    text = _EMOJI.sub("", text)
    text = text.replace("~", "～").replace("...", "…")
    text = _SPACES.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()[:MAX_CHARS]


def split_text(text, limit=180):
    """按句切分，避免一次请求太长被服务端截断，也方便缓存复用。"""
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    chunks, buffer = [], ""
    for part in parts:
        if not part:
            continue
        if len(buffer) + len(part) > limit and buffer:
            chunks.append(buffer)
            buffer = part
        else:
            buffer += part
    if buffer:
        chunks.append(buffer)
    return chunks or [text]


def _multipart(fields, filePath, fieldName="file"):
    """标准库拼 multipart，省得为了上传一个文件引入 requests。"""
    boundary = f"----catgirl{uuid.uuid4().hex}"
    blocks = []
    for key, value in fields.items():
        blocks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    filename = os.path.basename(filePath)
    mime = mimetypes.guess_type(filename)[0] or "audio/mpeg"
    blocks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{fieldName}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    with open(filePath, "rb") as handle:
        blocks.append(handle.read())
    blocks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(blocks), f"multipart/form-data; boundary={boundary}"


def _proxy():
    """宿主给的透明代理只注入到命令行进程，这里显式读一次，Windows 本地也能走。"""
    return (
        os.getenv("CATGIRL_TTS_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or None
    )


def _open(url, payload=None, headers=None, method=None):
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        return urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = ""
        if exc.code == 401:
            hint = "（密钥无效或没传）"
        elif exc.code == 402:
            hint = "（余额不足）"
        elif exc.code == 403:
            hint = "（可能是没实名认证，克隆音色需要实名）"
        raise TTSError(f"TTS 服务返回 {exc.code}{hint}：{detail}") from None
    except urllib.error.URLError as exc:
        raise TTSError(f"连不上 TTS 服务 {url}：{exc.reason}") from None


# ------------------------------------------------------- 本地常驻语音服务（SoVITS）
# GPT-SoVITS 是个独立项目，自带环境和 torch，不在猫娘的 .venv 里，
# 所以只能通过 HTTP 找它；第一次用到时按 config 里填的路径顺手拉起来。
SOVITS_DEFAULT_URL = "http://127.0.0.1:9880"
SOVITS_DEFAULT_CONFIG = r"GPT_SoVITS/configs/tts_infer.yaml"
SOVITS_SPAWN_COOLDOWN = 30  # 秒：拉过就别反复拉，避免刷一堆进程
SOVITS_PORT_GRACE = 90      # 秒：要加载 1.4G 模型，端口迟迟不通才判死

_sovitsServiceLock = threading.Lock()
_sovitsLastSpawn = 0.0

# 本机回环地址绝对不能走代理：宿主的透明代理只认 HTTP 转发，127.0.0.1 会被挡成 502
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _localServiceUp(baseUrl, timeout=3.0):
    """api_v2 是 FastAPI，端口通了说明模型已加载完（加载在启动时同步完成）。"""
    try:
        with _LOCAL_OPENER.open(f"{baseUrl}/docs", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


# ------------------------------------------------------------------ 音色配置
class VoiceProfile:
    """一个用户的音色设置，落在 <用户目录>/voice/profile.json。"""

    def __init__(self, cfg, directory):
        self.cfg = cfg
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "profile.json")
        self.data = self._defaults()
        self.data.update(self._read())

    def _defaults(self):
        provider = self.cfg.get("ttsProvider", "edge")
        preset = TTS_PROVIDERS.get(provider, TTS_PROVIDERS["edge"])
        return {
            "provider": provider,
            "voice": self.cfg.get("ttsVoice") or preset["voice"],
            "model": self.cfg.get("ttsModel") or preset["model"],
            "rate": self.cfg.get("ttsRate", "+0%"),
            "pitch": self.cfg.get("ttsPitch", "+0Hz"),
            "speed": float(self.cfg.get("ttsSpeed", preset["speed"])),
            "format": self.cfg.get("ttsFormat") or preset["format"],
            "refText": self.cfg.get("ttsRefText", ""),
            "refAudio": "",
            "customVoiceUri": "",
            "customName": "",
            "updatedAt": 0,
        }

    def _read(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @property
    def provider(self):
        return self.data.get("provider") or "edge"

    @property
    def voice(self):
        return self.data.get("voice") or ""

    def refPath(self):
        name = self.data.get("refAudio")
        if not name:
            return None
        path = os.path.join(self.dir, name)
        return path if os.path.exists(path) else None

    def save(self, patch):
        if patch:
            self.data.update({k: v for k, v in patch.items() if v is not None})
        self.data["updatedAt"] = time.time()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return self.data

    def public(self):
        data = dict(self.data)
        data["refReady"] = bool(self.refPath())
        data["hasCustomVoice"] = bool(data.get("customVoiceUri"))
        return data


# ------------------------------------------------------------------ 合成引擎
class TTS:
    def __init__(self, cfg, profileDir):
        self.cfg = cfg
        self.profile = VoiceProfile(cfg, profileDir)
        self.cacheDir = os.path.join(profileDir, "cache")
        os.makedirs(self.cacheDir, exist_ok=True)
        self._lock = threading.Lock()

    # -------------------------------------------------------------- 对外接口
    @property
    def provider(self):
        return self.profile.provider

    def enabled(self):
        return self.provider != "none"

    def apiKey(self):
        if self.cfg.get("ttsApiKey"):
            return self.cfg["ttsApiKey"]
        preset = TTS_PROVIDERS.get(self.provider, {})
        envName = preset.get("apiKeyEnv")
        return os.getenv(envName, "") if envName else ""

    def synthesize(self, text, useCache=True):
        """返回 {path, mime, format, cached, provider, voice}。"""
        if not self.enabled():
            raise TTSError("语音没开启（ttsProvider=none）")

        spoken = clean_text(text)
        if not spoken:
            raise TTSError("没有可朗读的文字")

        fmt = (self.profile.data.get("format") or "mp3").lower()
        if self.provider == "gptsovits":
            fmt = "wav"  # api_v2 直接回 32kHz WAV
        key = self._cacheKey(spoken, fmt)
        outPath = os.path.join(self.cacheDir, f"{key}.{fmt}")

        if useCache and os.path.exists(outPath) and os.path.getsize(outPath) > 512:
            return {
                "path": outPath,
                "mime": MIME_BY_FORMAT.get(fmt, "audio/mpeg"),
                "format": fmt,
                "cached": True,
                "provider": self.provider,
                "voice": self.profile.voice,
            }

        with self._lock:  # 同一实例串行，避免并发写坏缓存
            if useCache and os.path.exists(outPath) and os.path.getsize(outPath) > 512:
                return {
                    "path": outPath,
                    "mime": MIME_BY_FORMAT.get(fmt, "audio/mpeg"),
                    "format": fmt,
                    "cached": True,
                    "provider": self.provider,
                    "voice": self.profile.voice,
                }
            tmpPath = outPath + f".{uuid.uuid4().hex[:6]}.part"
            try:
                if self.provider == "edge":
                    self._edge(spoken, tmpPath)
                elif self.provider == "gptsovits":
                    self._sovits(spoken, tmpPath)
                elif self.provider in ("siliconflow", "openai"):
                    self._openaiSpeech(spoken, tmpPath, fmt)
                else:
                    raise TTSError(f"不认识的 TTS provider：{self.provider}")
                if not os.path.exists(tmpPath) or os.path.getsize(tmpPath) < 200:
                    raise TTSError("TTS 返回的音频是空的")
                os.replace(tmpPath, outPath)
            except TTSError:
                raise
            except Exception as exc:
                raise TTSError(f"合成失败：{exc}") from None
            finally:
                if os.path.exists(tmpPath):
                    os.remove(tmpPath)

        return {
            "path": outPath,
            "mime": MIME_BY_FORMAT.get(fmt, "audio/mpeg"),
            "format": fmt,
            "cached": False,
            "provider": self.provider,
            "voice": self.profile.voice,
        }

    # ------------------------------------------------------------ SoVITS 服务
    def sovitsBaseUrl(self):
        return (
            self.cfg.get("sovitsBaseUrl")
            or self.cfg.get("ttsBaseUrl")
            or SOVITS_DEFAULT_URL
        ).rstrip("/")

    def sovitsHealth(self):
        return _localServiceUp(self.sovitsBaseUrl())

    def ensureSovitsService(self, wait=240):
        """确保推理服务可用：先探活，没起来就拉一个，然后等模型加载完。"""
        baseUrl = self.sovitsBaseUrl()
        if _localServiceUp(baseUrl):
            return True

        spawned = False
        autoStart = self.cfg.get("sovitsAutoStart")
        autoStart = True if autoStart is None else bool(autoStart)
        if not autoStart:
            raise TTSError(
                f"GPT-SoVITS 没在跑（{baseUrl}）。先把它的 api_v2.py 启动起来，"
                "或把 sovitsAutoStart 设成 true 让它自动拉起"
            )
        self._spawnSovits(baseUrl)
        spawned = True

        # 1.4G 模型加载 + 预热通常 30~60 秒
        deadline = time.time() + wait
        graceEnd = time.time() + SOVITS_PORT_GRACE if spawned else deadline
        while time.time() < deadline:
            if _localServiceUp(baseUrl):
                return True
            if spawned and time.time() > graceEnd:
                raise TTSError(
                    f"GPT-SoVITS 没起来（{baseUrl} 端口不通），"
                    "看它项目根目录下的 api_v2.log 里的报错"
                )
            time.sleep(2)
        raise TTSError(
            f"等了 {wait} 秒 GPT-SoVITS 还没就绪，看看它项目根目录下 api_v2.log 的报错"
        )

    def _spawnSovits(self, baseUrl):
        """后台拉起 api_v2。失败只记日志，交给上层轮询时报错。"""
        global _sovitsLastSpawn
        script = str(self.cfg.get("sovitsScript") or "").strip()
        python = str(self.cfg.get("sovitsPython") or "").strip()
        config = self.cfg.get("sovitsConfig") or SOVITS_DEFAULT_CONFIG

        if not script or not python:
            raise TTSError(
                "要自动拉起 GPT-SoVITS，得先告诉猫娘它装在哪：在 config.json 里填 "
                "sovitsPython（该环境的 python 解释器）和 sovitsScript（api_v2.py 的路径），"
                "或设环境变量 CATGIRL_SOVITS_PYTHON / CATGIRL_SOVITS_SCRIPT。"
                "不想自动拉起就把 sovitsAutoStart 设成 false，自己先把服务跑起来。"
            )
        if not os.path.exists(script) or not os.path.exists(python):
            raise TTSError(
                f"GPT-SoVITS 服务文件对不上：脚本 {script}，解释器 {python}。"
                "检查 sovitsScript / sovitsPython 是否指向真实存在的文件。"
            )

        with _sovitsServiceLock:
            if time.time() - _sovitsLastSpawn < SOVITS_SPAWN_COOLDOWN:
                return  # 刚拉过，正在加载，交给外层轮询
            _sovitsLastSpawn = time.time()

            parsed = urllib.parse.urlparse(baseUrl)
            port = parsed.port or 9880
            host = parsed.hostname or "127.0.0.1"
            command = [python, script, "-a", host, "-p", str(port), "-c", config]

            # api_v2 依赖 NLTK 数据与 ffmpeg；没配就沿用系统环境里已有的
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            nltkData = str(self.cfg.get("sovitsNltkData") or "").strip()
            if nltkData:
                env["NLTK_DATA"] = env.get("NLTK_DATA") or nltkData
            ffmpegDir = str(self.cfg.get("sovitsFfmpegDir") or "").strip()
            if ffmpegDir:
                env["PATH"] = (
                    ffmpegDir
                    + os.pathsep
                    + os.path.join(ffmpegDir, "bin")
                    + os.pathsep
                    + env.get("PATH", "")
                )

            flags = 0
            if os.name == "nt":
                # 脱离控制台，关掉猫娘不会顺手带走它
                flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            # api_v2.py 必须在 GPT-SoVITS 项目根启动（配置里用的是相对路径）
            projectRoot = os.path.dirname(os.path.abspath(script))
            logPath = os.path.join(os.path.dirname(projectRoot), "api_v2.log")
            try:
                logHandle = open(logPath, "ab")
            except OSError:
                logHandle = subprocess.DEVNULL
            try:
                subprocess.Popen(
                    command,
                    cwd=projectRoot,
                    stdin=subprocess.DEVNULL,
                    stdout=logHandle,
                    stderr=logHandle,
                    creationflags=flags,
                    env=env,
                    close_fds=True,
                )
            except Exception as exc:
                raise TTSError(f"拉不起 GPT-SoVITS 服务：{exc}") from None
            finally:
                if logHandle is not subprocess.DEVNULL:
                    logHandle.close()

    # -------------------------------------------------------------- 各家实现
    def _edge(self, text, outPath):
        try:
            import edge_tts
        except ImportError:
            raise TTSError("没装 edge-tts：pip install edge-tts") from None

        voice = self.profile.voice
        voice = EDGE_ALIASES.get(voice.lower(), voice)
        rate = self.profile.data.get("rate") or "+0%"
        pitch = self.profile.data.get("pitch") or "+0Hz"
        proxy = _proxy()
        chunks = split_text(text)

        async def render():
            for index, chunk in enumerate(chunks):
                kwargs = {"rate": rate, "pitch": pitch}
                if proxy:
                    try:
                        communicate = edge_tts.Communicate(chunk, voice, proxy=proxy, **kwargs)
                    except TypeError:  # 老版本不支持 proxy 参数
                        communicate = edge_tts.Communicate(chunk, voice, **kwargs)
                else:
                    communicate = edge_tts.Communicate(chunk, voice, **kwargs)
                await communicate.save(os.path.join(tmpDir, f"{index:03d}.mp3"))

        tmpDir = tempfile.mkdtemp(prefix="edge-")
        try:
            asyncio.run(render())
            data = b""
            for name in sorted(os.listdir(tmpDir)):
                with open(os.path.join(tmpDir, name), "rb") as handle:
                    data += handle.read()
            with open(outPath, "wb") as handle:
                handle.write(data)
        except Exception as exc:
            raise TTSError(
                f"edge-tts 合成失败：{exc}。如果机器要走代理，设 CATGIRL_TTS_PROXY=http://127.0.0.1:端口"
            ) from None
        finally:
            shutil.rmtree(tmpDir, ignore_errors=True)

    def _openaiSpeech(self, text, outPath, fmt):
        baseUrl = (self.cfg.get("ttsBaseUrl") or TTS_PROVIDERS[self.provider]["baseUrl"]).rstrip("/")
        key = self.apiKey()
        if not key:
            raise TTSError(
                f"{self.provider} 需要密钥：设 CATGIRL_TTS_KEY，或把 {TTS_PROVIDERS[self.provider]['apiKeyEnv']} 放进环境变量"
            )

        model = self.profile.data.get("model") or TTS_PROVIDERS[self.provider]["model"]
        voice = self.profile.voice
        if self.provider == "siliconflow" and voice and ":" not in voice and not voice.startswith("speech:"):
            voice = f"{model}:{voice}"

        payload = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": fmt,
        }
        speed = float(self.profile.data.get("speed") or 1.0)
        if abs(speed - 1.0) > 0.01:
            payload["speed"] = max(0.25, min(4.0, speed))

        headers = {"Authorization": f"Bearer {key}"}
        with _open(f"{baseUrl}/audio/speech", payload, headers) as resp:
            audio = resp.read()
        with open(outPath, "wb") as handle:
            handle.write(audio)

    def _sovits(self, text, outPath):
        """本地 GPT-SoVITS api_v2.py：GET /tts，参考音频即音色。

        参考音频优先用全局配置（角色固定音色），没配才退回用户上传的录音。
        """
        refPath = self.cfg.get("sovitsRefAudio") or self.profile.refPath()
        if not refPath or not os.path.exists(refPath):
            raise TTSError(
                "GPT-SoVITS 需要参考音频：在 config 里配 sovitsRefAudio，"
                "或跑 python voice.py clone 你的录音.wav --text '录音里说的话'"
            )
        refText = (self.cfg.get("sovitsRefText") or self.profile.data.get("refText") or "").strip()
        refLang = self.cfg.get("sovitsRefLang") or self.profile.data.get("refLang") or "zh"

        self.ensureSovitsService()

        baseUrl = self.sovitsBaseUrl()
        params = {
            "text": text,
            "text_lang": self.profile.data.get("textLang") or "zh",
            "ref_audio_path": os.path.abspath(refPath),
            "prompt_text": refText,
            "prompt_lang": refLang,
            "text_split_method": "cut5",
            "batch_size": "1",
            "media_type": "wav",
            "streaming_mode": "false",
        }
        speed = float(self.profile.data.get("speed") or 1.0)
        if abs(speed - 1.0) > 0.01:
            params["speed_factor"] = f"{max(0.5, min(2.0, speed)):.2f}"

        url = f"{baseUrl}/tts?{urllib.parse.urlencode(params)}"
        try:
            with _LOCAL_OPENER.open(url, timeout=180) as resp:
                audio = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise TTSError(f"GPT-SoVITS 返回 {exc.code}：{detail}") from None
        except urllib.error.URLError as exc:
            raise TTSError(
                f"连不上 GPT-SoVITS（{baseUrl}）：{exc.reason}。"
                "先把它的 api_v2.py 启动起来，或检查 sovitsBaseUrl 是否写对"
            ) from None
        with open(outPath, "wb") as handle:
            handle.write(audio)

    # -------------------------------------------------------------- 克隆上传
    def uploadReference(self, audioPath, refText, customName=None):
        """把参考音频注册成云端音色，返回可复用的 voice uri。

        只有硅基流动需要这个步骤；本地 F5 / GPT-SoVITS 直接把参考音频
        路径喂给推理接口就行，没有「上传」这一步。
        """
        if self.provider not in ("siliconflow",):
            raise TTSError(f"{self.provider} 不需要上传音色，本地克隆直接用参考文件即可")

        key = self.apiKey()
        if not key:
            raise TTSError("硅基流动需要密钥：设 CATGIRL_TTS_KEY 或 SILICONFLOW_API_KEY")

        baseUrl = (self.cfg.get("ttsBaseUrl") or TTS_PROVIDERS["siliconflow"]["baseUrl"]).rstrip("/")
        model = self.profile.data.get("model") or TTS_PROVIDERS["siliconflow"]["model"]
        name = customName or f"catgirl-{uuid.uuid4().hex[:8]}"
        fields = {"model": model, "customName": name, "text": refText}
        body, contentType = _multipart(fields, audioPath)
        request = urllib.request.Request(
            f"{baseUrl}/uploads/audio/voice",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": contentType},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            hint = "（克隆音色需要账号实名认证）" if exc.code in (401, 403) else ""
            raise TTSError(f"上传音色失败 {exc.code}{hint}：{detail}") from None
        except urllib.error.URLError as exc:
            raise TTSError(f"连不上硅基流动：{exc.reason}") from None

        uri = data.get("uri") or data.get("data", {}).get("uri", "")
        if not uri:
            raise TTSError(f"上传成功但没拿到音色 uri：{data}")
        self.profile.save(
            {
                "provider": "siliconflow",
                "customVoiceUri": uri,
                "customName": name,
                "voice": uri,
                "refText": refText,
            }
        )
        return uri

    # -------------------------------------------------------------- 其它
    def _cacheKey(self, text, fmt):
        raw = "|".join(
            [
                self.provider,
                self.profile.voice,
                str(self.profile.data.get("model")),
                str(self.profile.data.get("rate")),
                str(self.profile.data.get("pitch")),
                str(self.profile.data.get("speed")),
                str(self.profile.data.get("refAudio")),
                str(self.profile.data.get("refText")),
                str(self.cfg.get("sovitsRefAudio") or ""),
                text,
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


    def listVoices(self):
        """当前 provider 可选的音色。"""
        provider = self.provider
        if provider == "gptsovits":
            refAudio = self.cfg.get("sovitsRefAudio") or ""
            refText = (self.cfg.get("sovitsRefText") or "").strip()
            return [
                {
                    "id": "reference",
                    "desc": (
                        f"角色参考音频：{os.path.basename(refAudio)}"
                        + (f"（{refText[:12]}…）" if refText else "")
                        if refAudio
                        else "未配置参考音频（sovitsRefAudio）"
                    ),
                }
            ]
        if provider == "edge":
            return [{"id": key, "desc": value} for key, value in EDGE_VOICES.items()]
        if provider == "siliconflow":
            model = self.profile.data.get("model") or TTS_PROVIDERS["siliconflow"]["model"]
            items = [{"id": f"{model}:{key}", "desc": value} for key, value in SILICON_VOICES.items()]
            if self.profile.data.get("customVoiceUri"):
                items.insert(
                    0,
                    {
                        "id": self.profile.data["customVoiceUri"],
                        "desc": f"我的克隆音色（{self.profile.data.get('customName') or '未命名'}）",
                    },
                )
            return items
        if provider == "openai":
            return [{"id": key, "desc": value} for key, value in OPENAI_VOICES.items()]
        if provider == "gptsovits":
            ready = bool(self.profile.refPath())
            return [
                {
                    "id": "reference",
                    "desc": ("已就绪的参考音频" if ready else "还没上传参考音频"),
                }
            ]
        return []


def describe(cfg):
    """/api/config 用，不暴露密钥。"""
    provider = cfg.get("ttsProvider", "edge")
    return {
        "provider": provider,
        "voice": cfg.get("ttsVoice") or TTS_PROVIDERS.get(provider, {}).get("voice"),
        "model": cfg.get("ttsModel") or TTS_PROVIDERS.get(provider, {}).get("model"),
        "baseUrl": cfg.get("ttsBaseUrl") or TTS_PROVIDERS.get(provider, {}).get("baseUrl"),
        "hasApiKey": bool(cfg.get("ttsApiKey")),
        "auto": bool(cfg.get("ttsAuto")),
    }
