"""模型调用层：一份代码同时对接本地服务和在线大模型。

只用标准库 urllib，不引入 HTTP 客户端依赖，服务器上少一个坑。

不同服务商的差异都在这里吸收掉：
- 鉴权：多数是 `Authorization: Bearer <key>`，OpenRouter 还要带来源标识
- 结构化输出：OpenAI 系支持 json_schema，DeepSeek/Moonshot 等只支持 json_object，
  都不支持的只能靠提示词 + 括号截取，所以做成逐级降级的策略链
- 思维链：Qwen 系要 reasoning_effort 或 enable_thinking，其他家不该收到这些字段
- 限流：在线接口会 429/5xx，必须指数退避重试，否则一条记忆就悄悄丢了
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "debug.log")

MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["fact", "preference", "event"]},
                    "content": {"type": "string"},
                    "importance": {"type": "integer"},
                },
                "required": ["kind", "content", "importance"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}

EXTRACT_PROMPT = """你是记忆整理模块，负责从对话中抽取值得长期记住的信息。

抽取标准：
- 只记关于主人（用户）的稳定信息：身份、偏好、习惯、重要事件、正在做的事、约定、人际与日程
- 也记猫娘与主人关系的变化（例如第一次见面、约定、主人夸过或批评过什么）
- 忽略：寒暄、一次性的情绪、没有信息量的闲聊、猫娘自己编造的内容
- 一条记忆只说一件事，用第三人称陈述并带上主语，控制在 40 字以内
- 宁可少记，也不要记废话；确实没有可记的就返回空数组

重要度打分：
1 = 可有可无的小事
3 = 一般偏好或事件
5 = 身份级信息（名字、生日、职业、禁忌、重大承诺）

只输出 JSON，不要任何解释、不要 Markdown 代码块：
{{"memories":[{{"kind":"fact|preference|event","content":"...","importance":3}}]}}

对话：
主人：{user}
{name}：{reply}
"""

SUMMARY_PROMPT = """把下面这段对话压缩成一段 120 字以内的第三人称摘要，用于日后回忆。

要求：
- 保留：谁做了什么、关键决定、情绪转折、约定
- 丢掉：客套、重复、语气词
- 直接输出摘要正文，不要"这段对话中"之类的开头

