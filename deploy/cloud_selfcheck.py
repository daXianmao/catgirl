"""云端版自检：不依赖网络，只验证配置与降级路径是否合理。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import PROVIDERS, DEFAULTS, load_config, describe
from tts import TTS, TTSError, TTS_PROVIDERS

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra else ""))
    if not cond:
        ok = False


cfg = load_config()

check("provider 默认在线服务商", cfg["provider"] == "dashscope", cfg["provider"])
check("不含本地推理预设", "lmstudio" not in PROVIDERS and "ollama" not in PROVIDERS,
      list(PROVIDERS))
check("语音默认 edge", cfg["ttsProvider"] == "edge", cfg["ttsProvider"])
check("无本地拉起开关", "sovitsAutoStart" not in cfg)
check("无本地 python 路径", "sovitsPython" not in cfg and "sovitsScript" not in cfg)
check("sovits 地址默认为空", cfg["sovitsBaseUrl"] == "", repr(cfg["sovitsBaseUrl"]))

print("--- describe ---")
import json
print(json.dumps(describe(cfg), ensure_ascii=False, indent=2))

# TTS 实例：edge 后端应能构建
tmpDir = tempfile.mkdtemp(prefix="catgirl-cloud-")
tts = TTS(cfg, tmpDir)
check("TTS 引擎构建", tts.provider == "edge", tts.provider)
check("音色有默认值", bool(tts.profile.voice), tts.profile.voice)

# gptsovits 未配地址时：探活应返回 False，合成应给出可读错误而不是崩栈
cfgSol = dict(cfg)
cfgSol["ttsProvider"] = "gptsovits"
cfgSol["sovitsBaseUrl"] = ""
ttsSol = TTS(cfgSol, tempfile.mkdtemp(prefix="catgirl-cloud-sol-"))
check("未配地址时 health=False", ttsSol.sovitsHealth() is False)
try:
    ttsSol.synthesize("测试", useCache=False)
    check("未配地址时报错", False, "竟然没报错")
except TTSError as exc:
    msg = str(exc)
    check("未配地址时报错可读", "CATGIRL_SOVITS_URL" in msg, msg)

print("\n结果：", "全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)
