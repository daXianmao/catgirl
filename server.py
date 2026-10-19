"""HTTP 服务：把猫娘暴露成 API，可以部署到线上服务器。

启动：
    uvicorn server:app --host 0.0.0.0 --port 8000

接口一览（带 /docs 自动生成的 Swagger）：
    GET    /healthz                 健康检查，给负载均衡用
    GET    /api/config              当前模型配置（不含密钥）
    GET    /api/profile             人格状态：好感度、心情、记忆数
    POST   /api/chat                对话，stream=false 返回完整 JSON
    POST   /api/chat/stream         对话，SSE 流式返回
    GET    /api/memories            查看长期记忆
    POST   /api/memories            手动写入一条记忆
    DELETE /api/memories/{id}       删除一条记忆
    POST   /api/session/new         开新会话（长期记忆保留）
    GET    /api/stats               记忆库统计
    GET    /api/voice               当前音色 + 可选音色列表
    PUT    /api/voice               切换音色 / 调语速音调
    POST   /api/voice/clone         上传参考音频做克隆（multipart）
    POST   /api/tts                 文字转语音，直接返回音频
    GET    /api/audio/{name}        取之前生成的语音
    GET    /                        内置的聊天页面

多用户：请求头 X-User-Id 区分，每个用户独立记忆库（data/users/<id>/memory.db）。
注意这是轻量隔离，不是身份认证——真上线请在前面套一层登录或网关。
"""
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from brain import BrainError
from config import PROVIDERS, describe, load_config
from core import CatgirlCore
from tts import (
    EDGE_ALIASES,
    TTS,
    TTS_PROVIDERS,
    TTS_PROVIDER_INFO,
    TTSError,
    describe as describe_tts,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG = load_config()

app = FastAPI(
    title="AI 猫娘 API",
    version="2.0.0",
    description="带三层长期记忆的猫娘，支持本地模型和在线大模型",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cores = {}
_coreLock = threading.Lock()


# --------------------------------------------------------------------- 鉴权
def require_auth(authorization: Optional[str] = Header(default=None)):
    """设了 CATGIRL_SERVER_KEY 才校验；没设就是开放接口（本地调试方便）。"""
    if not CFG.get("serverApiKey"):
        return
    expected = f"Bearer {CFG['serverApiKey']}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="缺少或错误的 Authorization")


def resolve_user(x_user_id: Optional[str] = Header(default=None)) -> str:
    """请求头里的用户标识，只留安全字符，防止拼路径时穿越。"""
    raw = x_user_id or "default"
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", raw)[:40]
    return safe or "default"


def get_core(userId: str) -> CatgirlCore:
    with _coreLock:
        core = _cores.get(userId)
        if core is None:
            dbPath = os.path.join(CFG["dataDir"], "users", userId, "memory.db")
            core = CatgirlCore(CFG, dbPath=dbPath)
            core.bootstrap()
            _cores[userId] = core
        return core


# --------------------------------------------------------------------- 语音
_ttsEngines = {}
_ttsLock = threading.Lock()


def userDir(userId, *parts):
    path = os.path.join(CFG["dataDir"], "users", userId, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def get_tts(userId: str) -> TTS:
    with _ttsLock:
        engine = _ttsEngines.get(userId)
        if engine is None:
            engine = TTS(CFG, userDir(userId, "voice"))
            _ttsEngines[userId] = engine
        return engine


def persist_audio(userId: str, text: str):
    """合成并落到用户音频目录，返回可访问的相对地址。"""
    engine = get_tts(userId)
    result = engine.synthesize(text)
    audioDir = userDir(userId, "audio")
    name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.{result['format']}"
    shutil.copyfile(result["path"], os.path.join(audioDir, name))
    prune_audio(audioDir)
    return {
        "audioUrl": f"/api/audio/{name}",
        "audioFormat": result["format"],
        "audioCached": result["cached"],
        "audioProvider": result["provider"],
    }


def prune_audio(audioDir, keep=300):
    """语音文件按时间留最近 300 个，否则一年下来能把磁盘吃满。"""
    try:
        files = [
            os.path.join(audioDir, name)
            for name in os.listdir(audioDir)
            if os.path.isfile(os.path.join(audioDir, name))
        ]
    except OSError:
        return
    if len(files) <= keep:
        return
    files.sort(key=os.path.getmtime, reverse=True)
    for path in files[keep:]:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------- 模型
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    stream: bool = True
    audio: bool = False  # 为 True 时额外合成语音，meta 里带 audioUrl


class MemoryRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=300)
    kind: str = "fact"
    importance: float = 4.0


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=3000)


class VoiceUpdate(BaseModel):
    provider: Optional[str] = None
    voice: Optional[str] = None
    model: Optional[str] = None
    rate: Optional[str] = None
    pitch: Optional[str] = None
    speed: Optional[float] = None
    refText: Optional[str] = None


# --------------------------------------------------------------------- 接口
@app.get("/healthz")
def healthz():
    return {"status": "ok", "provider": CFG["provider"], "model": CFG["model"]}


@app.get("/api/config", dependencies=[Depends(require_auth)])
def api_config():
    return {
        "current": describe(CFG),
        "tts": describe_tts(CFG),
        "providers": {
            name: {
                "baseUrl": preset["baseUrl"],
                "model": preset["model"],
                "embedModel": preset["embedModel"] or None,
                "apiKeyEnv": preset["apiKeyEnv"] or None,
            }
            for name, preset in PROVIDERS.items()
        },
    }


@app.get("/api/profile", dependencies=[Depends(require_auth)])
def api_profile(userId: str = Depends(resolve_user)):
    return get_core(userId).profile()


@app.post("/api/chat", dependencies=[Depends(require_auth)])
def api_chat(req: ChatRequest, userId: str = Depends(resolve_user)):
    """stream=false 时返回完整回复；stream=true 时走同一条流式逻辑但收集完再返回。"""
    core = get_core(userId)
    text, meta = core.respond(req.message)
    if meta.get("error"):
        raise HTTPException(status_code=502, detail=meta["error"])
    if req.audio and text:
        try:
            meta.update(persist_audio(userId, text))
        except TTSError as exc:
            meta["audioError"] = str(exc)
    return {"reply": text, **{k: v for k, v in meta.items() if k not in ("reply",)}}


@app.post("/api/chat/stream", dependencies=[Depends(require_auth)])
def api_chat_stream(req: ChatRequest, userId: str = Depends(resolve_user)):
    core = get_core(userId)

    def events():
        try:
            for kind, payload in core.respond_iter(req.message):
                if kind == "delta":
                    yield f"data: {json.dumps({'type': 'delta', 'text': payload}, ensure_ascii=False)}\n\n"
                else:
                    if payload.get("error"):
                        body = {"type": "error", "message": payload["error"]}
                        yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
                    else:
                        # 正文全部吐完之后再合成语音，前端拿到 meta 就能播
                        if req.audio and payload.get("reply"):
                            try:
                                payload.update(persist_audio(userId, payload["reply"]))
                            except TTSError as exc:
                                payload["audioError"] = str(exc)
                        yield f"data: {json.dumps({'type': 'meta', **payload}, ensure_ascii=False)}\n\n"
        except BrainError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # 生成器里抛异常会导致连接直接断，兜住并告知前端
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 关掉 Nginx 缓冲，否则流式会被攒成一坨才吐
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/memories", dependencies=[Depends(require_auth)])
def api_memories(limit: int = 30, kind: Optional[str] = None, userId: str = Depends(resolve_user)):
    return {"memories": get_core(userId).list_memories(min(limit, 200), kind)}


@app.post("/api/memories", dependencies=[Depends(require_auth)])
def api_add_memory(req: MemoryRequest, userId: str = Depends(resolve_user)):
    return get_core(userId).add_memory(req.content, req.kind, req.importance)


@app.delete("/api/memories/{memoryId}", dependencies=[Depends(require_auth)])
def api_forget(memoryId: int, userId: str = Depends(resolve_user)):
    if not get_core(userId).forget(memoryId):
        raise HTTPException(status_code=404, detail="没有这条记忆")
    return {"deleted": memoryId}


@app.post("/api/session/new", dependencies=[Depends(require_auth)])
def api_new_session(userId: str = Depends(resolve_user)):
    core = get_core(userId)
    return {"session": core.new_session()}


@app.get("/api/stats", dependencies=[Depends(require_auth)])
def api_stats(userId: str = Depends(resolve_user)):
    return get_core(userId).stats()


# --------------------------------------------------------------------- 语音
@app.get("/api/voice", dependencies=[Depends(require_auth)])
def api_voice(userId: str = Depends(resolve_user)):
    engine = get_tts(userId)
    return {
        "profile": engine.profile.public(),
        "voices": engine.listVoices(),
        "providers": TTS_PROVIDER_INFO,
    }


@app.put("/api/voice", dependencies=[Depends(require_auth)])
def api_set_voice(req: VoiceUpdate, userId: str = Depends(resolve_user)):
    engine = get_tts(userId)
    patch = {k: v for k, v in req.dict().items() if v is not None}
    provider = patch.get("provider")
    if provider:
        if provider not in TTS_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"不支持的 ttsProvider：{provider}")
        if provider != engine.provider:
            preset = TTS_PROVIDERS[provider]
            # 换了后端，旧音色在新后端不存在，回到该后端的默认音色
            patch.setdefault("voice", preset["voice"])
            patch.setdefault("model", preset["model"])
            patch.setdefault("format", preset["format"])

    if "voice" in patch and (provider or engine.provider) == "edge":
        patch["voice"] = EDGE_ALIASES.get(patch["voice"].lower(), patch["voice"])

    engine.profile.save(patch)
    return {"profile": engine.profile.public(), "voices": engine.listVoices()}


