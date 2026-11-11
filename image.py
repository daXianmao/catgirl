"""生图：让猫娘在聊天里画一张图发过来。

思路借自 dsh-image-studio（那是 DSH 插件，猫娘没有那个宿主），用原生 Python 重写：

- 人物存几张不同角度的参考图，每次出图都一起交给生图接口，保证「还是她」；
- 画什么由她自己决定：回复里写 <draw>画面描述</draw>，服务端据此出图；
- 画面里的穿着和地点默认跟着当前日程走，所以「我在录音棚」配的图也在录音棚；
- 有冷却，避免聊两句刷一张图烧钱。

接口用百炼万相（异步任务）：提交拿 task_id，轮询到 SUCCEEDED 再下载。
"""
import base64
import json
import os
import re
import subprocess
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8))

# 两套接口别混：
#   文生图（纯文字）走 text2image；带参考图的「保持这个人」走 image2image。
# 之前把参考图塞进文生图接口，模型不认这个字段，静默忽略，出来的是另一个人。
T2I_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
I2I_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{tid}"

# 模型想画图时写 <draw>画面</draw>；写成自闭合的 <draw/> 表示「按现在这样子画一张」
DRAW_TAG = re.compile(r"<draw(?:\s[^>]*)?>(.+?)</draw>", re.S | re.I)
BARE_DRAW = re.compile(r"<draw\s*[^>]*/>", re.I)
# 出站前要把标签整段剥掉，主人不该看到它
ANY_DRAW = re.compile(r"<draw(?:\s[^>]*)?>.*?</draw>|<draw\s*[^>]*/>", re.S | re.I)

DEFAULT_LOOK = (
    "洛天依，17 岁少女，浅紫灰色长发，齐刘海，两侧垂着长长的发束直至腰际，"
    "发间别着蓝色蝴蝶结和白色小花发饰，翠绿色大眼睛，皮肤白皙，脸型小巧，"
    "笑起来嘴角微微上扬"
)

# 写死风格而不是交给模型自由发挥：她每天几十条消息，风格飘了就像换了个人
DEFAULT_STYLE = (
    "日系二次元动漫画风，干净清晰的线稿，赛璐璐平涂配柔和渐变阴影，"
    "配色通透鲜艳，大眼睛带高光，TV 动画质感，浅色干净背景"
)

# 只靠风格词管不住模型：它一看到写实的参考图就往照片方向走，必须明着禁
ANIME_LOCK = (
    "整张图必须是 2D 二次元动漫插画（手绘动画质感），"
    "不要照片质感，不要 3D 渲染，不要 BJD 玩偶感，不要写实皮肤细节"
)


def log(text):
    stamp = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
    print(f"[{stamp}] [生图] {text}", flush=True)


def _overlaps(text, other):
    """place 里的词要是已经出现在 doing 里，就别再补一遍。"""
    if not other:
        return False
    for size in (4, 3, 2):
        for start in range(len(other) - size + 1):
            if other[start:start + size] in text:
                return True
    return False


class ImageError(RuntimeError):
    pass


