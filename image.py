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
    "洛天依，浅紫灰色长发，齐刘海，头顶扎着一个 ∞ 形的发环"
    "（两个扁平的椭圆环在中间交汇连在一起，像横过来的数字 8，"
    "整体宽度和她的脸宽差不多；环的下半段被头顶的头发盖住，"
    "只有两个向上凸的拱形从头发里探出来），"
    "交汇处是一个四瓣花形的浅蓝色发饰（四片椭圆花瓣朝四个斜角展开，中心一个圆点），"
    "两侧垂着长长的发束直至腰际，"
    "发间别着白色小花发饰，翠绿色大眼睛，皮肤白皙，脸型小巧，"
    "笑起来嘴角微微上扬"
)

# 头顶这个 ∞ 是她最好认的特征，缺了就只是个「浅紫灰长发的女孩」。
# 形态、宽度、中心发饰、前后遮挡都得写死，早期十几版全错：先画成两个分开的双丸子，
# 再是竖立的圆环，然后摆在正头顶像皇冠、环宽超到耳朵外面、中心蝴蝶结太大、
# 最后一版是把整圈环都画在头发外面（环的下半圈压在刘海上，像贴在额头的装饰）。
# 正确的是——扁平椭圆、中间连通成 ∞、总宽约等于脸宽、中心一个小的四瓣花，
# 并且环的下半段要收进头发后面，只留两个上拱的弧。
# 注意别说「藏在脑后」：正面视角表达不了这个，模型最省事的做法是把环整个删掉，
# 只能说「下半段被头顶的头发压住」，它才会用头发去盖。
# 参考图里不一定有，光靠一句话管不住，所以和发色一样在提示词首尾各钉一次。
HAIR_LOCK = (
    "她头顶扎着一个 ∞ 形的发环：两个扁平的椭圆环（左右宽、上下窄）并排在正中间交汇、"
    "连在一起构成 ∞ 符号；环身只有一束头发那么细，环内是空的；"
    "整个 ∞ 的宽度和她的脸宽差不多，两侧不超过脸颊；"
    "前后关系很重要：只有两个环的上半段从头顶探出来，形成两个向上凸的拱形弧，"
    "环的下半段被头顶的头发压在下面，弧的两端伸进头发里就消失——"
    "是用头发盖住它，不是把环画短、画断或者去掉；"
    "交汇处是一个四瓣花形的浅蓝色发饰——四片椭圆花瓣朝四个斜角展开、中心一个圆点，"
    "浅蓝填充深蓝描边，不是用缎带打的蝴蝶结——花饰小巧，完整露在头发表面，不要盖住环身"
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

# 描述里出现这些词，说明是他临时指定穿什么：这时候不能再拿参考图的白裙压回去，
# 否则「换上这件」永远换不掉。宁可多认几个词，也不要每个字都写全的语义分析。
OUTFIT_WORDS = (
    "穿", "裙", "衫", "外套", "卫衣", "睡衣", "睡裙", "吊带", "礼服", "婚纱", "泳装",
    "泳衣", "制服", "校服", "汉服", "和服", "毛衣", "衬衫", "短裤", "长裤", "牛仔",
    "袜", "鞋", "靴", "帽", "围巾", "披风", "袍", "背心", "上衣", "这套", "那件", "这件",
)


def wantsOutfit(brief):
    """描述里是不是在指定服装。"""
    return any(word in (brief or "") for word in OUTFIT_WORDS)


# 用来在画面描述里找服装片段。比 OUTFIT_WORDS 收得紧一些：像「这件」「那件」
# 这种指代词不算，否则「拿着那件毛衣」会被整句剪掉。
OUTFIT_NOUN = re.compile(
    r"卫衣|毛衣|衬衫|针织|开衫|外套|大衣|风衣|羽绒|夹克|马甲|背心|"
    r"连衣裙|裙子|短裙|长裙|吊带|礼服|睡裙|睡衣|泳装|泳衣|制服|校服|汉服|和服|"
    r"围巾|牛仔|短裤|长裤|裤子|袜子|靴|鞋子|高跟鞋|帽子"
)


def stripOutfit(brief):
    """把画面描述里的服装片段剪掉。

    主人发来衣服图时，她常顺手把日程里那身写进描述（「蓝色卫衣和青绿围巾」），
    模型看到这么具体的文字就跟着文字画，图2 白给了。15:51 那次实测就是这样：
    日志写着「2 张参考（含来图换装）」，画出来还是卫衣加围巾。
    所以有服装参考图时，先把这些片段剪掉，只留场景/动作/姿势/光线。
    """
    if not brief:
        return ""
    parts = re.split(r"([，,。；;])", brief)
    kept = [part for part in parts if not OUTFIT_NOUN.search(part)]
    text = "".join(kept).strip(" ，,。；;、")
    if not text:
        return ""
    # 剪完可能剩个孤零零的「穿着」，或者「和」挂在句尾，收拾一下
    text = re.sub(r"(穿着|身穿|穿了|换上|套着|披着)$", "", text).strip(" ，,、")
    text = re.sub(r"[，,]{2,}", "，", text)
    return text or ""



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
        # 冷却只剩几十秒时别报错让人干等下一次：等一等直接画，主人根本分不出来
        self.grace = int(cfg.get("imageGrace") or 90)
        self.refDir = cfg.get("imageRefDir") or "/data/image/refs"
        self.outDir = cfg.get("imageOutDir") or "/data/image/out"
        # 主人发来的图落盘在这儿，等他说「换上这件」时当第二张参考
        self.inboxDir = cfg.get("imageInboxDir") or "/data/image/inbox"
        self.inboxKeep = int(cfg.get("imageInboxKeep") or 60)
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
    @staticmethod
    def _uri(path):
        """本地图片转 data URI。容器里没有公网地址，只能 base64 内嵌。"""
        with open(path, "rb") as handle:
            raw = handle.read()
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")

    def _refs(self, limit=1):
        """参考图转 data URI。

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
                uris.append(self._uri(path))
            except OSError as exc:
                log(f"读参考图 {name} 失败：{exc}")
                continue
            if len(uris) >= limit:
                break
        return uris

    # ------------------------------------------------------------ 来图
    def saveInbox(self, raw, ext=".jpg"):
        """主人的来图落盘，留给他「换上这件」时当参考。失败返回空串。"""
        if not raw:
            return ""
        try:
            os.makedirs(self.inboxDir, exist_ok=True)
            stamp = datetime.now(LOCAL_TZ).strftime("in-%Y%m%d-%H%M%S")
            path = os.path.join(self.inboxDir, f"{stamp}-{int(time.time() * 1000) % 1000:03d}{ext}")
            with open(path, "wb") as handle:
                handle.write(raw)
        except OSError as exc:
            log(f"存来图失败：{exc}")
            return ""
        self._pruneInbox()
        return path

    def _pruneInbox(self):
        """来图只留最近几十张，别把 2G 内存的小机器塞满。"""
        if self.inboxKeep <= 0:
            return
        files = []
        try:
            for name in os.listdir(self.inboxDir):
                full = os.path.join(self.inboxDir, name)
                try:
                    files.append((os.path.getmtime(full), full))
                except OSError:
                    pass
        except OSError:
            return
        if len(files) <= self.inboxKeep:
            return
        files.sort()
        for _mtime, full in files[: len(files) - self.inboxKeep]:
            try:
                os.remove(full)
            except OSError:
                pass

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
    def buildPrompt(self, brief, hasRef=False, hasGarment=False):
        """拼生图提示词。

        有参考图时写的是「编辑指令」：先钉死长相不许变，再描述新场景，
        最后明确要求换掉背景、姿势、表情和构图。不写这一段，模型会把
        参考图的灰底正面站姿整个搬过来，每次自拍都是同一张。

        穿什么按优先级来：他发来的那张衣服图 > 文字里点名的那套 >
        日程里排的 outfit > 参考图的白裙。文字说不清蕾丝花边这种细节，
        所以有图就先看图——这是「换上这件」最贴的做法。
        """
        brief = (brief or "").strip()
        # 有服装参考图时，描述里的衣服会和图2 打架（15:51 实测画成了卫衣围巾），先剪掉
        if hasGarment:
            brief = stripOutfit(brief)
        scene = self._scene()
        if hasGarment:
            # 日程里的 outfit 同理，也会把卫衣围巾塞进提示词
            scene = stripOutfit(scene)
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
        if hasGarment:
            change = (
                "再确认一次：她身上穿的是图2 那件衣服，不是【画面】或【此刻】里提到的任何衣服。"
                "款式、剪裁、领口高低、肩带、袖长、下摆长度全部和图2 一模一样，"
                "不要自作主张加领子、加长袖、加外套、加内搭，也不要把它换成别的同类衣服。"
                "背景、光影、构图、视角、姿势和表情换成新的，不要沿用图1里的纯灰背景和正面站姿。"
            )
        elif brief and wantsOutfit(brief):
            change = (
                "服装按【画面】里写的那套换掉，连发型和配饰也按描述来，"
                "背景、光影、构图、视角、姿势和表情也全部换成新的，"
                "不要沿用图1里的纯灰背景和正面站姿。"
            )
        elif outfit:
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
            "都要和图1是同一个人。"
            f"{HAIR_LOCK}，这一条不能省，少了就不像她了。"
            "她的头发永远是浅紫灰色、眼睛永远是翠绿色——"
            "环境光再暖（夕阳、台灯、霓虹）也只加一层光晕，不许把发色染成橘色金色。",
        ]
        if hasGarment:
            lines.append(
                "图2是他发来的服装参考，是这张图里**唯一有效**的服装要求。"
                "衣服的颜色、款式、领口、袖口、蕾丝花边、纽扣、长短全部照图2，"
                "图2里的人物长相、姿势和背景一概不要。"
            )
        lines.append("把她画到下面这个新场景里：")
        if brief:
            lines.append(f"【画面】{brief}")
            if hasGarment:
                lines.append(
                    "（【画面】只用来定场景、动作、姿势、景别和光线。"
                    "这段文字里如果提到任何衣服，一律作废，服装只看图2。）"
                )
        else:
            lines.append(
                "【画面】穿着图2那件衣服，随手拍一张她此刻的样子"
                if hasGarment
                else "【画面】随手拍一张她此刻的样子"
            )
        if scene:
            lines.append(f"【此刻】{scene}")
        lines.append(f"【风格】{style}")
        lines.append(f"【硬性要求】{ANIME_LOCK}")
        lines.append(change)
        # 首尾各钉一次：模型对最后一行最敏感，「夕阳把头发照成橘色」这类
        # 描述真能把发色带跑偏，末尾再提醒一句压住它
        lines.append("提醒：她的发色始终是浅紫灰色，瞳色始终是翠绿色，任何光线下都不变。")
        lines.append(f"提醒：{HAIR_LOCK}，画的时候留出头顶空间，别把两个环切出画面。")
        if hasGarment:
            lines.append("提醒：她身上穿的是图2 那件衣服，不是你文字里描述的任何衣服。")
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

    def draw(self, brief, force=False, garmentPath=""):
        """画一张，返回本地路径。失败抛 ImageError，调用方决定怎么说。

        garmentPath：主人发来的衣服图。给了就当第二张参考，衣服照它穿——
        文字转述蕾丝花边这种细节永远不准，直接上图才靠谱。
        """
        if not self.enabled:
            raise ImageError("生图功能没开")
        if not self.apiKey:
            raise ImageError("没有可用的生图密钥")
        left = self.cooldownLeft()
        if left > 0 and not force:
            if left <= self.grace:
                # 剩几十秒就等，别为这点时间把「换上这件」直接拒了
                log(f"冷却只剩 {left} 秒，等一等再画")
                time.sleep(left + 1)
            else:
                raise ImageError(f"冷却中，还有 {left} 秒")

        refs = self._refs()
        garment = ""
        if garmentPath:
            try:
                if os.path.getsize(garmentPath) > 6 * 1024 * 1024:
                    log("来图超过 6MB，当参考跳过")
                else:
                    garment = self._uri(garmentPath)
            except OSError as exc:
                log(f"读来图失败：{exc}")
        if garment and refs:
            images = refs + [garment]
        else:
            images = refs
            garment = ""
        prompt = self.buildPrompt(brief, bool(refs), bool(garment))
        started = time.time()
        taskId = self._submit(prompt, images)
        url = self._wait(taskId)
        path = self._download(url)
        self.markDrawn()
        self._prune()
        log(
            f"出图 {os.path.basename(path)}｜{len(images)} 张参考"
            f"{'（含来图换装）' if garment else ''}｜"
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
