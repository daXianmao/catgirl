# AI 猫娘 · 云端部署版

> **这是 `cloud` 分支**：模型一律走在线 API，不需要显卡，不装任何本地推理服务。
> 想在本机跑 LM Studio / Ollama 或本地 GPT-SoVITS，请切 `main` 分支。
> 两个分支的差异清单与切换方法见 [BRANCHES.md](BRANCHES.md)。

保留三样东西：**猫娘本体**（人格 + 三层长期记忆 + 网页）、**QQ 接入**、**语音**。

```
server.py      FastAPI 服务（对外入口）
core.py        核心：人格、记忆、上下文组装
brain.py       模型调用层：只连在线 API，自动适配各家结构化输出
memory.py      记忆库：SQLite + 向量检索 + 混合打分
tts.py         语音层：edge / 硅基流动 / OpenAI / 自建 SoVITS
voice.py       音色管理命令行（试听 / 换音色 / 克隆）
qq.py          QQ 适配器（OneBot v11，对接 NapCat / Lagrange）
config.py      配置：在线服务商预设 + 环境变量
catgirl.py     命令行入口（服务器上直接聊两句调试用）
persona.md     人格设定，改这里换性格
web/index.html 内置聊天页面
deploy/        systemd 单元 / 假协议端 / 云端自检脚本
```

## 一、五分钟跑起来

云端版唯一需要的就是一个**在线模型 API Key**（任选一家，见下表）。

```bash
cp .env.example .env
# 改三行：CATGIRL_PROVIDER / CATGIRL_API_KEY / CATGIRL_SERVER_KEY
docker compose up -d --build
docker compose logs -f
```

打开 `http://服务器IP:8000` 就是聊天页，`/docs` 是交互式 API 文档。