@app.post("/api/voice/clone", dependencies=[Depends(require_auth)])
async def api_clone_voice(
    file: UploadFile = File(...),
    text: str = Form(...),
    provider: str = Form("auto"),
    name: str = Form(""),
    userId: str = Depends(resolve_user),
):
    """上传一段 5~15 秒的干净人声，克隆成她的音色。

    siliconflow：上传到云端换 voice uri；
    gptsovits：只存本地，推理时把参考音频喂给本地服务。
    """
    engine = get_tts(userId)
    target = provider if provider != "auto" else engine.provider
    if target == "edge":
        raise HTTPException(status_code=400, detail="edge 是固定音色库，不能克隆，请选 gptsovits 或 siliconflow")
    if target not in ("siliconflow", "gptsovits"):
        raise HTTPException(status_code=400, detail=f"{target} 不支持音色克隆")

    refText = (text or "").strip()
    if len(refText) < 2:
        raise HTTPException(status_code=400, detail="请填写参考音频里说的原话，越长越准")

    data = await file.read()
    if len(data) < 16 * 1024:
        raise HTTPException(status_code=400, detail="音频太短，至少 5 秒清晰人声")
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="音频超过 12MB，请先裁剪到 15 秒内")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac"):
        ext = ".wav"
    refName = f"ref{ext}"
    voiceDir = userDir(userId, "voice")
    refPath = os.path.join(voiceDir, refName)
    with open(refPath, "wb") as handle:
        handle.write(data)

    engine.profile.save(
        {
            "provider": target,
            "refAudio": refName,
            "refText": refText,
            "format": "wav" if target == "gptsovits" else "mp3",
            # 本地后端的「音色」就是这份参考音频，没有独立 voice id
            "voice": "reference" if target == "gptsovits" else engine.profile.voice,
        }
    )

    uri = ""
    if target == "siliconflow":
        try:
            uri = engine.uploadReference(refPath, refText, name.strip() or None)
        except TTSError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    return {
        "profile": engine.profile.public(),
        "voices": engine.listVoices(),
        "customVoiceUri": uri or None,
        "bytes": len(data),
    }


