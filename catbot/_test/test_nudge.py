"""冷场暖场（nudge）的闸门测试。

每个场景只放开一道闸门、其余保持关闭 —— 这样一旦不符合预期，
就能立刻指出是**哪一条规则**没生效，而不是笼统地说"她没说话"。

只有「应该触发」的两条会真正调用模型（要验文案质量），其余都在闸门处被拦下。
"""
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import bot as botmod        # noqa: E402


OWNER = "10001"
GROUP = "610086723"
OUT = []


def log(s=""):
    print(s)
    OUT.append(str(s))


def seg_text(segs):
    return "".join(s.get("data", {}).get("text", "") for s in segs)


class FakeAPI:
    def __init__(self):
        self.sent = []
        self.self_id = "1234567890"
        self.ws_url = "ws://127.0.0.1:3001"
        self.token = ""
        self.http_url = "http://127.0.0.1:3000"

    def send_private(self, uid, segs):
        self.sent.append(("private", str(uid), segs))

    def send_group(self, gid, segs):
        self.sent.append(("group", str(gid), segs))


BASE_CFG = {
    "enabled": True,
    "silence_min": 45,
    "silence_max": 240,
    "cooldown": 40,
    "private_max_per_day": 3,
    "group_enabled": True,
    "group_silence_min": 60,
    "group_max_per_day": 1,
    "quiet_hours": ["23:30", "09:00"],
    "after_greet_grace": 3600,
}

log("=== 准备 ===")
tmp = Path(tempfile.mkdtemp(prefix="nudgetest_"))
# 隔离一切写盘：暖场状态现在会落盘（run/nudge_state.json），
# 必须在构造 CatBot **之前**改掉路径 —— `__init__` 里就会读它。
# 不改的话，跑一次测试就会把线上真实状态读进来 / 写出去。
botmod.NUDGE_STATE = tmp / "nudge_state.json"
botmod.GREET_STATE = tmp / "last_greet.txt"
b = botmod.CatBot()
b.api = FakeAPI()


class FakeLLM:
    """假模型：不联网、不要 Key，直接回一句固定的话。

    暖场文案本身好不好不是这里要验的 —— 这里只验"该不该开口"的闸门。
    用假模型可以让这份测试**自包含**：别人克隆仓库后不填 Key 也能跑通。
    """

    model = "fake"
    api_base = "(本地假模型)"

    def chat(self, system_prompt, history, user_line):
        return "（探头）主人还在嘛～"


b.llm = FakeLLM()
b.memory.dir = tmp
b.memory._cache.clear()
# 主人是谁必须由测试自己钉死：这份测试验的是闸门逻辑，
# 不该受用户 config.json 里 owner_qq 填没填影响（别人刚克隆下来多半是空的）。
b.cfg.data["proactive"]["owner_qq"] = OWNER
# 假装大脑已经起来 10 分钟：_nudge_tick 里有一道「刚启动 3 分钟不暖场」的闸门，
# 不放开它，下面所有"应该触发"的用例都会被挡住（那是给重启场景准备的，不是这里）。
b._boot_at = time.time() - 600
# 造一点真实上文，让暖场文案有"尾巴"可以接
b.memory.append(f"p{OWNER}", "今天改代码改得好累啊",
                "主人辛苦啦，快过来让小柚呼噜呼噜（尾巴缠上来）")
b.memory.append(f"p{OWNER}", "我先去洗个澡", "去吧去吧，小柚在这儿等你回来喵")
log(f"  模型    : {b.llm.model} @ {b.llm.api_base}")
log(f"  记忆隔离: {tmp}")
log()

fails = []