裸机（不用 Docker）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && vi .env
set -a && . ./.env && set +a
.venv/bin/python catgirl.py        # 终端里直接聊，验证 key 通不通
```

确认配置算不算对，跑一下自检（不联网、不消耗 token）：

```bash
.venv/bin/python deploy/cloud_selfcheck.py
```

它会检查：默认服务商是不是在线、语音默认是不是 edge、有没有残留的本地推理参数。

## 二、选对话模型

全部走 OpenAI 兼容接口，换 `CATGIRL_PROVIDER` 就行，代码不用动：

| provider | 默认对话模型 | 默认向量模型 | 密钥变量 |
|---|---|---|---|
| `openai` | gpt-4o-mini | text-embedding-3-small | `OPENAI_API_KEY` |
| `deepseek` | deepseek-chat | 无 | `DEEPSEEK_API_KEY` |
| `moonshot` | moonshot-v1-8k | 无 | `MOONSHOT_API_KEY` |
| `dashscope` | qwen-plus | text-embedding-v3 | `DASHSCOPE_API_KEY` |
| `zhipu` | glm-4-flash | embedding-3 | `ZHIPU_API_KEY` |
| `siliconflow` | Qwen2.5-7B-Instruct | BAAI/bge-m3 | `SILICONFLOW_API_KEY` |
| `openrouter` | gpt-4o-mini | 无 | `OPENROUTER_API_KEY` |

DeepSeek / Moonshot / OpenRouter 没有 embedding 接口，长期记忆会退化成
「重要度 + 新鲜度」排序。想保留语义召回，把向量单独指向一家：

```bash
CATGIRL_PROVIDER=deepseek
CATGIRL_API_KEY=sk-deepseek
CATGIRL_EMBED_BASE_URL=https://api.siliconflow.cn/v1
CATGIRL_EMBED_API_KEY=sk-siliconflow
CATGIRL_EMBED_MODEL=BAAI/bge-m3
```

中文场景强烈建议用 `bge-m3` 这类中文向量模型：英文向量模型对中文否定几乎无感
（「讨厌香菜」和「喜欢香菜」余弦相似度 = 1.0），程序虽加了极性校验兜底，换模型才是根治。

## 三、部署

### 方式 A：Docker（推荐）

```bash
git clone <你的仓库> catgirl && cd catgirl
git checkout cloud
cp .env.example .env       # 填 CATGIRL_PROVIDER / CATGIRL_API_KEY / CATGIRL_SERVER_KEY
docker compose up -d --build
```

记忆库在命名卷 `catgirl_data` 里，容器重建不丢。

### 方式 B：systemd（裸机 / 轻量云）

```bash
cd /opt/catgirl
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && vi .env
cp deploy/catgirl.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now catgirl
journalctl -u catgirl -f
```

### 反向代理（Nginx）

流式必须关缓冲，否则前端要等到整段生成完才看到字：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

Caddy 更省事：`catgirl.example.com { reverse_proxy 127.0.0.1:8000 { flush_interval -1 } }`

### 上线前必做

1. **设 `CATGIRL_SERVER_KEY`**，否则 `/api/*` 谁都能调，烧的是你的 token。
2. 前面再套一层登录或网关，`X-User-Id` 只是隔离标识，不是身份认证。
3. 给 `data/` 配卷或备份，那是猫娘的全部记忆。
4. 单 worker 起就够了：瓶颈在上游 API 的延迟，不在本机 CPU。

## 四、API

服务起来后打开 `http://服务器:8000/docs` 有交互式文档。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/config` | 当前模型配置（不含密钥） |
| GET | `/api/profile` | 好感度、心情、记忆数 |
| POST | `/api/chat` | 对话，返回完整 JSON |
| POST | `/api/chat/stream` | 对话，SSE 流式 |
| GET | `/api/memories` | 查看长期记忆 |
| POST | `/api/memories` | 手动写一条记忆 |
| DELETE | `/api/memories/{id}` | 删除记忆 |
| POST | `/api/session/new` | 开新会话（长期记忆保留） |
| GET | `/api/stats` | 记忆库统计 |
| GET | `/api/voice` | 当前音色 + 可选音色列表 |
| PUT | `/api/voice` | 换音色 / 调语速音调 |
| POST | `/api/voice/clone` | 上传参考音频克隆（multipart） |
| POST | `/api/tts` | 文字转语音，直接返回音频流 |
| GET | `/api/audio/{name}` | 取对话生成的语音 |

请求头：`X-User-Id` 区分用户（默认 `default`），设了服务密钥时加
`Authorization: Bearer <CATGIRL_SERVER_KEY>`。

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "X-User-Id: alice" \
  -d '{"message":"我叫林澈，讨厌香菜"}'
```

```json
{"reply":"林澈……记住了。","mood":"好奇","affinity":32,"memoriesUsed":["..."]}
```

流式接口返回 SSE：

```
data: {"type":"delta","text":"记得"}
data: {"type":"meta","reply":"...","mood":"认真","affinity":33,"memoriesUsed":["..."]}
data: [DONE]
```

Python 客户端：

```python
import requests

res = requests.post(
    "http://服务器:8000/api/chat",
    headers={"X-User-Id": "alice", "Authorization": "Bearer <key>"},
    json={"message": "晚上点外卖要注意什么？"},
    timeout=120,
)
print(res.json()["reply"])
```

## 五、语音

云端版默认用 `edge`：免费、免密钥、零部署，适合服务器直接开箱用。

| 后端 | 能克隆 | 成本 | 前提 |
|---|---|---|---|
| `edge` | ❌ 只能选预设音色 | 免费 | 服务器能访问微软 TTS |
| `siliconflow` | ✅ 10 秒录音零样本克隆 | 按字符计费，很便宜 | API Key + **账号实名认证** |
| `openai` | ❌ 固定音色 | 按量计费 | 任意 OpenAI 兼容 `/audio/speech` |
| `gptsovits` | ✅ 参考音频克隆，最像 | 自建机器的电费 | 另起一台带 GPU 的机器跑 `api_v2.py` |

### 选音色（立即可用）

```bash
.venv/bin/python voice.py list                  # 看有哪些音色
.venv/bin/python voice.py use xiaoyi            # 换成晓伊（少女感）
.venv/bin/python voice.py use xiaoxiao --rate +8%   # 晓晓，语速快 8%
.venv/bin/python voice.py say "主人，听得到吗" --play
```

或在网页里展开「记忆」面板，底部有音色下拉、试听和克隆入口。

### 想克隆成「某个人的声音」

```bash
export CATGIRL_TTS_PROVIDER=siliconflow
export CATGIRL_TTS_KEY=sk-xxxx
.venv/bin/python voice.py clone 我的录音.wav --text "录音里逐字对应的原话"
```

返回一个 `speech:名字:xxx:xxx` 的音色 ID，之后所有回复都用这个声音。

### 可选：自建 GPT-SoVITS（最像，但要另一台机器）

猫娘**不会**帮你启动它，只当 HTTP 服务调用；所以它可以跑在任何一台带 GPU 的机器上，
云端这台机器只负责发请求：

```bash
# 在 GPU 机器上
git clone https://github.com/RVC-Boss/GPT-SoVITS && cd GPT-SoVITS
python api_v2.py -a 0.0.0.0 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml

# 在猫娘这台机器上
CATGIRL_TTS_PROVIDER=gptsovits
CATGIRL_SOVITS_URL=http://10.0.0.8:9880
CATGIRL_SOVITS_REF=/opt/GPT-SoVITS/refs/bell.wav   # 填「服务端」上的路径
CATGIRL_SOVITS_REF_TEXT=我现在需要一个长达三秒的语音
```

注意 `sovitsRefAudio` 是**推理服务那台机器上的路径**，不是猫娘容器的路径。
连不上时 `/api/tts` 会明确报「连不上 GPT-SoVITS（地址）」，不会有隐藏的本地拉起行为。

### 参考音频的硬要求（各家都一样，决定成败）

- **5~15 秒**，干净人声，无背景音乐、无混响、无第二个人说话
- `--text` 必须和录音内容**逐字一致**，包括「嗯」「那个」这类语气词
- 格式 wav/mp3/m4a 都行，16kHz 以上；**优先 WAV 单声道**，5~10 秒最佳

### 对话直接返回语音

`POST /api/chat` 加 `"audio": true`，返回里会多出 `audioUrl`：

```json
{"reply":"……你好呀。","audioUrl":"/api/audio/1792408545-453a27ea.mp3","audioProvider":"edge"}
```

流式接口同样，`meta` 事件里带 `audioUrl`。语音合成在正文吐完之后才做，
不拖慢首字延迟；相同文本会命中缓存，第二次直接返回。

### 几个容易踩的点

- **`edge` 不要挂在对公网接口后面**：没 SLA、可能被限流，且微软随时改协议。对外请用 `siliconflow`。
- **多实例部署**：语音文件落在 `<dataDir>/users/<id>/audio/`，多台机器要挂共享卷或改成对象存储。
- **TTS 失败不影响聊天**：合成异常只会写进 `audioError`，文字照常返回。

## 六、接入 QQ

`qq.py` 是独立的 OneBot v11 客户端，和 Web 服务可以同时跑、共用同一份记忆目录。

### 两条路，先选一条

| 方案 | 要不要额外进程 | 资源 | 风险 |
|---|---|---|---|
| **协议端**（NapCat / Lagrange） | 要，常驻一个 QQ 客户端容器 | NapCat 约 400~700MB 内存 | 非官方实现，**违反 QQ 用户协议，有封号风险**，只建议小号 |
| **QQ 官方机器人** | 不要，走 Webhook | 几乎为零 | 要企业/个人开发者认证、备案域名 + HTTPS，私聊权限受限，需审核 |

下面的实现按**协议端**做（能私聊、能群聊、不用审核）。如果你的目标是正式对外的服务，
应该走官方机器人，不要用协议端。

### 起协议端（NapCat，Docker）

```bash
# 只起猫娘本体
docker compose up -d catgirl

# 连 QQ 一起起（catgirl + qq + napcat 三个容器）
docker compose --profile qq up -d
```

首次启动后：

1. 看 `docker compose logs napcat` 里的 WebUI 地址和 token；
2. 浏览器打开 `http://服务器IP:6099`，用**小号**扫码登录；
3. 在「网络配置」里新建 **WebSocket 服务端**，端口 `3001`，token 随便设一个长串；
4. 把同一个 token 填进 `.env` 的 `CATGIRL_QQ_TOKEN`，`docker compose --profile qq up -d` 重启生效。

`.env` 里最少要填这三项：

```bash
CATGIRL_QQ_ENABLED=true
CATGIRL_QQ_WS=ws://napcat:3001        # 裸机部署改 ws://127.0.0.1:3001
CATGIRL_QQ_GROUPS=123456789,987654321 # 允许的群号，留空 = 群里完全不说话
```

裸机（不用 Docker）就是：

```bash
python qq.py            # 常驻
python qq.py --check    # 只看配置
python qq.py --debug    # 退出时打印收发统计
```

### 行为规则

| 项 | 默认 | 说明 |
|---|---|---|
| 私聊 | 开 | `CATGIRL_QQ_PRIVATE=false` 关掉 |
| 群聊 | **全关** | 白名单为空不接任何群，必须显式加群号 |
| 群里必须 @ | 是 | `CATGIRL_QQ_AT_ONLY=false` 可改成听到就回（容易吵） |
| 记忆隔离 | 私聊按人 / 群聊按群 | 群里她记得的是「这个群」，不是某个人的私事 |
| 发言人标识 | 开 | 群里自动加「小满：」前缀，她能分清谁在说话 |
| 分条 | 400 字 | 超出按句切分，逐条发，间隔 0.4s |
| 限流 | 1.5s/条 | 防刷屏，也防上游 API 被限流 |
| 去重 | 开 | 断线重连后同一条消息不会回两遍 |
| 语音 | 关 | `CATGIRL_QQ_VOICE=true` 时，≤120 字的回复会额外发一条语音（实验性） |

群里发 `/reset`、`重置` 或 `新会话`，她会开新会话但保留长期记忆。

### 关于 2核2G 够不够

猫娘本体实测常驻 **54~66MB**，NapCat 是 Electron 版 QQ，实测 **400~700MB**：

| 机器 | 建议 |
|---|---|
| 2核2G4M | 只跑 `qq.py` + NapCat，别同时开 Web 服务 |
| 2核4G5M | **推荐**。Web + QQ + 协议端一起跑，还有并发余量 |
| 1核1G | 只能跑 Web 或 QQ 其中一个，且要配 swap |

云端版没有本地推理，所以**不需要显卡、也不需要 4G 以上内存**。
带宽不是问题：文字消息几 KB，语音 24kbps 也就 3KB/s。

### 不登录 QQ 也能验证

`deploy/fake_onebot.py` 是个假协议端，能确认整条链路通不通：

```bash
python deploy/fake_onebot.py 事件.json 30
CATGIRL_QQ_WS=ws://127.0.0.1:3099 CATGIRL_QQ_GROUPS=30003 python qq.py
```

事件文件格式见脚本头部注释，它会把你发出去的每条 action 打印成 `ACTION ...`。

## 七、记忆怎么工作

| 层 | 存什么 | 位置 | 生命周期 |
|---|---|---|---|
| 短期 | 最近 12 句原文 | `turns` 表 | 本次会话 |
| 长期 | 抽取的事实/偏好 + 向量 | `memories` 表 | 永久，超 400 条按分值淘汰 |
| 摘要 | 每 20 轮压缩的片段 | `memories`（kind=summary） | 永久 |

**检索**不是纯向量：

```
score = 0.65 × 余弦相似度 + 0.25 × (重要度/5) + 0.10 × 时间新鲜度（30 天半衰期）
```

**写入**在后台线程里做：每轮回复后让模型把对话抽成结构化记忆（kind/content/importance），
再向量化入库，不阻塞对话。结构化输出按 `json_schema → json_object → 纯提示词`
逐级降级，兼容不同服务商。

**合并**前会做极性校验：向量相似度 ≥ 0.90 且否定/正负向词一致才合并，
避免把「讨厌香菜」和「喜欢香菜」记成一条。

**遗忘**：超过容量时淘汰「重要度低 + 旧 + 从没被想起过」的记忆。

## 八、性能与容量

- 单次对话 = 1 次向量 + 1 次流式生成；后台再补 1 次记忆抽取（在线 API 约 1~2 秒）
- 记忆检索是纯 Python 线性扫描，几千条内无感；上万条建议换 `sqlite-vec` 或 FAISS
- SQLite 开了 WAL，单机多进程读没问题，写靠文件锁；每用户一个库文件互不干扰
- 延迟主要取决于上游 API：国内服务商通常 300~800ms 首字，跨境服务商要 1~3s

## 九、调试

```bash
CATGIRL_DEBUG=1 python catgirl.py
```

`data/debug.log` 会记录抽取的原始 JSON、finish_reason、token 用量、
结构化输出降级过程、空回复重试。调人格和记忆质量先看这个文件。

## 十、已知限制

1. **云端版没有本地推理**：断网就彻底不能说话。需要离线能力请用 `local` 分支。
2. **向量质量取决于模型**：英文向量模型对中文否定不敏感，换 `bge-m3` 明显更好。
3. **近重复记忆**：同义不同措辞仍会留下几条相近记录（阈值与误合并之间的取舍）。
4. **`X-User-Id` 不是认证**：只做数据隔离，别直接暴露在公网。
5. **没有记忆衰减曲线**：只有检索时的时间衰减和容量淘汰，还没有「反复回忆会加强」。
6. **edge 不能克隆、也没有情绪**：需要「像某个人」只能走 siliconflow 或自建 gptsovits。
7. **克隆效果七成看参考音频**：录音有底噪、有伴奏、和参考文本差一个字，都会明显掉质量。
8. **QQ 协议端有封号风险**：NapCat/Lagrange 属于非官方客户端，腾讯随时可能风控；只上小号，别用主号。
9. **QQ 语音是实验性的**：`record` 段需要协议端自己把 mp3 转成 silk，且容器要能读到 `/data` 里的音频文件；转不了时会退化成只发文字。
10. **QQ 端不流式**：QQ 没有逐字输出能力，所以是攒完整段再发；首字延迟等于整段生成时间。
