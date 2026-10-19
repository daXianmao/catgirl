"""配置中心：本地模型和在线大模型共用一套接口，靠 provider 切换。

优先级：环境变量 > 同目录 config.json > provider 预设默认值。
这样本地开发直接跑，上线时把密钥塞进环境变量就行，代码一行不用改。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 每个预设描述一家服务商。embed 为空表示这家没有 embedding 接口，
# 需要另外指定 CATGIRL_EMBED_* 参数，否则长期记忆会退化成纯重要度排序。
PROVIDERS = {
    "lmstudio": {
        "baseUrl": "http://127.0.0.1:1234/v1",
        "model": "qwen/qwen3.8-27b",
        "embedModel": "text-embedding-nomic-embed-text-v1.5",
        "apiKeyEnv": "",
        "jsonMode": "json_schema",
        "thinkingMode": "reasoning_effort",
        "timeout": 300,
    },
    "ollama": {
        "baseUrl": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "embedModel": "nomic-embed-text",
        "apiKeyEnv": "",
        "jsonMode": "json_schema",
        "thinkingMode": "none",
        "timeout": 300,
    },
    "openai": {
        "baseUrl": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "embedModel": "text-embedding-3-small",
        "apiKeyEnv": "OPENAI_API_KEY",
        "jsonMode": "json_schema",
        "thinkingMode": "none",
        "timeout": 120,
    },
    "deepseek": {
        "baseUrl": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "embedModel": "",
        "apiKeyEnv": "DEEPSEEK_API_KEY",
        "jsonMode": "json_object",
        "thinkingMode": "none",
        "timeout": 120,
    },
    "moonshot": {
        "baseUrl": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "embedModel": "",
        "apiKeyEnv": "MOONSHOT_API_KEY",
        "jsonMode": "json_object",
        "thinkingMode": "none",
        "timeout": 120,
    },
    "dashscope": {
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "embedModel": "text-embedding-v3",
        "apiKeyEnv": "DASHSCOPE_API_KEY",
        "jsonMode": "json_object",
        "thinkingMode": "enable_thinking",
        "timeout": 120,
    },
    "zhipu": {
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "embedModel": "embedding-3",
        "apiKeyEnv": "ZHIPU_API_KEY",
        "jsonMode": "json_object",
        "thinkingMode": "none",
        "timeout": 120,
    },
    "siliconflow": {
        "baseUrl": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "embedModel": "BAAI/bge-m3",
        "apiKeyEnv": "SILICONFLOW_API_KEY",
        "jsonMode": "json_object",
        "thinkingMode": "enable_thinking",
        "timeout": 120,
    },
    "openrouter": {
        "baseUrl": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "embedModel": "",
        "apiKeyEnv": "OPENROUTER_API_KEY",
        "jsonMode": "json_object",
        "thinkingMode": "none",
        "timeout": 120,
    },
}

DEFAULTS = {
    "name": "铃铛",
    "provider": "lmstudio",
    "temperature": 0.85,
    "maxTokens": 800,
    "topK": 8,
    "recentTurns": 12,
    "summaryEvery": 20,
    "dataDir": os.path.join(BASE_DIR, "data"),
    "disableThinking": True,
    "maxMemories": 400,
    "host": "0.0.0.0",
    "port": 8000,
    "serverApiKey": "",
    # 语音：本机 GPT-SoVITS 常驻服务（9880），角色音色固定在 sovitsRefAudio
    "ttsProvider": "gptsovits",
    "ttsVoice": "",
    "ttsModel": "",
    "ttsBaseUrl": "",
    "ttsApiKey": "",
    "ttsRate": "+0%",
    "ttsPitch": "+0Hz",
    "ttsSpeed": 1.0,
    "ttsFormat": "",
    "ttsRefText": "",
    "ttsAuto": False,
    # GPT-SoVITS 常驻推理服务：装在项目外，靠 HTTP 调用，要用时自动拉起。
    # 下面 python / script / 参考音频都没有默认值，得按自己机器上的安装位置填
    # （写进 config.json 或设 CATGIRL_SOVITS_* 环境变量）。
    "sovitsBaseUrl": "http://127.0.0.1:9880",
    "sovitsAutoStart": True,
    "sovitsPython": "",
    "sovitsScript": "",
    "sovitsConfig": "GPT_SoVITS/configs/tts_infer.yaml",
    "sovitsRefAudio": "",
    "sovitsRefText": "",
    "sovitsRefLang": "zh",
    # api_v2 运行时依赖的额外目录；留空则用系统环境里已有的
    "sovitsFfmpegDir": "",
    "sovitsNltkData": "",
    # QQ（OneBot v11 协议端）：默认关闭，开启后由 qq.py 单独跑一个进程
    "qqEnabled": False,
    "qqWsUrl": "ws://127.0.0.1:3001",
    "qqToken": "",
    "qqProxy": "",
    "qqSelfId": "",
    "qqGroups": "",
    "qqPrivate": True,
    "qqAtOnly": True,
    "qqRate": 1.5,
    "qqMaxChars": 400,
    "qqVoice": False,
    "qqNickname": True,
}


def _read_config_file():
    path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"config.json 读不了：{exc}")


def _env(name, default=None):
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config(overrides=None):
    """返回一份扁平配置字典，供 Brain / Core / Server 共用。

    provider 必须先由「参数 > 环境变量 > config.json > 默认」定下来，
    再去取对应的预设；否则 --provider deepseek 会拿到 lmstudio 的地址。
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v}
    fileCfg = _read_config_file()
    provider = (
        overrides.get("provider")
        or _env("CATGIRL_PROVIDER")
        or fileCfg.get("provider")
        or DEFAULTS["provider"]
    )
    if provider not in PROVIDERS:
        raise SystemExit(f"未知的 provider：{provider}，可选 {', '.join(PROVIDERS)}")

    cfg = dict(DEFAULTS)
    cfg.update(dict(PROVIDERS[provider]))

    # 语音预设也按 provider 取，但字段要加 tts 前缀，
    # 否则 baseUrl/model 会覆盖掉对话模型的同名键
    from tts import TTS_PROVIDERS  # 延迟导入，避免配置层被语音层反向依赖

    ttsProvider = (
        _env("CATGIRL_TTS_PROVIDER")
        or fileCfg.get("ttsProvider")
        or DEFAULTS["ttsProvider"]
    )
    if ttsProvider not in TTS_PROVIDERS:
        raise SystemExit(
            f"未知的 ttsProvider：{ttsProvider}，可选 {', '.join(TTS_PROVIDERS)}"
        )
    ttsPreset = TTS_PROVIDERS[ttsProvider]
    cfg["ttsBaseUrl"] = ttsPreset["baseUrl"]
    cfg["ttsModel"] = ttsPreset["model"]
    cfg["ttsVoice"] = ttsPreset["voice"]
    cfg["ttsFormat"] = ttsPreset["format"]
    cfg["ttsSpeed"] = ttsPreset["speed"]
    cfg["ttsProvider"] = ttsProvider

    cfg.update({k: v for k, v in fileCfg.items() if v is not None})
    cfg["provider"] = provider

    # 环境变量优先级最高，方便容器化部署时注入
    envMap = {
        "CATGIRL_NAME": "name",
        "CATGIRL_BASE_URL": "baseUrl",
        "CATGIRL_MODEL": "model",
        "CATGIRL_EMBED_MODEL": "embedModel",
        "CATGIRL_TEMP": "temperature",
        "CATGIRL_MAX_TOKENS": "maxTokens",
        "CATGIRL_TOP_K": "topK",
        "CATGIRL_RECENT_TURNS": "recentTurns",
        "CATGIRL_SUMMARY_EVERY": "summaryEvery",
        "CATGIRL_DATA": "dataDir",
        "CATGIRL_MAX_MEMORIES": "maxMemories",
        "CATGIRL_HOST": "host",
        "CATGIRL_PORT": "port",
        "CATGIRL_SERVER_KEY": "serverApiKey",
        "CATGIRL_JSON_MODE": "jsonMode",
        "CATGIRL_THINKING_MODE": "thinkingMode",
        "CATGIRL_TIMEOUT": "timeout",
        "CATGIRL_TTS_PROVIDER": "ttsProvider",
        "CATGIRL_TTS_VOICE": "ttsVoice",
        "CATGIRL_TTS_MODEL": "ttsModel",
        "CATGIRL_TTS_BASE_URL": "ttsBaseUrl",
        "CATGIRL_TTS_RATE": "ttsRate",
        "CATGIRL_TTS_PITCH": "ttsPitch",
        "CATGIRL_TTS_FORMAT": "ttsFormat",
        "CATGIRL_TTS_REF_TEXT": "ttsRefText",
        "CATGIRL_SOVITS_URL": "sovitsBaseUrl",
        "CATGIRL_SOVITS_PYTHON": "sovitsPython",
        "CATGIRL_SOVITS_SCRIPT": "sovitsScript",
        "CATGIRL_SOVITS_REF": "sovitsRefAudio",
        "CATGIRL_SOVITS_REF_TEXT": "sovitsRefText",
        "CATGIRL_SOVITS_FFMPEG": "sovitsFfmpegDir",
        "CATGIRL_SOVITS_NLTK": "sovitsNltkData",
        "CATGIRL_QQ_WS": "qqWsUrl",
        "CATGIRL_QQ_TOKEN": "qqToken",
        "CATGIRL_QQ_PROXY": "qqProxy",
        "CATGIRL_QQ_SELF_ID": "qqSelfId",
        "CATGIRL_QQ_GROUPS": "qqGroups",
        "CATGIRL_QQ_RATE": "qqRate",
        "CATGIRL_QQ_MAX_CHARS": "qqMaxChars",
    }
    for envName, key in envMap.items():
        value = _env(envName)
        if value is not None:
            cfg[key] = value

    cfg["disableThinking"] = _as_bool(_env("CATGIRL_DISABLE_THINKING", fileCfg.get("disableThinking")), True)
    cfg["ttsAuto"] = _as_bool(_env("CATGIRL_TTS_AUTO", fileCfg.get("ttsAuto")), False)
    cfg["sovitsAutoStart"] = _as_bool(_env("CATGIRL_SOVITS_AUTOSTART", fileCfg.get("sovitsAutoStart")), True)
    cfg["qqEnabled"] = _as_bool(_env("CATGIRL_QQ_ENABLED", fileCfg.get("qqEnabled")), False)
    cfg["qqPrivate"] = _as_bool(_env("CATGIRL_QQ_PRIVATE", fileCfg.get("qqPrivate")), True)
    cfg["qqAtOnly"] = _as_bool(_env("CATGIRL_QQ_AT_ONLY", fileCfg.get("qqAtOnly")), True)
    cfg["qqVoice"] = _as_bool(_env("CATGIRL_QQ_VOICE", fileCfg.get("qqVoice")), False)
    cfg["qqNickname"] = _as_bool(_env("CATGIRL_QQ_NICKNAME", fileCfg.get("qqNickname")), True)
    cfg["qqRate"] = float(cfg["qqRate"] or 1.5)
    cfg["qqMaxChars"] = int(cfg["qqMaxChars"] or 400)
    cfg["temperature"] = float(cfg["temperature"])
    cfg["maxTokens"] = int(cfg["maxTokens"])
    cfg["topK"] = int(cfg["topK"])
    cfg["recentTurns"] = int(cfg["recentTurns"])
    cfg["summaryEvery"] = int(cfg["summaryEvery"])
    cfg["maxMemories"] = int(cfg["maxMemories"])
    cfg["timeout"] = int(cfg["timeout"])
    cfg["port"] = int(cfg["port"])
    cfg["ttsSpeed"] = float(cfg["ttsSpeed"] or 1.0)

    # 对话密钥：优先 CATGIRL_API_KEY，其次按 provider 约定读对应环境变量
    apiKey = _env("CATGIRL_API_KEY")
    if not apiKey and cfg.get("apiKeyEnv"):
        apiKey = _env(cfg["apiKeyEnv"], "")
    cfg["apiKey"] = apiKey or fileCfg.get("apiKey", "")

    # 向量服务可以独立于对话服务（比如对话用 DeepSeek、向量用硅基流动的 bge-m3）
    cfg["embedBaseUrl"] = _env("CATGIRL_EMBED_BASE_URL", fileCfg.get("embedBaseUrl", cfg["baseUrl"]))
    cfg["embedApiKey"] = _env("CATGIRL_EMBED_API_KEY", fileCfg.get("embedApiKey", cfg["apiKey"]))

    # 语音密钥单独一套，避免和对话密钥混用（同一家也可能不同 key）
    cfg["ttsApiKey"] = _env("CATGIRL_TTS_KEY", fileCfg.get("ttsApiKey", ""))
    if not cfg["ttsApiKey"]:
        ttsKeyEnv = TTS_PROVIDERS.get(cfg["ttsProvider"], {}).get("apiKeyEnv")
        if ttsKeyEnv and _env(ttsKeyEnv):
            cfg["ttsApiKey"] = _env(ttsKeyEnv)

    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})

    cfg["needsKey"] = bool(PROVIDERS[provider].get("apiKeyEnv"))
    return cfg


def describe(cfg):
    """给日志和 /api/config 用，绝不回显密钥本身。"""
    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "embedModel": cfg["embedModel"] or None,
        "baseUrl": cfg["baseUrl"],
        "hasApiKey": bool(cfg.get("apiKey")),
        "embedBaseUrl": cfg["embedBaseUrl"],
        "hasEmbedApiKey": bool(cfg.get("embedApiKey")),
        "hasServerKey": bool(cfg.get("serverApiKey")),
        "tts": {
            "provider": cfg.get("ttsProvider"),
            "voice": cfg.get("ttsVoice") or None,
            "model": cfg.get("ttsModel") or None,
            "hasApiKey": bool(cfg.get("ttsApiKey")),
            "auto": bool(cfg.get("ttsAuto")),
        },
        "qq": {
            "enabled": bool(cfg.get("qqEnabled")),
            "wsUrl": cfg.get("qqWsUrl"),
            "groups": [g for g in str(cfg.get("qqGroups") or "").replace("，", ",").split(",") if g.strip()],
            "private": bool(cfg.get("qqPrivate")),
            "atOnly": bool(cfg.get("qqAtOnly")),
            "voice": bool(cfg.get("qqVoice")),
        },
    }