def case(name, expect_hit, *, key, idle_min, cfg=None, greet_age=None,
         proactive_off=False, ws=True, **st_extra):
    b._active.clear()
    b.api.sent.clear()
    b._ws_connected = ws
    b._last_greet_ts = (time.time() - greet_age) if greet_age else 0.0

    n = dict(BASE_CFG)
    if cfg:
        n.update(cfg)
    b.cfg.data["proactive"]["nudge"] = n
    b.cfg.data["proactive"]["enabled"] = not proactive_off

    st = {"at": time.time() - idle_min * 60, "day": "", "count": 0,
          "nudge_at": 0.0, "engaged": False}
    st.update(st_extra)
    b._active[key] = st

    b._nudge_tick()
    got = bool(b.api.sent)
    ok = (got == expect_hit)
    if not ok:
        fails.append(name)
    log(f"{'OK ' if ok else '!! '}{name:<32} 期望"
        f"{'触发' if expect_hit else '不动'} → 实际{'触发' if got else '不动'}")
    if got:
        chan, target, segs = b.api.sent[0]
        log(f"      {chan} {target}: {seg_text(segs)}")
    return ok


log("=== A. 什么时候算冷场 ===")
case("静默 10 分钟（还早）", False, key=f"p{OWNER}", idle_min=10)
case("静默 50 分钟（该开口了）", True, key=f"p{OWNER}", idle_min=50)
case("静默 5 小时（那是散了）", False, key=f"p{OWNER}", idle_min=300)

log()
log("=== A2. 协议层断开时不开口 ===")
# 掉线时如果照暖，文案生成要花一次推理钱、还会把"刚暖过"的冷却和当天配额
# 一起吃掉 —— 等真连上了反而没得聊。实测线上踩过：断线一小时里白暖了 3 次。
case("协议层断开（说了也发不出去）", False, key=f"p{OWNER}", idle_min=50, ws=False)
case("协议层恢复后照常暖", True, key=f"p{OWNER}", idle_min=50, ws=True)

log()
log("=== A3. 早安也受同一条约束（纯判断，不调模型）===")
# 早安比暖场更要紧：它有个"先落盘再发"的防重发机制。断线时要是落盘了，
# 就等于"今天已经发过"，主人当天再也收不到 —— 所以断线时必须整条判掉、且不落盘。


def greet_case(name, expect, *, clock, ws, last_day=""):
    b._last_greet_day = last_day
    b._ws_connected = ws
    got = b._greet_due(datetime(2026, 9, 25, clock[0], clock[1]), "08:00")
    ok = got == expect
    if not ok:
        fails.append(name)
    log(f"{'OK ' if ok else '!! '}{name:<30} 期望"
        f"{'发' if expect else '不发'} → 实际{'发' if got else '不发'}")


greet_case("09:00 · 没发过 · 连着", True, clock=(9, 0), ws=True)
greet_case("07:00 · 还没到点", False, clock=(7, 0), ws=True)
greet_case("09:00 · 今天已经发过", False, clock=(9, 0), ws=True,
           last_day="2026-09-25")
greet_case("09:00 · 协议层断着（留给连上后补发）", False, clock=(9, 0), ws=False)
b._last_greet_day = ""
b._ws_connected = True

log()
log("=== B. 频率控制 ===")
case("5 分钟前刚暖过一次", False, key=f"p{OWNER}", idle_min=50,
     nudge_at=time.time() - 300)
case("今天已经暖满 3 次", False, key=f"p{OWNER}", idle_min=50,
     day=datetime.now().strftime("%Y-%m-%d"), count=3)
case("早安刚发出去 10 分钟", False, key=f"p{OWNER}", idle_min=50, greet_age=600)
case("总闸关掉（proactive.enabled）", False, key=f"p{OWNER}", idle_min=50,
     proactive_off=True)

log()
log("=== C. 群聊更克制 ===")
case("群里从没理过她", False, key=f"g{GROUP}", idle_min=120)
case("群里参与过，静默 30 分钟", False, key=f"g{GROUP}", idle_min=30, engaged=True)
case("群里参与过，静默 70 分钟", True, key=f"g{GROUP}", idle_min=70, engaged=True)
case("群里今天已暖过 1 次", False, key=f"g{GROUP}", idle_min=70, engaged=True,
     day=datetime.now().strftime("%Y-%m-%d"), count=1)

log()
log("=== D. 不打扰无关的人 ===")
case("陌生人的私聊冷场了", False, key="p99999999", idle_min=120)

