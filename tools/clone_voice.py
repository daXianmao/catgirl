#!/usr/bin/env python3
"""百炼声音复刻 / 声音设计小工具。

复刻（有录音）：给 10~20 秒干净的说话音频，生成一个 voice id。
设计（没录音）：给一段文字描述音色，也能生成 voice id。

用法：

    # 看看账号里已经有哪些音色
    python clone_voice.py --list

    # 用本地音频复刻（自动转 base64，不需要 OSS）
    python clone_voice.py --audio ref.wav --model cosyvoice-v3-flash --prefix tianyi

    # 不想要录音，用文字描述造一个音色
    python clone_voice.py --design "十六岁少女，声音清亮柔和，说话活泼有精神" \
        --preview-text "你好呀，今天也要开心哦。" --model cosyvoice-v3-flash --prefix tianyi

    # 顺手合成一段样音试听
    python clone_voice.py --audio ref.wav --prefix tianyi --preview "你好，我是洛天依。"

密钥优先取 --key，其次 CATGIRL_TTS_KEY，最后 DASHSCOPE_API_KEY。
音色和模型是绑死的：v3 的音色只能配 v3，v3.5 的音色只能配 v3.5。
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://dashscope.aliyuncs.com/api/v1"
MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


def _key(args):
    for candidate in (args.key, os.getenv("CATGIRL_TTS_KEY"), os.getenv("DASHSCOPE_API_KEY")):
        if candidate:
            return candidate.strip()
    sys.exit("没拿到密钥：传 --key，或设 CATGIRL_TTS_KEY / DASHSCOPE_API_KEY")


def _post(url, key, payload, timeout=90):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        sys.exit(f"接口返回 {exc.code}：{detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"连不上 {url}：{exc.reason}")


def _audio_uri(path):
    if not os.path.exists(path):
        sys.exit(f"找不到音频文件：{path}")
    size = os.path.getsize(path)
    if size > 10 * 1024 * 1024:
        sys.exit(f"音频 {size / 1048576:.1f} MB，超过 10 MB 上限，请先压缩")
    ext = os.path.splitext(path)[1].lower()
    if ext not in MIME:
        sys.exit(f"只支持 wav / mp3 / m4a，当前是 {ext or '未知格式'}")
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{MIME[ext]};base64,{encoded}"


def listVoices(base, key):
    data = _post(
        f"{base}/services/audio/tts/customization",
        key,
        {"model": "voice-enrollment", "input": {"action": "list_voice", "page_index": 0, "page_size": 50}},
    )
    voices = ((data or {}).get("output") or {}).get("voice_list") or []
    if not voices:
        print("账号下还没有自定义音色")
        return
    print(f"共 {len(voices)} 个：")
    for item in voices:
        print(f"  {item.get('voice_id')}  model={item.get('target_model')}  status={item.get('status')}")


def createVoice(base, key, model, prefix, audio=None, design=None, previewText=None):
    payloadInput = {"action": "create_voice", "target_model": model, "prefix": prefix}
    if design:
        payloadInput["voice_prompt"] = design
        payloadInput["preview_text"] = previewText or "你好，很高兴见到你。"
    else:
        payloadInput["audio"] = audio
    data = _post(f"{base}/services/audio/tts/customization", key, {"model": "voice-enrollment", "input": payloadInput})
    output = (data or {}).get("output") or {}
    voiceId = output.get("voice_id")
    if not voiceId:
        sys.exit(f"没拿到 voice id：{json.dumps(data, ensure_ascii=False)[:300]}")
    print(f"已提交，voice_id = {voiceId}")

    # 刚建好的音色要部署一会儿，轮询到 OK 才能用
    for _ in range(40):
        data = _post(
            f"{base}/services/audio/tts/customization",
            key,
            {"model": "voice-enrollment", "input": {"action": "query_voice", "voice_id": voiceId}},
        )
        output = (data or {}).get("output") or {}
        status = output.get("status")
        if status in ("OK", "DEPLOYED"):
            print("音色就绪")
            return voiceId
        if status not in ("DEPLOYING", "PENDING", None):
            sys.exit(f"音色状态异常：{status} {json.dumps(data, ensure_ascii=False)[:200]}")
        time.sleep(2)
    sys.exit("等待超时，音色还在部署，过会儿用 --list 看看状态")


def preview(base, key, model, voice, text, outPath):
    data = _post(
        f"{base}/services/audio/tts/SpeechSynthesizer",
        key,
        {"model": model, "input": {"text": text, "voice": voice, "format": "mp3", "sample_rate": 24000}},
    )
    audio = ((data or {}).get("output") or {}).get("audio") or {}
    if audio.get("data"):
        raw = base64.b64decode(audio["data"])
    elif audio.get("url"):
        with urllib.request.urlopen(audio["url"], timeout=90) as resp:
            raw = resp.read()
    else:
        sys.exit(f"没拿到音频：{json.dumps(data, ensure_ascii=False)[:300]}")
    with open(outPath, "wb") as handle:
        handle.write(raw)
    print(f"样音已保存：{outPath}（{len(raw) // 1024} KB）")


def main():
    parser = argparse.ArgumentParser(description="百炼声音复刻 / 声音设计")
    parser.add_argument("--audio", help="参考音频（wav/mp3/m4a，10~20 秒干净人声）")
    parser.add_argument("--design", help="用文字描述音色，代替录音")
    parser.add_argument("--preview-text", dest="previewText", help="声音设计时的试听文本")
    parser.add_argument("--prefix", default="voice", help="音色名前缀，方便区分来源")
    parser.add_argument("--model", default="cosyvoice-v3-flash", help="目标模型，复刻会绑定它")
    parser.add_argument("--base-url", dest="base", default=DEFAULT_BASE)
    parser.add_argument("--key", help="API Key，不传则读环境变量")
    parser.add_argument("--list", action="store_true", help="列出现有音色")
    parser.add_argument("--voice", help="用这个音色合成样音")
    parser.add_argument("--preview", help="样音文本，配合 --voice 使用")
    parser.add_argument("--out", default="preview.mp3", help="样音保存路径")
    args = parser.parse_args()

    key = _key(args)

    if args.list:
        listVoices(args.base, key)
        return

    if args.design:
        voiceId = createVoice(args.base, key, args.model, args.prefix, design=args.design, previewText=args.previewText)
    elif args.audio:
        voiceId = createVoice(args.base, key, args.model, args.prefix, audio=_audio_uri(args.audio))
    elif args.preview and args.voice:
        preview(args.base, key, args.model, args.voice, args.preview, args.out)
        return
    else:
        parser.print_help()
        return

    print()
    print("下一步：把这个 voice id 填进 .env 的 CATGIRL_TTS_VOICE，")
    print("同时把 CATGIRL_TTS_PROVIDER 设成 cosyvoice、CATGIRL_TTS_MODEL 设成同一个模型。")
    if args.preview:
        preview(args.base, key, args.model, voiceId, args.preview, args.out)


if __name__ == "__main__":
    main()
