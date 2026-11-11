"""每日日程：给她一份「自己正在过的日子」。

做法参考了 dsh-daily-schedule（那是个 DSH 侧栏插件，猫娘这边没有 DSH 宿主，
装不进去），所以只把那套思路用原生 Python 重写：每晚生成明天的一整天，
聊天时把「此刻在做什么、接下来想干嘛」塞进系统提示。

存储不新增表，直接借 kv 表：键是 schedule:YYYY-MM-DD，值是 JSON。
"""

import json
import random
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8))

# 一天的「类型」，按工作日 / 周末分开加权。演出日故意压得很低，
# 天天演出就不稀罕了。
KINDS_WORKDAY = (("录音日", 3), ("练歌日", 3), ("创作日", 2), ("休息日", 1))
KINDS_WEEKEND = (("休息日", 3), ("外出日", 2), ("创作日", 2), ("演出日", 1))

# 她的生活底色。想改就在 config 里填 scheduleWorld，整段覆盖。
DEFAULT_WORLD = (
    "她住在城西一间老小区的两居室，客厅角落支着一支麦克风和一台电钢，那是她的录音角。\n"
    "楼下便利店她每天都要去一趟，街角有家开了很多年的面馆，老板认得她。\n"
    "录音棚在城东，坐地铁四站；有时也去河边那家小 livehouse 试唱。\n"
    "她没有同事，最常打交道的是制作人和几个一起做音乐的朋友。"
)

# 生成时给她看的人设片段长度。人设几千字，全塞进去只是浪费 token，
# 前一段讲清「她是谁、什么性子」就够模型抓住味道了。
PERSONA_SLICE = 1200

# 日程段落的硬约束，模型写飞了就用这些兜住
MAX_SEGMENTS = 24
MAX_DOING_CHARS = 40


def log(text):
    print(f"[{datetime.now(LOCAL_TZ):%H:%M:%S}] [日程] {text}", flush=True)


def _minutes(text, default=None):
    """把 "07:30" 变成 450。"24:00" 要能变成 1440。"""
    try:
        hour, minute = str(text).strip().split(":")[:2]
        total = int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return default
    if total < 0 or total > 1440:
        return default
    return total


def _clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _shortAction(doing):
    """注入只要一个模糊印象，别把「配唱主歌第二遍，重录三处气口，
    和制作人商量桥段」整串塞进系统提示——60 字的回复根本用不上那么多。"""
    text = str(doing or "").strip()
    for mark in ("，", "、", "；", ",", ";"):
        pos = text.find(mark)
        if 4 <= pos <= 24:
            text = text[:pos]
            break
    return text[:28].strip()


def _overlaps(text, other):
    """place 里的词要是已经出现在 doing 里，就别再补一遍。
    「坐在面馆靠窗位」+「在街角面馆」这种重复读起来很傻。"""
    if not other:
        return False
    for size in (4, 3, 2):
        for start in range(len(other) - size + 1):
            if other[start:start + size] in text:
                return True
    return False