log()
log("=== E. 免打扰时段（用当前时刻反推一段窗口）===")
_now = datetime.now()
_h0 = (_now.hour) % 24
_qh = [f"{_h0:02d}:00", f"{(_h0 + 2) % 24:02d}:00"]
case(f"当前时刻落进 {_qh[0]}~{_qh[1]}", False, key=f"p{OWNER}", idle_min=50,
     cfg={"quiet_hours": _qh})

log()
log("=== F. 免打扰时段判定（跨零点这类边界）===")
for t, qh, want in [
    (datetime(2026, 9, 24, 23, 45), ["23:30", "09:00"], True),
    (datetime(2026, 9, 24, 3, 0), ["23:30", "09:00"], True),
    (datetime(2026, 9, 24, 8, 59), ["23:30", "09:00"], True),
    (datetime(2026, 9, 24, 9, 0), ["23:30", "09:00"], False),
    (datetime(2026, 9, 24, 10, 0), ["09:00", "12:00"], True),
    (datetime(2026, 9, 24, 13, 0), ["09:00", "12:00"], False),
    (datetime(2026, 9, 24, 10, 0), None, False),
    (datetime(2026, 9, 24, 10, 0), ["00:00", "00:00"], False),
]:
    got = b._quiet_now(t, qh)
    ok = (got == want)
    if not ok:
        fails.append(f"quiet {t.time()} {qh}")
    log(f"{'OK ' if ok else '!! '}{str(t.time()):<9} qh={str(qh):<18} "
        f"期望{'静默' if want else '可发言'} → 实际{'静默' if got else '可发言'}")

log()
log("=== G. 活跃度记录（谁的消息算数）===")
b.pool.submit = lambda *a, **k: None      # 拦掉真实处理，只看记账


def feed(uid, mt, gid=None, mid=1):
    ev = {"post_type": "message", "message_type": mt, "user_id": str(uid),
          "self_id": "1234567890", "message_id": mid, "message": []}
    if gid:
        ev["group_id"] = gid
    b._on_event(json.dumps(ev))


checks = []
b._active.clear()
feed(OWNER, "private", mid=1)
checks.append(("别人发私聊 → 记活跃", f"p{OWNER}" in b._active))
b._active.clear()
feed("1234567890", "private", mid=2)
checks.append(("机器人自己发的 → 不记", f"p1234567890" not in b._active))
b._active.clear()
feed("111", "group", gid=GROUP, mid=3)
checks.append(("群里任何人说话 → 记活跃", f"g{GROUP}" in b._active))
b._active.clear()
feed(OWNER, "private", mid=4)
feed(OWNER, "private", mid=4)
checks.append(("同一条消息重复推送 → 只记一次", True))
# engaged 只在回过话时置位
b._active.clear()
b._touch(f"p{OWNER}")
checks.append(("仅收到消息 → 不算参与过", not b._active[f"p{OWNER}"]["engaged"]))
b._touch(f"p{OWNER}", engaged=True)
checks.append(("回过话 → 标记参与过", b._active[f"p{OWNER}"]["engaged"]))

for name, ok in checks:
    if not ok:
        fails.append(name)
    log(f"{'OK ' if ok else '!! '}{name}")

log()
fchecks = []
log("=== F. 暖场状态落盘（重启不丢「回过话」标记）===")
# 这一段守的是 2026-09-25 那个真实故障：状态只在内存里 → 大脑一重启，
# 「她在这个群回过话」就清零 → 群暖场静默失效（日志里一句错都不报）。
STATE = botmod.NUDGE_STATE
if STATE.exists():
    STATE.unlink()


def reload_bot(fresh_boot: bool = False):
    """新造一个大脑 = 模拟一次重启。

    fresh_boot=True 时保留「刚刚才起来」的启动时刻 —— 测「启动 3 分钟内
    不暖场」那条闸门要用它。默认挪到 10 分钟前，好让别的用例不被那道闸门挡住。
    """
    nb = botmod.CatBot()
    nb.api = FakeAPI()
    nb.llm = FakeLLM()
    nb.cfg.data["proactive"]["owner_qq"] = OWNER
    nb.memory.dir = tmp
    nb.memory._cache.clear()
    if not fresh_boot:
        nb._boot_at = time.time() - 600
    return nb