@app.post("/api/tts", dependencies=[Depends(require_auth)])
def api_tts(req: TTSRequest, userId: str = Depends(resolve_user)):
    try:
        result = get_tts(userId).synthesize(req.text)
    except TTSError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    with open(result["path"], "rb") as handle:
        audio = handle.read()
    return Response(
        content=audio,
        media_type=result["mime"],
        headers={
            "Cache-Control": "no-cache",
            "X-TTS-Provider": result["provider"],
            "X-TTS-Cached": "1" if result["cached"] else "0",
        },
    )


@app.get("/api/audio/{name}", dependencies=[Depends(require_auth)])
def api_audio(name: str, userId: str = Depends(resolve_user)):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
        raise HTTPException(status_code=400, detail="文件名不合法")
    path = os.path.join(CFG["dataDir"], "users", userId, "audio", name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="没有这个音频")
    media = mimetypes.guess_type(name)[0] or "audio/mpeg"
    return FileResponse(path, media_type=media)


@app.get("/")
def index():
    page = os.path.join(BASE_DIR, "web", "index.html")
    if not os.path.exists(page):
        return {"message": "AI 猫娘 API 已启动，访问 /docs 查看接口文档"}
    return FileResponse(page, media_type="text/html; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=CFG["host"],
        port=CFG["port"],
        log_level="info",
    )