class ScheduleKeeper:
    """管日程的生成、存储、注入。所有方法都不该抛错——日程是锦上添花，
    坏了也绝不能把聊天带崩。"""

    def __init__(self, cfg, store, brain, name="洛天依", persona="", isBusy=None):
        self.enabled = cfg.get("scheduleEnabled", True) is not False
        self.hour = int(cfg.get("scheduleHour") or 22)
        self.store = store
        self.brain = brain
        self.name = name
        self.persona = persona or ""
        self.world = (cfg.get("scheduleWorld") or "").strip() or DEFAULT_WORLD
        # 主人在说话的时候别去抢模型，本地推理会互相踢
        self.isBusy = isBusy or (lambda: False)
        self._genLock = threading.Lock()
        self._ticker = None
        self._tickerStarted = False

    # ------------------------------------------------------------ 存取
    def _key(self, day):
        return f"schedule:{day.isoformat()}"

    def load(self, day):
        raw = self.store.get_kv(self._key(day))
        if not raw:
            return None
        try:
            plan = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(plan, dict) or not plan.get("segments"):
            return None
        return plan

    def save(self, day, plan):
        self.store.set_kv(self._key(day), json.dumps(plan, ensure_ascii=False))
        # 只留最近 21 天，kv 表别无声地长大
        self._prune(day)

    def _prune(self, today):
        try:
            cutoff = (today - timedelta(days=21)).isoformat()
            self.store.delete_kv_before("schedule:", cutoff)
        except Exception:
            pass

    # ------------------------------------------------------------ 生成
    def _pickKind(self, day):
        table = KINDS_WEEKEND if day.weekday() >= 5 else KINDS_WORKDAY
        names = [name for name, _ in table]
        weights = [weight for _, weight in table]
        return random.choices(names, weights=weights, k=1)[0]

    def _recentText(self, day):
        """最近三天的类型 + 几个活动，防止天天一个样。"""
        out = []
        for offset in (1, 2, 3):
            past = day - timedelta(days=offset)
            plan = self.load(past)
            if not plan:
                continue
            picks = [
                seg["doing"] for seg in plan.get("segments") or []
                if seg.get("doing") and len(seg["doing"]) <= 12
            ][:3]
            line = f"{past:%m-%d}「{plan.get('kind') or '未知'}」"
            if picks:
                line += "：" + "、".join(picks)
            out.append(line)
        return "；".join(out) if out else "（她刚来这里，还没有记录）"

    def _buildPrompt(self, day, kind):
        weekday = "一二三四五六日"[day.weekday()]
        return [
            {
                "role": "system",
                "content": (
                    f"你在替「{self.name}」排她一天的日子。她是虚拟歌手，从声音的世界醒来，"
                    "这具身体只是让她能站到人前。十六岁。\n\n"
                    f"【她的性格】\n{self.persona[:PERSONA_SLICE]}\n\n"
                    f"【她的生活】\n{self.world}\n\n"
                    "要求：\n"
                    "- 从 00:00 排到 24:00，段与段首尾相接，不许留空档\n"
                    "- 每段 40 分钟到 3 小时，一共 7 到 12 段\n"
                    "- 活动写具体动作，别写「自由活动」「休息」这种空话；"
                    "「用手机外放录了段和声」比「练习音乐」好\n"
                    "- doing 写成一个能接在「你正在」后面的动词短语，20 字以内，只写一个动作；"
                    "地点写 place，穿着写 outfit，谁在一起写 with，别都塞进 doing\n"
                    "- 一天里要有吃饭、犯困、发呆、刷手机、走神这些真实的部分，"
                    "别排成流水线\n"
                    "- 睡觉就写睡觉，在床上\n"
                    "- 00:00 到 07:00 基本都在睡觉；除非那天有夜间演出或熬夜赶稿，"
                    "否则别把日常活动排进凌晨\n"
                    "- 参考最近几天，别重复\n"
                    "只输出 JSON，不要任何解释或代码块标记。格式：\n"
                    '{"kind":"日程类型","segments":[{"start":"07:40","end":"09:10",'
                    '"doing":"做什么","place":"在哪","outfit":"穿什么","with":"和谁"}]}'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"日期：{day:%Y-%m-%d}（周{weekday}）。今天的类型定成「{kind}」。\n"
                    f"最近几天：{self._recentText(day)}"
                ),
            },
        ]

    @staticmethod
    def _loadJson(raw):
        """模型偶尔会套上 ```json 或者前后加话，截出中间那对花括号。"""
        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        return json.loads(text)

    def _normalize(self, raw, day):
        """把模型写的时间轴理干净：排序、去重叠、首尾补满。"""
        items = []
        for seg in raw or []:
            if not isinstance(seg, dict):
                continue
            doing = str(seg.get("doing") or "").strip()
            if not doing:
                continue
            start = _minutes(seg.get("start"))
            end = _minutes(seg.get("end"))
            if start is None:
                continue
            if end is None or end <= start:
                end = min(start + 60, 1440)
            items.append({
                "start": max(0, min(start, 1439)),
                "end": max(1, min(end, 1440)),
                "doing": doing[:MAX_DOING_CHARS],
                "place": str(seg.get("place") or "").strip()[:20],
                "outfit": str(seg.get("outfit") or "").strip()[:20],
                "with": str(seg.get("with") or "").strip()[:20],
            })
        if not items:
            return []
        items.sort(key=lambda seg: seg["start"])

        fixed = []
        cursor = 0
        for seg in items:
            if seg["start"] < cursor:  # 和上一段重叠，往后推到接上
                seg["start"] = cursor
            if seg["end"] <= seg["start"]:
                seg["end"] = min(seg["start"] + 30, 1440)
            if seg["start"] >= 1440 or len(fixed) >= MAX_SEGMENTS:
                break
            fixed.append(seg)
            cursor = seg["end"]
        if not fixed:
            return []

        # 睡觉的时间也补上，不然 00:00 到起床那段是空白，注入时找不到「此刻」
        if fixed[0]["start"] > 0:
            fixed.insert(0, {
                "start": 0, "end": fixed[0]["start"], "doing": "睡觉",
                "place": "床上", "outfit": "睡衣", "with": "",
            })
        if fixed[-1]["end"] < 1440:
            fixed.append({
                "start": fixed[-1]["end"], "end": 1440, "doing": "睡觉",
                "place": "床上", "outfit": "睡衣", "with": "",
            })
        return fixed

    def _fallback(self, day, kind):
        """模型彻底不行的时候也要有日子过，别让她一天都空着。"""
        table = [
            ("00:00", "07:30", "睡觉", "床上", "睡衣"),
            ("07:30", "08:20", "赖着不想起，躺着翻手机", "床上", "睡衣"),
            ("08:20", "09:10", "洗漱，煮了碗面", "厨房", "家居服"),
            ("09:10", "12:00", "对着电钢改一段旋律", "录音角", "家居服"),
            ("12:00", "13:10", "下楼吃面", "街角面馆", "外套"),
            ("13:10", "17:00", "在录音棚试唱", "录音棚", "外套"),
            ("17:00", "18:30", "回来的路上买了布丁", "便利店", "外套"),
            ("18:30", "20:00", "躺着不想动，随便放了首歌", "客厅", "家居服"),
            ("20:00", "22:30", "在阳台小声唱歌", "阳台", "家居服"),
            ("22:30", "24:00", "睡觉", "床上", "睡衣"),
        ]
        segments = [
            {
                "start": _minutes(start), "end": _minutes(end), "doing": doing,
                "place": place, "outfit": outfit, "with": "",
            }
            for start, end, doing, place, outfit in table
        ]
        return {
            "date": day.isoformat(), "kind": kind, "segments": segments,
            "generatedAt": int(time.time()), "fallback": True,
        }

    def generate(self, day):
        """生成某天的日程。并发安全：同一时间只跑一份。"""
        kind = self._pickKind(day)
        plan = None
        with self._genLock:
            for attempt in range(2):
                try:
                    raw = self.brain.chat(
                        self._buildPrompt(day, kind), maxTokens=1600,
                        temperature=0.9, jsonMode=True,
                    )
                    data = self._loadJson(raw)
                    segments = self._normalize(data.get("segments"), day)
                    if segments:
                        plan = {
                            "date": day.isoformat(),
                            "kind": str(data.get("kind") or kind).strip()[:10],
                            "segments": segments,
                            "generatedAt": int(time.time()),
                        }
                        break
                    log(f"{day} 第 {attempt + 1} 次生成出来的段是空的，重来")
                except Exception as exc:
                    log(f"{day} 第 {attempt + 1} 次生成失败：{exc!r}")
        if plan is None:
            plan = self._fallback(day, kind)
            log(f"{day} 用兜底模板顶上")
        self.save(day, plan)
        log(f"{day} 生成好了：「{plan['kind']}」，{len(plan['segments'])} 段"
            + ("（兜底）" if plan.get("fallback") else ""))
        return plan

    def ensure(self, now=None):
        """今天没有就补，过了设定时间就顺手把明天备好。"""
        if not self.enabled:
            return
        now = now or datetime.now(LOCAL_TZ)
        today = now.date()
        try:
            if not self.load(today):
                self.generate(today)
            if now.hour >= self.hour:
                tomorrow = today + timedelta(days=1)
                if not self.load(tomorrow):
                    self.generate(tomorrow)
        except Exception:
            log("ensure 出错：\n" + traceback.format_exc())

    # ------------------------------------------------------------ 注入
    def _segmentAt(self, segments, minutes):
        for seg in segments:
            if seg["start"] <= minutes < seg["end"]:
                return seg
        return segments[0] if segments else None

    def current(self, now=None):
        """此刻那一段（含 doing / place / outfit）。没日程返回 None。

        生图要拿「她现在在哪、穿什么」，单独开一个口子，
        免得外面去碰 _segmentAt 这种内部实现。
        """
        if not self.enabled:
            return None
        try:
            now = now or datetime.now(LOCAL_TZ)
            plan = self.load(now.date())
            segments = (plan or {}).get("segments") or []
            if not segments:
                return None
            return self._segmentAt(segments, now.hour * 60 + now.minute)
        except Exception:
            return None

    def injectText(self, now=None):
        """给系统提示的一段。没日程、没开、出任何错，都返回空字符串。"""
        if not self.enabled:
            return ""
        try:
            now = now or datetime.now(LOCAL_TZ)
            plan = self.load(now.date())
            if not plan:
                return ""
            segments = plan.get("segments") or []
            if not segments:
                return ""
            minutes = now.hour * 60 + now.minute
            current = self._segmentAt(segments, minutes)
            if current is None:
                return ""
            weekday = "一二三四五六日"[now.weekday()]

            lines = ["## 你正在过的日子"]
            lines.append(f"- 今天是 {now:%m月%d日}（周{weekday}），{plan.get('kind') or '平常的一天'}")

            action = _shortAction(current["doing"])
            if action.startswith("睡"):
                head = f"- 现在 {now:%H:%M}，你在睡觉"
            else:
                # 不写「你正在」：doing 常常以「坐在」「躺在」起头，会变成「正在坐在」
                head = f"- 现在 {now:%H:%M}，{action}"
            place = current.get("place") or ""
            if place and not _overlaps(action, place):
                head += f"，在{place}"
            if current.get("outfit"):
                head += f"，穿着{current['outfit']}"
            lines.append(head)

            index = segments.index(current)
            for seg in segments[index + 1:index + 3]:
                # 也不加「打算」：doing 常写成「弹了三段」，加了就成「打算弹了三段」
                lines.append(f"- 之后 {_clock(seg['start'])} {_shortAction(seg['doing'])}")
            if index + 1 >= len(segments) and action.startswith("睡"):
                lines.append("- 这一觉睡到明天了")

            tomorrow = self.load(now.date() + timedelta(days=1))
            if tomorrow:
                lines.append(f"- 明天是{tomorrow.get('kind') or '平常的一天'}")

            lines.append(
                "这些是你自己正在过的日子，不是任务清单：顺口提一句可以，"
                "但不要报几点几点，也不要说出「日程」「计划」「安排」「时间段」这些词。"
            )
            lines.append("主人找你有正事，先把手上这摊放下认真办，别拿「我在忙」推。")
            return "\n".join(lines)
        except Exception:
            log("注入失败：\n" + traceback.format_exc())
            return ""

    # ------------------------------------------------------------ 定时
    def startTicker(self, interval=300, delay=8):
        """开个后台线程，每隔几分钟看一眼今天/明天的日程在不在。

        启动后先等一小会儿再跑：进程刚起来时正要加载曲库、连模型，
        这会儿去抢模型最容易撞上。
        """
        if not self.enabled or self._tickerStarted:
            return
        self._tickerStarted = True

        def loop():
            time.sleep(delay)
            while True:
                try:
                    if self.isBusy():
                        # 主人在说话，让开。但别一让就是五分钟——
                        # 这一轮聊完就该补上，所以隔十几秒再探一次。
                        time.sleep(15)
                        continue
                    self.ensure()
                except Exception:
                    log("定时检查出错：\n" + traceback.format_exc())
                time.sleep(interval)

        self._ticker = threading.Thread(target=loop, daemon=True, name="schedule-ticker")
        self._ticker.start()
        log(f"定时器已启动（每 {interval} 秒检查一次，{self.hour}:00 生成明天的）")

    # ------------------------------------------------------------ 给外部的
    def view(self, day=None):
        """看某天（默认今天）的完整日程，给调试和主人查用。"""
        day = day or datetime.now(LOCAL_TZ).date()
        plan = self.load(day)
        if not plan:
            return None
        return {
            "date": plan.get("date"),
            "kind": plan.get("kind"),
            "fallback": bool(plan.get("fallback")),
            "segments": [
                {
                    "time": f"{_clock(seg['start'])}-{_clock(seg['end'])}",
                    "doing": seg["doing"],
                    "place": seg.get("place"),
                    "outfit": seg.get("outfit"),
                    "with": seg.get("with"),
                }
                for seg in plan.get("segments") or []
            ],
        }