对话：
{history}
"""


def _debug(tag, text):
    """CATGIRL_DEBUG=1 时把模型原始输出落到 data/debug.log，方便调记忆质量。"""
    if not os.getenv("CATGIRL_DEBUG"):
        return
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG), exist_ok=True)
        with open(DEBUG_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"\n===== {time.strftime('%H:%M:%S')} {tag} =====\n{text}\n")
    except OSError:
        pass


class BrainError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Brain:
    def __init__(self, cfg):
        self.cfg = cfg
        self.baseUrl = cfg["baseUrl"].rstrip("/")
        self.chatModel = cfg["model"]
        self.embedModel = cfg.get("embedModel") or ""
        self.embedBaseUrl = (cfg.get("embedBaseUrl") or cfg["baseUrl"]).rstrip("/")
        self.embedApiKey = cfg.get("embedApiKey") or cfg.get("apiKey") or ""
        self.apiKey = cfg.get("apiKey") or ""
        self.provider = cfg.get("provider", "lmstudio")
        self.timeout = int(cfg.get("timeout", 120))
        self.jsonMode = (cfg.get("jsonMode") or "json_object").lower()
        self.thinkingMode = (cfg.get("thinkingMode") or "none").lower()
        self.disableThinking = bool(cfg.get("disableThinking", True))

        # 多模态：qq.py 收到图片时用它把图转成一句描述，留空则关闭图片识别。
        # baseUrl / apiKey 留空就复用主配置，通常只需要换 model 名。
        self.visionModel = (cfg.get("visionModel") or "").strip()
        self.visionBaseUrl = (cfg.get("visionBaseUrl") or cfg["baseUrl"]).rstrip("/")
        self.visionApiKey = cfg.get("visionApiKey") or self.apiKey

        # 语音识别：QQ 语音转文字，复用 qwen3-asr-flash（OpenAI 兼容 input_audio）。
        self.asrModel = (cfg.get("asrModel") or "").strip()
        self.asrBaseUrl = (cfg.get("asrBaseUrl") or cfg["baseUrl"]).rstrip("/")
        self.asrApiKey = cfg.get("asrApiKey") or self.apiKey

        # 本机 LM Studio 并发推理会互相踢掉，统一串行最稳；在线服务也无所谓这点延迟
        self._lock = threading.RLock()
        self._jsonStrategy = None  # 记住哪一档能用，避免每次都试错
        self.embedAvailable = bool(self.embedModel)

    # ------------------------------------------------------------------ 底层
    def _headers(self, apiKey):
        headers = {"Content-Type": "application/json"}
        if apiKey:
            headers["Authorization"] = f"Bearer {apiKey}"
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/local-catgirl"
            headers["X-Title"] = "Local Catgirl"
        return headers

    def _request(self, url, payload=None, apiKey=None, retried=0):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers=self._headers(apiKey if apiKey is not None else self.apiKey),
            method="GET" if payload is None else "POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            status = exc.code
            # 429 限流 / 5xx 抖动：在线服务常见，退避重试
            if retried < 3 and (status == 429 or 500 <= status < 600):
                time.sleep(1.5 * (2**retried))
                return self._request(url, payload, apiKey, retried + 1)
            # 本地 LM Studio 偶发卸载模型，重发一次即可重新加载
            if retried < 1 and "unloaded" in detail.lower():
                time.sleep(1.5)
                return self._request(url, payload, apiKey, retried + 1)
            raise BrainError(f"{status}：{detail}", status=status) from None
        except urllib.error.URLError as exc:
            raise BrainError(
                f"连不上 {self.baseUrl}\n"
                f"本地部署请确认 LM Studio / Ollama 的服务器已启动；"
                f"在线服务请检查网络和 baseUrl。({exc.reason})"
            ) from None

    def list_models(self):
        with self._request(f"{self.baseUrl}/models") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [item.get("id") for item in data.get("data", [])]

    # ---------------------------------------------------------------- 多模态
    def describeImage(self, dataUri, prompt=None):
        """把一张图片交给多模态模型，返回一句中文描述；未配置则返回空串。

        传 data URI 而不是图片 URL，是因为 QQ 的图片链接带时效，
        下载完立刻编码最稳妥。
        """
        if not self.visionModel or not dataUri:
            return ""
        prompt = prompt or "用一句中文描述这张图片，30 字以内，只说看到的内容，不要客套。"
        payload = {
            "model": self.visionModel,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": dataUri}},
                ],
            }],
            "max_tokens": 160,
            "temperature": 0.2,
            "stream": False,
        }
        _debug("视觉", f"model={self.visionModel}")
        with self._lock:
            with self._request(
                f"{self.visionBaseUrl}/chat/completions", payload, self.visionApiKey
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        return (data["choices"][0]["message"].get("content") or "").strip()

    # ---------------------------------------------------------------- 语音识别
    def transcribe(self, dataUri):
        """把一段音频（data URI）交给 qwen3-asr-flash 转成文字；未配置则返回空串。

        QQ 语音经过协议端 get_record 转成 wav 后，编码成 data:audio/wav;base64,…
        这里走 compatible-mode 的 chat/completions，用 input_audio content 传进去，
        复用对话模型同一个 key，不用另开服务。
        """
        if not self.asrModel or not dataUri:
            return ""
        payload = {
            "model": self.asrModel,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {"data": dataUri},
                }],
            }],
            "stream": False,
            "asr_options": {"enable_itn": False},
        }
        _debug("语音识别", f"model={self.asrModel}")
        with self._lock:
            with self._request(
                f"{self.asrBaseUrl}/chat/completions", payload, self.asrApiKey
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        return (data["choices"][0]["message"].get("content") or "").strip()

    # ------------------------------------------------------------ 请求体拼装
    def _thinking_payload(self):
        """只给认识这些字段的服务商发，避免严格的网关直接 400。"""
        if not self.disableThinking:
            return {}
        if self.thinkingMode == "reasoning_effort":
            return {"reasoning_effort": "none"}
        if self.thinkingMode == "enable_thinking":
            return {"enable_thinking": False}
        return {}

    def _chat_payload(self, messages, maxTokens, temperature, stream, jsonStrategy):
        payload = {
            "model": self.chatModel,
            "messages": messages,
            "max_tokens": maxTokens,
            "temperature": temperature,
            "stream": stream,
        }
        payload.update(self._thinking_payload())
        if jsonStrategy == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "memory_items", "strict": True, "schema": MEMORY_SCHEMA},
            }
        elif jsonStrategy == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    # ------------------------------------------------------------------ 对话
    def chat(self, messages, maxTokens=800, temperature=0.85, jsonMode=False, retried=False):
        with self._lock:
            return self._chat_locked(messages, maxTokens, temperature, jsonMode, retried)

    def _chat_locked(self, messages, maxTokens, temperature, jsonMode, retried):
        strategy = self._jsonStrategy if jsonMode else None
        payload = self._chat_payload(messages, maxTokens, temperature, False, strategy)
        _debug("请求", f"model={self.chatModel} json={strategy} maxTokens={maxTokens}")

        try:
            with self._request(f"{self.baseUrl}/chat/completions", payload) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except BrainError as exc:
            # 该服务商不支持当前结构化输出档位，降级再试
            if jsonMode and exc.status in (400, 422) and strategy != "none":
                nextStrategy = {"json_schema": "json_object", "json_object": "none"}.get(
                    strategy or self.jsonMode
                )
                _debug("降级", f"{strategy or self.jsonMode} -> {nextStrategy}：{exc}")
                self._jsonStrategy = nextStrategy
                return self._chat_locked(messages, maxTokens, temperature, jsonMode, retried)
            raise

        choice = data["choices"][0]
        message = choice.get("message", {})
        content = (message.get("content") or "").strip()
        reasoning = message.get("reasoning_content") or ""
        _debug(
            "chat响应",
            f"finish={choice.get('finish_reason')} usage={data.get('usage')} "
            f"content_len={len(content)} reasoning_len={len(reasoning)}",
        )

        # 思维链把 max_tokens 吃光时正文会是空串，翻倍额度重试一次
        if not content and not retried:
            _debug("空回复重试", f"maxTokens {maxTokens} -> {maxTokens * 2}")
            return self._chat_locked(messages, min(4096, maxTokens * 2), temperature, jsonMode, True)
        return content

    def chat_stream(self, messages, maxTokens=800, temperature=0.85):
        """流式输出，逐段 yield 文本增量。持锁覆盖整个流的生命周期。"""
        with self._lock:
            yield from self._stream_locked(messages, maxTokens, temperature)

    def _stream_locked(self, messages, maxTokens, temperature):
        payload = self._chat_payload(messages, maxTokens, temperature, True, None)
        with self._request(f"{self.baseUrl}/chat/completions", payload) as resp:
            for rawLine in resp:
                line = rawLine.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    delta = json.loads(chunk)["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                piece = delta.get("content")
                if piece:
                    yield piece

    # ------------------------------------------------------------------ 向量
    def embed(self, text):
        if not self.embedModel:
            raise BrainError("没有配置向量模型", status=400)
        payload = {"model": self.embedModel, "input": text}
        with self._lock:
            with self._request(
                f"{self.embedBaseUrl}/embeddings", payload, apiKey=self.embedApiKey
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        return data["data"][0]["embedding"]

    def embed_safe(self, text):
        """向量服务不可用时返回 None，记忆退化成纯重要度+新鲜度排序。"""
        if not self.embedAvailable:
            return None
        try:
            return self.embed(text)
        except BrainError as exc:
            _debug("向量失败", str(exc))
            return None

    # -------------------------------------------------------------- 记忆抽取
    def extract_memories(self, userText, replyText, name):
        prompt = EXTRACT_PROMPT.format(user=userText, reply=replyText, name=name)
        messages = [{"role": "user", "content": prompt}]

        raw = ""
        try:
            raw = self.chat(messages, maxTokens=900, temperature=0.2, jsonMode=True)
        except BrainError as exc:
            _debug("抽取失败(降级)", str(exc))
            try:
                raw = self.chat(messages, maxTokens=900, temperature=0.2)
            except BrainError:
                return []
        _debug("抽取原始输出", raw)
        return self._parse_items(raw)

    @staticmethod
    def _parse_items(raw):
        raw = (raw or "").strip()
        if not raw:
            return []
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        items = data.get("memories") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []

        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content or len(content) > 200:
                continue
            kind = str(item.get("kind", "fact")).strip() or "fact"
            if kind not in ("fact", "preference", "event", "summary"):
                kind = "fact"
            try:
                importance = float(item.get("importance", 3))
            except (TypeError, ValueError):
                importance = 3.0
            cleaned.append({"kind": kind, "content": content, "importance": importance})
        return cleaned

    # -------------------------------------------------------------- 会话摘要
    def summarize(self, turnRows):
        lines = []
        for row in turnRows:
            speaker = "主人" if row["role"] == "user" else "猫娘"
            lines.append(f"{speaker}：{row['content']}")
        history = "\n".join(lines)[:6000]
        messages = [{"role": "user", "content": SUMMARY_PROMPT.format(history=history)}]
        try:
            return self.chat(messages, maxTokens=300, temperature=0.3).strip()
        except BrainError:
            return ""
