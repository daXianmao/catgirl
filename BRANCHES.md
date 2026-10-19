# 分支说明

同一套猫娘，两种部署形态。代码 90% 相同，差异集中在「模型怎么调用」。

| | `main` · 本地部署版 | `cloud` · 云端部署版 |
|---|---|---|
| 定位 | 自己机器上跑，功能最全 | 服务器 / 容器部署 |
| 对话模型 | LM Studio / Ollama 本地推理，也可切在线 API | **只能**在线 API |
| 语音 | GPT-SoVITS 本地常驻（可自动拉起）+ edge / 硅基流动 / OpenAI | edge（默认）/ 硅基流动 / OpenAI / 自建 SoVITS（远程 HTTP） |
| 硬件要求 | 需要显卡（本地推理），磁盘 8~15GB | 2核2G 起，不需要显卡 |
| 离线可用 | 是 | 否（断网就哑了） |
| 额外文件 | — | `deploy/cloud_selfcheck.py` 部署自检 |

## 切换

```bash
cd catgirl
git checkout main     # 本地部署版
git checkout cloud    # 云端部署版
git branch -v         # 看当前在哪个分支
```

## 本地配置不会被切分支弄丢

`config.json`、`.env`、`data/` 都写在 `.gitignore` 里：

- `config.json` 记录本机路径和音色，切分支不动它
- `data/` 是猫娘的全部记忆，切分支不会覆盖
- 两个分支各自跑起来时，读的都是同一份本地配置

也就是说：在 cloud 分支上如果 `config.json` 里还留着 `ttsProvider: gptsovits`，
它会照着执行（去连那个地址）。云端首次部署请从 `config.example.json` 重新复制一份，
或者干脆用 `.env` 覆盖，别沿用本地那份。

## 改公共代码

两边都要的改动，先在 `main` 改完提交，再合并进 cloud：

```bash
git checkout main
# ... 改代码 ...
git commit -am "fix: xxx"

git checkout cloud
git merge main        # 有冲突就按提示解，通常不会（两边改的文件基本不重叠）
```

只影响云端的改动，直接在 `cloud` 上改，别反向合回 main。

## cloud 相对 main 的全部差异

文件层面就这 8 个：

| 文件 | 差异 |
|---|---|
| `config.py` | 删 `lmstudio` / `ollama` 两个本地预设；删 `sovitsPython` / `sovitsScript` / `sovitsConfig` / `sovitsAutoStart`；默认 `provider=dashscope`、`ttsProvider=edge` |
| `tts.py` | 删本地拉起逻辑（`subprocess.Popen`、NLTK/ffmpeg 路径、进程冷却）约 100 行；只保留 HTTP 客户端；参考音频按「服务端路径」处理 |
| `voice.py` | 提示文案从「本地拉起」改为「远端服务」 |
| `Dockerfile` | 注释改为云端语义 |
| `.env.example` | 重写：去掉本地路径与 F5 残留，语音默认 edge |
| `config.example.json` | 云端默认值 |
| `README.md` | 重写：围绕 Docker + NapCat + API 密钥 |
| `deploy/cloud_selfcheck.py` | 新增，部署自检（不联网、不耗 token） |

`brain.py`、`memory.py`、`core.py`、`server.py`、`qq.py`、`persona.md`、`web/` 两边**完全一样**。