class ImageStudio:
    """一次出图：拼提示词 → 提交 → 轮询 → 下载落盘。"""

    def __init__(self, cfg, store=None, schedule=None, name="洛天依"):
        self.cfg = cfg
        self.store = store
        self.schedule = schedule
        self.name = name
        self.enabled = cfg.get("imageEnabled", True) is not False
        # 带参考图时用编辑模型（能锁住长相），没参考图才退回纯文生图
        self.editModel = (cfg.get("imageEditModel") or "wan2.5-i2i-preview").strip()
        self.model = (cfg.get("imageModel") or "wan2.5-t2i-preview").strip()
        self.size = (cfg.get("imageSize") or "1280*1280").strip()
        self.cooldown = int(cfg.get("imageCooldown") or 900)
        self.refDir = cfg.get("imageRefDir") or "/data/image/refs"
        self.outDir = cfg.get("imageOutDir") or "/data/image/out"
        self.keep = int(cfg.get("imageKeep") or 120)
        self.appearance = (cfg.get("imageLook") or DEFAULT_LOOK).strip()
        self.style = (cfg.get("imageStyle") or DEFAULT_STYLE).strip()
        # 生图可以单独走一家（比如对话用 cheap 模型、画图用另一家的 key）
        self.apiKey = (cfg.get("imageApiKey") or cfg.get("apiKey") or "").strip()
        self.timeout = int(cfg.get("imageTimeout") or 150)

    # ------------------------------------------------------------ 状态
    def ready(self):
        return bool(self.enabled and self.apiKey)

    def cooldownLeft(self):
        """还要等多少秒才能再画。没有 store 就当没冷却。"""
        if not self.store or self.cooldown <= 0:
            return 0
        try:
            last = self.store.get_kv_float("lastImageAt", 0.0)
        except Exception:
            return 0
        return max(0, int(self.cooldown - (time.time() - last)))

    def markDrawn(self):
        if self.store:
            try:
                self.store.set_kv("lastImageAt", time.time())
            except Exception:
                pass

    # ------------------------------------------------------------ 素材
    def _refs(self, limit=1):
        """参考图转 data URI。容器里没有公网地址，只能 base64 内嵌。

        只取一张：编辑模型的多图语义是「把这几张融在一起」，
        塞多张不同角度反而会换来换去，一张正脸最稳。
        """
        uris = []
        try:
            names = sorted(os.listdir(self.refDir))
        except OSError:
            return uris
        for name in names:
            if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            path = os.path.join(self.refDir, name)
            try:
                # 万相对单张参考图有体积限制，太大了干脆不用，别让整次出图失败
                if os.path.getsize(path) > 6 * 1024 * 1024:
                    log(f"参考图 {name} 超过 6MB，跳过")
                    continue
                with open(path, "rb") as handle:
                    raw = handle.read()
            except OSError as exc:
                log(f"读参考图 {name} 失败：{exc}")
                continue
            mime = "image/png" if name.lower().endswith(".png") else "image/jpeg"
            uris.append(f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"))
            if len(uris) >= limit:
                break
        return uris

    def _segment(self):
        """当前时间段。拿不到返回 None。"""
        if not self.schedule:
            return None
        try:
            return self.schedule.current() or None
        except Exception:
            return None

    def _scene(self):
        """此刻在做什么、在哪、穿什么。拿不到就返回空串，纯文字出图。"""
        seg = self._segment()
        if not seg:
            return ""
        doing = (seg.get("doing") or "").strip()
        parts = [doing] if doing else []
        # place / outfit 常常和 doing 说法重叠（「楼下便利店」+「小区便利店」，
        # 「穿厚卫衣」再套一层「穿着」），重复拼接读起来像机器写的
        place = (seg.get("place") or "").strip()
        if place and not _overlaps(doing, place):
            parts.append("在" + place)
        outfit = (seg.get("outfit") or "").strip()
        if outfit:
            parts.append(outfit if outfit.startswith(("穿", "戴", "套")) else "穿着" + outfit)
        return "，".join(parts)

    # ------------------------------------------------------------ 提示词
    def buildPrompt(self, brief, hasRef=False):
        """拼生图提示词。

        有参考图时写的是「编辑指令」：先钉死长相不许变，再描述新场景，
        最后明确要求换掉背景、姿势、表情和构图。不写这一段，模型会把
        参考图的灰底正面站姿整个搬过来，每次自拍都是同一张。

        穿什么分两种：日程里排了 outfit 就按日程换掉；没排就保留参考图
        那套白色小礼服——否则模型会随手编一套，她自己都不认识。
        """
        brief = (brief or "").strip()
        scene = self._scene()
        style = self.style

        if not hasRef:
            lines = [f"【人物】{self.appearance}"]
            if brief:
                lines.append(f"【画面】{brief}")
            if scene:
                lines.append(f"【此刻】{scene}")
            lines.append(f"【风格】{style}")
            lines.append(f"【硬性要求】{ANIME_LOCK}")
            lines.append("画一张全新的画面，构图、视角、景别、姿势、动作、表情都按上面的文字安排。")
            return "\n".join(lines)

        seg = self._segment() or {}
        outfit = (seg.get("outfit") or "").strip()
        if outfit:
            change = (
                "服装按【此刻】里写的那套换掉，背景、光影、构图、视角、姿势和表情"
                "也全部换成新的，不要沿用图1里的纯灰背景和正面站姿。"
            )
        else:
            change = (
                "保留图1里这套穿搭：白色露肩连衣裙，泡泡短袖，黑色蝴蝶结领结，"
                "白色荷叶边配金色刺绣的短裙摆。只换背景、光影、构图、视角、姿势和表情，"
                "不要沿用图1里的纯灰背景和正面站姿。"
            )

        lines = [
            "图1是同一个人物的二次元形象设定。保持她的长相完全不变：脸型、五官、"
            "眼睛形状和瞳色、刘海分法、发色、以及发间的蓝色蝴蝶结和白色小花发饰，"
            "都要和图1是同一个人。她的头发永远是浅紫灰色、眼睛永远是翠绿色——"
            "环境光再暖（夕阳、台灯、霓虹）也只加一层光晕，不许把发色染成橘色金色。",
            "把她画到下面这个新场景里：",
        ]
        if brief:
            lines.append(f"【画面】{brief}")
        else:
            lines.append("【画面】随手拍一张她此刻的样子")
        if scene:
            lines.append(f"【此刻】{scene}")
        lines.append(f"【风格】{style}")
        lines.append(f"【硬性要求】{ANIME_LOCK}")
        lines.append(change)
        # 首尾各钉一次：模型对最后一行最敏感，「夕阳把头发照成橘色」这类
        # 描述真能把发色带跑偏，末尾再提醒一句压住它
        lines.append("提醒：她的发色始终是浅紫灰色，瞳色始终是翠绿色，任何光线下都不变。")
        return "\n".join(lines)

    # ------------------------------------------------------------ 出图
    def _post(self, url, body, extraHeaders=None):
        headers = {
            "Authorization": f"Bearer {self.apiKey}",
            "Content-Type": "application/json",
        }
        if extraHeaders:
            headers.update(extraHeaders)
        req = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _submit(self, prompt, refs):
        # 关掉提示词扩写：模型一扩写就会自己加一堆「唯美」「梦幻」，
        # 出来的东西和她实际在过的日子对不上
        params = {
            "size": self.size,
            "n": 1,
            "prompt_extend": False,
            "watermark": False,
        }
        if refs:
            url = I2I_URL
            payload = {
                "model": self.editModel,
                "input": {"prompt": prompt, "images": refs},
                "parameters": params,
            }
        else:
            url = T2I_URL
            payload = {
                "model": self.model,
                "input": {"prompt": prompt},
                "parameters": params,
            }
        try:
            data = self._post(url, payload, {"X-DashScope-Async": "enable"})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ImageError(f"提交失败 HTTP {exc.code}：{detail}") from exc
        except Exception as exc:
            raise ImageError(f"提交失败：{exc}") from exc

        taskId = ((data.get("output") or {}).get("task_id")) or ""
        if not taskId:
            raise ImageError(f"没拿到任务号：{json.dumps(data, ensure_ascii=False)[:300]}")
        return taskId

    def _wait(self, taskId):
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(3)
            try:
                req = urllib.request.Request(
                    TASK_URL.format(tid=taskId),
                    headers={"Authorization": f"Bearer {self.apiKey}"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    out = (json.loads(resp.read().decode("utf-8", "replace")).get("output")) or {}
            except urllib.error.HTTPError as exc:
                raise ImageError(f"查任务失败 HTTP {exc.code}") from exc
            except Exception as exc:
                log(f"查任务出错（继续等）：{exc}")
                continue

            status = out.get("task_status")
            if status == "SUCCEEDED":
                results = out.get("results") or []
                url = (results[0].get("url") if results else "") or ""
                if not url:
                    raise ImageError("任务成功但没有图片地址")
                return url
            if status in ("FAILED", "CANCELED"):
                msg = out.get("message") or json.dumps(out, ensure_ascii=False)[:300]
                raise ImageError(f"生成失败（{status}）：{msg}")
        raise ImageError("等图超时")

    def _download(self, url):
        # 万相给的地址是签名 OSS 链接，有有效期，必须当场取回
        try:
            with urllib.request.urlopen(url, timeout=90) as resp:
                raw = resp.read()
        except Exception as exc:
            raise ImageError(f"下载图片失败：{exc}") from exc
        if len(raw) < 1024:
            raise ImageError(f"下载到的图片太小（{len(raw)} 字节），多半是坏图")

        now = datetime.now(LOCAL_TZ)
        folder = os.path.join(self.outDir, now.strftime("%Y-%m"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, now.strftime("img-%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.png")
        with open(path, "wb") as handle:
            handle.write(raw)
        return self._shrink(path)

    def _shrink(self, path):
        """转成 JPEG 并发之前先缩一道。

        万相给的是两三兆的 PNG，base64 之后要三四兆——服务器上行很窄，
        传这一张就得等好几秒，聊天里发图会明显卡。压完只有两三百 KB。
        """
        target = os.path.splitext(path)[0] + ".jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
                 "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", target],
                check=True, timeout=30,
            )
        except Exception as exc:
            log(f"压缩失败，发原图：{exc}")
            return path
        try:
            if os.path.getsize(target) < os.path.getsize(path):
                os.remove(path)
                return target
            os.remove(target)
        except OSError:
            pass
        return path

    def _prune(self):
        """只留最近 keep 张，别把 2G 内存的小机器塞满。"""
        if self.keep <= 0:
            return
        files = []
        for root, _dirs, names in os.walk(self.outDir):
            for name in names:
                if name.lower().endswith((".png", ".jpg", ".jpeg")):
                    full = os.path.join(root, name)
                    try:
                        files.append((os.path.getmtime(full), full))
                    except OSError:
                        pass
        if len(files) <= self.keep:
            return
        files.sort()
        for _mtime, full in files[: len(files) - self.keep]:
            try:
                os.remove(full)
            except OSError:
                pass

    def draw(self, brief, force=False):
        """画一张，返回本地路径。失败抛 ImageError，调用方决定怎么说。"""
        if not self.enabled:
            raise ImageError("生图功能没开")
        if not self.apiKey:
            raise ImageError("没有可用的生图密钥")
        left = self.cooldownLeft()
        if left > 0 and not force:
            raise ImageError(f"冷却中，还有 {left} 秒")

        refs = self._refs()
        prompt = self.buildPrompt(brief, bool(refs))
        started = time.time()
        taskId = self._submit(prompt, refs)
        url = self._wait(taskId)
        path = self._download(url)
        self.markDrawn()
        self._prune()
        log(
            f"出图 {os.path.basename(path)}｜{len(refs)} 张参考｜"
            f"{time.time() - started:.1f}s｜{brief[:24]}"
        )
        return path

    def status(self):
        """给调试用的一行状态。"""
        refs = self._refs()
        return (
            f"enabled={self.enabled} edit={self.editModel} t2i={self.model} "
            f"size={self.size} cooldown={self.cooldown}s refs={len(refs)} out={self.outDir}"
        )
