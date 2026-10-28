"""配置中心（云端部署版）：模型一律通过在线 API 调用。

和本地版最大的区别：这里没有任何本地推理相关的东西 ——
不含 LM Studio / Ollama 预设，也不含本地语音服务的启动路径。
所有差异都靠环境变量注入，容器化部署时一行配置都不用改代码。

优先级：环境变量 > 同目录 config.json > provider 预设默认值。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 每个预设描述一家服务商。embed 为空表示这家没有 embedding 接口，
# 需要另外指定 CATGIRL_EMBED_* 参数，否则长期记忆会退化成纯重要度排序。
# 云端版只保留在线服务商；本地推理（LM Studio / Ollama）见 local 分支。
PROVIDERS = {
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
    "name": "铃洛",
    "provider": "dashscope",
    "temperature": 0.85,
    # 铃洛一条回复只要 40 字以内，400 留了足够余量（含 state 标签）。
    # 之前 800 太宽松，模型会顺着写成长段小说体。
    "maxTokens": 400,
    "topK": 8,
    "recentTurns": 12,
    "summaryEvery": 20,
    "dataDir": os.path.join(BASE_DIR, "data"),
    "disableThinking": True,
    "maxMemories": 400,
    "host": "0.0.0.0",
    "port": 8000,
    "serverApiKey": "",
    # 语音：云端版默认 Edge 在线音色（免费、免密钥、零部署），
    # 想克隆音色就切 siliconflow / openai，靠配置切换不用改代码
    "ttsProvider": "edge",
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
    # 可选：自建的 GPT-SoVITS HTTP 服务（跑在另一台机器上，这里只填地址）。
    # 云端版不会去启动它，连不上就报错，不会尝试拉起本地进程。
    "sovitsBaseUrl": "",
    "sovitsRefAudio": "",
    "sovitsRefText": "",
    "sovitsRefLang": "zh",
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
    # 语音发送策略：smart = 只在必要的时候发（对方点名要 / 语音对语音 / 特别开心）；
    # always = 旧行为，每条回复后面都跟一条语音
    "qqVoiceMode": "smart",
    # 语音文件的投递方式：auto = 按协议端位置自动选（协议端不在本机就走 base64）；
    # base64 = 强制把音频塞进消息里（协议端在别的机器时必须用它）；
    # file = 强制给本地路径（协议端和机器人共用一个卷时才有效）
    "qqVoiceUri": "auto",
    "qqNickname": True,
    "ownerQq": "",
    # 主动搭话：没人跟她说话的时候，她自己掷骰子决定要不要先开口。
    # 只在私聊触发，且必须同时满足「安静够久 + 不在静默时段 + 今天没超次数 + 没有连续被晾着」。
    # 掷骰子（random.random）不花 token，只有真开口那一轮才走模型。
    "qqSpeakUp": False,
    "qqSpeakUpChance": 0.15,     # 每轮掷骰子的命中概率
    "qqSpeakUpEvery": 1800,      # 多久掷一次（秒）
    "qqSpeakUpIdle": 3600,       # 上一次对话结束多久之后才允许开口（秒）
    "qqSpeakUpDaily": 4,         # 每天最多主动开口几次
    "qqSpeakUpQuiet": "1-8",     # 静默时段（小时，左闭右开；写 23-8 表示跨午夜）
    "qqSpeakUpTarget": "",       # 对谁搭话；留空 = 主人（ownerQq）
    # 心情：愉悦(v)/激动(a) 两个数，随时间回落到基线。
    # 半衰期单位是分钟 —— 180 分钟 = 三小时后这份情绪只剩一半浓度。
    "moodHalfLife": 180,
    "moodInertia": 0.6,         # 新情绪替换旧情绪的力度，0=完全不改，1=完全覆盖
    # 图片识别：用多模态模型把 QQ 图片转成一句描述再过给主模型。
    # 留空 = 关闭（只发「［图片］」占位符）。baseUrl / apiKey 留空则复用主配置。
    "visionModel": "qwen3.7-plus",
    "visionBaseUrl": "",
    "visionApiKey": "",
    "visionMaxImages": 3,
    # 语音识别：把 QQ 语音消息转成文字，走 dashscope 的 qwen3-asr-flash。
    # 留空 = 关闭（只发「［语音］」占位符）。baseUrl / apiKey 留空则复用主配置。
    "asrModel": "qwen3-asr-flash",
    "asrBaseUrl": "",
    "asrApiKey": "",
    # 手机桥接（真机方案）：安卓手机上跑官方 QQ 客户端 + 通知监听脚本，
    # 由手机主动连过来，本进程当服务端。默认关闭，且默认只监听回环，
    # 要跨机连（如 Tailscale 内网）才把 host 改成 0.0.0.0 或内网 IP。
    "phoneEnabled": False,
    "phoneHost": "127.0.0.1",
    "phonePort": 8765,
    "phoneToken": "",
    "phonePrivate": True,
    "phoneGroups": "",
    # 通知里拿不到 QQ 号，只能靠昵称认人。填主人的 QQ 昵称，
    # 命中就当成主人在说话（会加上「主人（昵称）：」前缀，语气/记忆都按主人走）
    "phoneOwnerName": "",
    # 群消息里没有 @ 信息，只能靠「文本里提到她的名字」判断是否在叫她
    "phoneNameTrigger": True,
    "phoneRate": 1.5,
    "phoneMaxChars": 400,
    # QQ 官方机器人（开放平台 OpenAPI v2）：不走逆向协议，零风控。
    # 代价是群里必须 @ 才收得到消息，且只能被动回复（5 分钟内、同一消息最多 5 条）。
    "qqbotEnabled": False,
    "qqbotAppId": "",
    "qqbotSecret": "",
    # 机器人没上线时只有沙箱环境可用，所以默认先沙箱
    "qqbotSandbox": False,
    # 官方给的是 openid 不是 QQ 号，第一次对话从日志里抄进来
    "qqbotOwnerOpenid": "",
    "qqbotRate": 1.2,
    "qqbotMaxChars": 400,
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
        "CATGIRL_SOVITS_REF": "sovitsRefAudio",
        "CATGIRL_SOVITS_REF_TEXT": "sovitsRefText",
        "CATGIRL_SOVITS_REF_LANG": "sovitsRefLang",
        # 留空＝按文本长度自动选切分方式（60 字以内整段、以上四句一切）
        "CATGIRL_SOVITS_CUT": "sovitsCut",
        "CATGIRL_SOVITS_TEMP": "sovitsTemperature",
        "CATGIRL_SOVITS_TOPK": "sovitsTopK",
        "CATGIRL_QQ_WS": "qqWsUrl",
        "CATGIRL_QQ_TOKEN": "qqToken",
        "CATGIRL_QQ_PROXY": "qqProxy",
        "CATGIRL_QQ_SELF_ID": "qqSelfId",
        "CATGIRL_QQ_GROUPS": "qqGroups",
        "CATGIRL_QQ_RATE": "qqRate",
        "CATGIRL_QQ_MAX_CHARS": "qqMaxChars",
        "CATGIRL_QQ_VOICE_MODE": "qqVoiceMode",
        "CATGIRL_QQ_VOICE_URI": "qqVoiceUri",
        "CATGIRL_QQ_SPEAK_UP_CHANCE": "qqSpeakUpChance",
        "CATGIRL_QQ_SPEAK_UP_EVERY": "qqSpeakUpEvery",
        "CATGIRL_QQ_SPEAK_UP_IDLE": "qqSpeakUpIdle",
        "CATGIRL_QQ_SPEAK_UP_DAILY": "qqSpeakUpDaily",
        "CATGIRL_QQ_SPEAK_UP_QUIET": "qqSpeakUpQuiet",
        "CATGIRL_QQ_SPEAK_UP_TARGET": "qqSpeakUpTarget",
        "CATGIRL_MOOD_HALF_LIFE": "moodHalfLife",
        "CATGIRL_MOOD_INERTIA": "moodInertia",
        "CATGIRL_OWNER_QQ": "ownerQq",
        "CATGIRL_VISION_MODEL": "visionModel",
        "CATGIRL_VISION_BASE_URL": "visionBaseUrl",
        "CATGIRL_VISION_API_KEY": "visionApiKey",
        "CATGIRL_ASR_MODEL": "asrModel",
        "CATGIRL_ASR_BASE_URL": "asrBaseUrl",
        "CATGIRL_ASR_API_KEY": "asrApiKey",
        "CATGIRL_PHONE_HOST": "phoneHost",
        "CATGIRL_PHONE_PORT": "phonePort",
        "CATGIRL_PHONE_TOKEN": "phoneToken",
        "CATGIRL_PHONE_GROUPS": "phoneGroups",
        "CATGIRL_PHONE_OWNER_NAME": "phoneOwnerName",
        "CATGIRL_PHONE_RATE": "phoneRate",
        "CATGIRL_PHONE_MAX_CHARS": "phoneMaxChars",
        "CATGIRL_QQBOT_APP_ID": "qqbotAppId",
        "CATGIRL_QQBOT_SECRET": "qqbotSecret",
        "CATGIRL_QQBOT_OWNER_OPENID": "qqbotOwnerOpenid",
        "CATGIRL_QQBOT_RATE": "qqbotRate",
        "CATGIRL_QQBOT_MAX_CHARS": "qqbotMaxChars",
    }
    for envName, key in envMap.items():
        value = _env(envName)
        if value is not None:
            cfg[key] = value

    cfg["disableThinking"] = _as_bool(_env("CATGIRL_DISABLE_THINKING", fileCfg.get("disableThinking")), True)
    cfg["ttsAuto"] = _as_bool(_env("CATGIRL_TTS_AUTO", fileCfg.get("ttsAuto")), False)
    cfg["qqEnabled"] = _as_bool(_env("CATGIRL_QQ_ENABLED", fileCfg.get("qqEnabled")), False)
    cfg["qqPrivate"] = _as_bool(_env("CATGIRL_QQ_PRIVATE", fileCfg.get("qqPrivate")), True)
    cfg["qqAtOnly"] = _as_bool(_env("CATGIRL_QQ_AT_ONLY", fileCfg.get("qqAtOnly")), True)
    cfg["qqVoice"] = _as_bool(_env("CATGIRL_QQ_VOICE", fileCfg.get("qqVoice")), False)
    cfg["qqNickname"] = _as_bool(_env("CATGIRL_QQ_NICKNAME", fileCfg.get("qqNickname")), True)
    cfg["qqRate"] = float(cfg["qqRate"] or 1.5)
    cfg["qqMaxChars"] = int(cfg["qqMaxChars"] or 400)
    cfg["qqSpeakUp"] = _as_bool(_env("CATGIRL_QQ_SPEAK_UP", fileCfg.get("qqSpeakUp")), False)
    cfg["qqSpeakUpChance"] = max(0.0, min(1.0, float(cfg["qqSpeakUpChance"] or 0.15)))
    cfg["qqSpeakUpEvery"] = max(60, int(cfg["qqSpeakUpEvery"] or 1800))
    cfg["qqSpeakUpIdle"] = max(300, int(cfg["qqSpeakUpIdle"] or 3600))
    cfg["qqSpeakUpDaily"] = max(0, int(cfg["qqSpeakUpDaily"] or 4))
    cfg["moodHalfLife"] = max(1.0, float(cfg["moodHalfLife"] or 180))
    cfg["moodInertia"] = max(0.0, min(1.0, float(cfg["moodInertia"] or 0.6)))
    cfg["phoneEnabled"] = _as_bool(_env("CATGIRL_PHONE_ENABLED", fileCfg.get("phoneEnabled")), False)
    cfg["phonePrivate"] = _as_bool(_env("CATGIRL_PHONE_PRIVATE", fileCfg.get("phonePrivate")), True)
    cfg["phoneNameTrigger"] = _as_bool(_env("CATGIRL_PHONE_NAME_TRIGGER", fileCfg.get("phoneNameTrigger")), True)
    cfg["phonePort"] = int(cfg["phonePort"] or 8765)
    cfg["phoneRate"] = float(cfg["phoneRate"] or 1.5)
    cfg["phoneMaxChars"] = int(cfg["phoneMaxChars"] or 400)
    cfg["qqbotEnabled"] = _as_bool(_env("CATGIRL_QQBOT_ENABLED", fileCfg.get("qqbotEnabled")), False)
    cfg["qqbotSandbox"] = _as_bool(_env("CATGIRL_QQBOT_SANDBOX", fileCfg.get("qqbotSandbox")), False)
    cfg["qqbotRate"] = float(cfg["qqbotRate"] or 1.2)
    cfg["qqbotMaxChars"] = int(cfg["qqbotMaxChars"] or 400)
    cfg["visionMaxImages"] = max(0, int(cfg.get("visionMaxImages") or 0))
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
            "speakUp": bool(cfg.get("qqSpeakUp")),
            "speakUpEvery": cfg.get("qqSpeakUpEvery"),
            "speakUpQuiet": cfg.get("qqSpeakUpQuiet"),
        },
        "phone": {
            "enabled": bool(cfg.get("phoneEnabled")),
            "addr": f"{cfg.get('phoneHost')}:{cfg.get('phonePort')}",
            "hasToken": bool(cfg.get("phoneToken")),
            "private": bool(cfg.get("phonePrivate")),
            "ownerName": cfg.get("phoneOwnerName") or "",
            "groups": [g for g in str(cfg.get("phoneGroups") or "").replace("，", ",").split(",") if g.strip()],
        },
        "qqbot": {
            "enabled": bool(cfg.get("qqbotEnabled")),
            "appId": cfg.get("qqbotAppId") or "",
            "hasSecret": bool(cfg.get("qqbotSecret")),
            "sandbox": bool(cfg.get("qqbotSandbox")),
        },
    }