b._active.clear()
b._touch(f"g{GROUP}")                       # 只收到消息，没回过话
b._touch(f"g{GROUP}", engaged=True)         # 回过话了 → force 落盘
b._save_nudge_state(force=True)
fchecks.append(("状态文件已生成", STATE.exists()))

raw = json.loads(STATE.read_text(encoding="utf-8"))
fchecks.append(("文件里有这个群", f"g{GROUP}" in (raw.get("sessions") or {})))
fchecks.append(("engaged 被写进去了",
               bool(raw["sessions"][f"g{GROUP}"].get("engaged"))))

b2 = reload_bot()
st = b2._active.get(f"g{GROUP}") or {}
fchecks.append(("重启后恢复了这个群", bool(st)))
fchecks.append(("重启后 engaged 还在（这是关键）", bool(st.get("engaged"))))
fchecks.append(("重启后 at 还在", float(st.get("at") or 0) > 0))

# 没有 force 的普通消息：靠 20 秒节流，不该每条都写盘
b2._active.clear()
b2._save_nudge_state(force=True)
before = STATE.stat().st_mtime_ns
b2._touch(f"g{GROUP}")                      # 节流内，不该写
fchecks.append(("节流生效：紧跟着的普通消息不写盘",
               STATE.stat().st_mtime_ns == before))

# 文件坏了 / 不在 → 当作全新开始，绝不抛异常
STATE.write_text("{坏掉的 json", encoding="utf-8")
try:
    b3 = reload_bot()
    fchecks.append(("状态文件损坏时照常启动（当全新开始）", True))
    fchecks.append(("损坏时不会带出脏数据", f"g{GROUP}" not in b3._active))
except Exception as e:
    fchecks.append((f"状态文件损坏时照常启动（实际抛了 {type(e).__name__}）", False))
STATE.unlink()

# 超过 7 天的会话没暖场价值（silence_max 最多几小时），加载时就该丢掉
old = time.time() - 8 * 86400
STATE.write_text(json.dumps({"version": 1, "sessions": {
    "g999": {"at": old, "day": "", "count": 0, "nudge_at": 0, "engaged": True},
    "g888": {"at": time.time() - 60, "day": "", "count": 0,
             "nudge_at": 0, "engaged": True}}}), encoding="utf-8")
b4 = reload_bot()
fchecks.append(("7 天前的会话不恢复", "g999" not in b4._active))
fchecks.append(("60 秒前的会话恢复", "g888" in b4._active))

# 启动后 3 分钟内不暖场（避免"重启即冒泡"）
def _fresh_state():
    """一条「静默 60 分钟、她回过话」的私聊记录 —— 窗口是开着的。"""
    return {f"p{OWNER}": {"at": time.time() - 60 * 60, "day": "", "count": 0,
                          "nudge_at": 0.0, "engaged": True}}


b5 = reload_bot(fresh_boot=True)           # ← 刚刚才起来
b5._active = _fresh_state()
b5._ws_connected = True
b5.cfg.data["proactive"]["enabled"] = True
b5.cfg.data["proactive"]["nudge"] = dict(BASE_CFG)
b5.api.sent.clear()
b5._nudge_tick()
fchecks.append(("刚启动 3 分钟内不暖场", not b5.api.sent))

# 重置状态再试。上一次要是真发出去了，会把冷却和当天配额一起吃掉 ——
# 那样这一条会因为「冷却」而通过，测不到「启动宽限」这件事本身。
b5._active = _fresh_state()
b5._boot_at = time.time() - 600
b5.api.sent.clear()
b5._nudge_tick()
fchecks.append(("过了 3 分钟照常暖", bool(b5.api.sent)))

for name, ok in fchecks:
    if not ok:
        fails.append(name)
    log(f"{'OK ' if ok else '!! '}{name}")

log()
log("=== 结果 ===")
if fails:
    log(f"失败 {len(fails)} 项：{fails}")
else:
    log("全部通过")

Path(BASE / "_test" / "nudge_out.txt").write_text("\n".join(OUT), encoding="utf-8")
sys.exit(1 if fails else 0)
