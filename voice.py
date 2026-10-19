"""音色管理小工具：不用起服务也能换声音、克隆、试听。

    python voice.py status                         看当前用什么音色
    python voice.py list                           列出可选音色
    python voice.py use xiaoyi                     切音色（支持简写）
    python voice.py provider gptsovits            换后端（gptsovits/edge/siliconflow/openai）
    python voice.py rate +8% --pitch +2Hz          调语速与音调
    python voice.py clone 录音.wav --text "录音里说的原话" --provider gptsovits
    python voice.py say "主人，听得到吗" --play
"""
import argparse
import os
import sys

from config import load_config
from tts import (
    EDGE_ALIASES,
    TTS,
    TTS_PROVIDERS,
    TTS_PROVIDER_INFO,
    TTSError,
)


def buildEngine(user, provider=None):
    cfg = load_config()
    if provider:
        cfg["ttsProvider"] = provider
        preset = TTS_PROVIDERS[provider]
        cfg["ttsVoice"] = preset["voice"]
        cfg["ttsModel"] = preset["model"]
        cfg["ttsFormat"] = preset["format"]
    profileDir = os.path.join(cfg["dataDir"], "users", user, "voice")
    return TTS(cfg, profileDir)


def cmdStatus(args):
    engine = buildEngine(args.user)
    data = engine.profile.public()
    info = TTS_PROVIDER_INFO.get(engine.provider, {})
    print(f"后端      {engine.provider} · {info.get('label', '')}")
    print(f"音色      {data.get('voice') or '（未设置）'}")
    print(f"模型      {data.get('model') or '（无）'}")
    print(f"语速音调  rate={data.get('rate')} pitch={data.get('pitch')} speed={data.get('speed')}")
    print(f"参考音频  {engine.profile.refPath() or '（无）'}")
    if engine.provider == "gptsovits":
        baseUrl = engine.sovitsBaseUrl()
        if engine.sovitsHealth():
            print(f"推理服务  就绪 · {baseUrl}")
        else:
            print(f"推理服务  连不上（{baseUrl}），请确认远端 api_v2 已启动、端口已放通")
        print(f"角色参考  {engine.cfg.get('sovitsRefAudio') or '（未配置）'}")
    print(f"克隆音色  {data.get('customVoiceUri') or '（无）'}")
    print(f"密钥      {'已配置' if engine.apiKey() else '未配置'}")
    print(f"目录      {engine.profile.dir}")


def cmdList(args):
    engine = buildEngine(args.user, args.provider)
    info = TTS_PROVIDER_INFO.get(engine.provider, {})
    print(f"当前后端：{engine.provider} · {info.get('label', '')}")
    for item in engine.listVoices():
        mark = " *" if item["id"] == engine.profile.voice else "  "
        print(f"{mark} {item['id']:<38} {item['desc']}")
    print("\n * 表示当前音色")


def cmdUse(args):
    engine = buildEngine(args.user, args.provider)
    voice = args.voice
    if engine.provider == "edge":
        voice = EDGE_ALIASES.get(voice.lower(), voice)
    engine.profile.save({"voice": voice})
    print(f"已切换：{engine.provider} → {voice}")


def cmdProvider(args):
    if args.provider not in TTS_PROVIDERS:
        raise SystemExit(f"不支持的 provider：{args.provider}，可选 {', '.join(TTS_PROVIDERS)}")
    engine = buildEngine(args.user, args.provider)
    preset = TTS_PROVIDERS[args.provider]
    engine.profile.save(
        {
            "provider": args.provider,
            "voice": preset["voice"],
            "model": preset["model"],
            "format": preset["format"],
        }
    )
    info = TTS_PROVIDER_INFO.get(args.provider, {})
    print(f"已切换到 {args.provider} · {info.get('label', '')}，默认音色 {preset['voice'] or '参考音频'}")
    if info.get("needsKey") and not engine.apiKey():
        print(f"提示：这个后端需要密钥，设 CATGIRL_TTS_KEY 或 {preset['apiKeyEnv']}")


def cmdRate(args):
    patch = {}
    if args.rate:
        patch["rate"] = args.rate if args.rate.startswith(("+", "-")) else f"+{args.rate}"
    if args.pitch:
        patch["pitch"] = args.pitch if args.pitch.startswith(("+", "-")) else f"+{args.pitch}"
    if args.speed is not None:
        patch["speed"] = args.speed
    if not patch:
        raise SystemExit("至少给一个 --rate / --pitch / --speed")
    engine = buildEngine(args.user)
    engine.profile.save(patch)
    print("已更新：", patch)


def wavInfo(path):
    """读出 WAV 的时长/采样率；不是 WAV 就返回 None。"""
    import wave

    try:
        with wave.open(path) as handle:
            return {
                "seconds": round(handle.getnframes() / handle.getframerate(), 2),
                "rate": handle.getframerate(),
                "channels": handle.getnchannels(),
            }
    except Exception:
        return None


def cmdClone(args):
    provider = args.provider
    if not provider:
        provider = buildEngine(args.user).provider
    if provider == "edge":
        raise SystemExit("edge 只有固定音色库，不能克隆。请加 --provider siliconflow / gptsovits")
    if not os.path.exists(args.audio):
        raise SystemExit(f"找不到音频文件：{args.audio}")
    if len(args.text.strip()) < 2:
        raise SystemExit("必须用 --text 填上录音里逐字对应的原话")

    engine = buildEngine(args.user, provider)

    ext = os.path.splitext(args.audio)[1].lower() or ".wav"

    size = os.path.getsize(args.audio)
    if size < 16 * 1024:
        raise SystemExit("音频太短，建议 5~15 秒清晰人声")
    if size > 12 * 1024 * 1024:
        raise SystemExit("音频超过 12MB，先裁剪到 15 秒内")

    info = wavInfo(args.audio) if ext == ".wav" else None
    if provider == "gptsovits" and info:
        if info["seconds"] > 10:
            print(f"提示：参考音频 {info['seconds']} 秒，GPT-SoVITS 只取前 10 秒，建议先裁到 5~10 秒")
        elif info["seconds"] < 3:
            raise SystemExit("GPT-SoVITS 的参考音频太短，至少 3 秒，建议 5~10 秒")

    refName = f"ref{ext}"
    refPath = os.path.join(engine.profile.dir, refName)
    with open(args.audio, "rb") as src, open(refPath, "wb") as dst:
        dst.write(src.read())

    local = provider == "gptsovits"
    engine.profile.save(
        {
            "provider": provider,
            "refAudio": refName,
            "refText": args.text.strip(),
            "format": "wav" if local else "mp3",
            # 自建后端的「音色」就是这份参考音频，没有独立 voice id
            "voice": "reference" if local else engine.profile.voice,
        }
    )
    print(f"参考音频已保存：{refPath}" + (f"（{info['seconds']}s / {info['rate']}Hz）" if info else ""))

    if provider == "siliconflow":
        uri = engine.uploadReference(refPath, args.text.strip(), args.name)
        print(f"克隆完成，音色 ID：{uri}")
    else:
        print("参考音频已登记。把这份文件放到 GPT-SoVITS 那台机器上，路径填进 sovitsRefAudio 即可。")


def cmdSay(args):
    engine = buildEngine(args.user)
    try:
        result = engine.synthesize(args.text)
    except TTSError as exc:
        raise SystemExit(f"合成失败：{exc}")
    outPath = args.out or result["path"]
    if args.out:
        with open(result["path"], "rb") as src, open(outPath, "wb") as dst:
            dst.write(src.read())
    size = os.path.getsize(outPath)
    print(f"已生成：{outPath}（{size/1024:.1f} KB，{'命中缓存' if result['cached'] else '新合成'}）")
    if args.play:
        if sys.platform == "win32":
            os.startfile(outPath)  # noqa: S606 - 用户显式要求播放
        else:
            print("非 Windows 环境，请手动播放")


def main():
    parser = argparse.ArgumentParser(description="猫娘音色管理")
    parser.add_argument("--user", default="default", help="用户标识，默认 default")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看当前音色设置")

    p = sub.add_parser("list", help="列出可选音色")
    p.add_argument("--provider", help="临时指定后端")

    p = sub.add_parser("use", help="切换音色")
    p.add_argument("voice")
    p.add_argument("--provider", help="同时切换后端")

    p = sub.add_parser("provider", help="切换 TTS 后端")
    p.add_argument("provider")

    p = sub.add_parser("rate", help="调整语速 / 音调")
    p.add_argument("--rate", help='例如 "+8%%"')
    p.add_argument("--pitch", help='例如 "+2Hz"')
    p.add_argument("--speed", type=float, help="0.5~2.0，云端后端用")

    p = sub.add_parser("clone", help="用参考音频克隆音色")
    p.add_argument("audio")
    p.add_argument("--text", required=True, help="参考音频里逐字对应的原文")
    p.add_argument(
        "--provider",
        choices=["siliconflow", "gptsovits"],
        help="克隆后端（默认沿用当前后端）",
    )
    p.add_argument("--name", default="", help="云端音色名称")

    p = sub.add_parser("say", help="合成一句话")
    p.add_argument("text")
    p.add_argument("-o", "--out", help="另存为")
    p.add_argument("--play", action="store_true", help="合成后直接播放（Windows）")

    args = parser.parse_args()
    handlers = {
        "status": cmdStatus,
        "list": cmdList,
        "use": cmdUse,
        "provider": cmdProvider,
        "rate": cmdRate,
        "clone": cmdClone,
        "say": cmdSay,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
