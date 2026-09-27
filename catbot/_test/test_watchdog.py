# -*- coding: utf-8 -*-
"""
自动重启 / 健康探针自测 —— 不依赖 QQ、不依赖模型，也不动真机器人。

覆盖：
  1. 默认值一致性（daemon.py 的兜底 vs bot.py 的 DEFAULT_CONFIG）
  2. 配置软着陆（脏值不许抛，超范围要钳制）
  3. 文件改动比对（纯函数）
  4. 心跳判定（纯函数：僵死 / 卡死 / 掉线 / 豁免期 / 旧进程）
  5. 重启策略（崩溃循环保护、每小时上限、恢复）
  6. 闭环：bot.py 产出的心跳 → daemon.py 能读、能判、判为健康
  7. 监听清单（该盯的盯上，热重载的不许盯）

运行:
    python _test/test_watchdog.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot          # noqa: E402
import daemon       # noqa: E402

# 测试进程自己记账就行，别往真的 daemon.log 里写字
daemon.log.handlers.clear()
daemon.log.addHandler(__import__("logging").NullHandler())

PASS = FAIL = 0
NOW = 1_000_000.0
TMP = Path(tempfile.mkdtemp(prefix="xiaoyou_hb_"))


def ck(name, got, expect):
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         期望: %r\n         实际: %r" % (name, expect, got))


def ck_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


def ck_has(name, got, needle):
    global PASS, FAIL
    if isinstance(got, str) and needle in got:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         期望包含: %r\n         实际: %r" % (name, needle, got))


print("\n=== 1. 默认值一致性 ===")
bot_defaults = daemon.bot_defaults()
ck_true("能从 bot.py 抽到 DEFAULT_CONFIG",
        isinstance(bot_defaults.get("watch"), dict) and "health" in bot_defaults,
        "抽到的是: %r" % (list(bot_defaults)[:6],))
for _sec, _vals in daemon._FALLBACK.items():
    for _k, _v in _vals.items():
        ck("兜底 %s.%s == bot.py 默认值" % (_sec, _k),
           bot_defaults.get(_sec, {}).get(_k), _v)


print("\n=== 2. 配置软着陆（脏值不许抛、超范围要钳） ===")
ck("_num 正常", daemon._num(5, 9), 5.0)
ck("_num 字符串数字", daemon._num("7", 9), 7.0)
ck("_num None 退默认", daemon._num(None, 9), 9)
ck("_num 乱字符退默认", daemon._num("abc", 9), 9)
ck("_num 列表退默认", daemon._num([1], 9), 9)
ck("_num NaN 退默认", daemon._num(float("nan"), 9), 9)
ck("_num inf 退默认", daemon._num(float("inf"), 9), 9)

_orig_cfg = daemon.CONFIG_FILE
fake_cfg = TMP / "config.json"
fake_cfg.write_text(json.dumps({
    "watch": {"enabled": False, "interval_sec": "oops", "settle_sec": -5,
              "cooldown_sec": 99999},
    "health": {"enabled": True, "interval_sec": 0, "hb_stale_sec": 1,
               "busy_sec": "x", "ws_down_sec": 999999, "grace_sec": -3,
               "max_per_hour": 0},
}), encoding="utf-8")
daemon.CONFIG_FILE = fake_cfg
wc, hc = daemon.watch_cfg(), daemon.health_cfg()
ck("watch.enabled 读得到关", wc["enabled"], False)
ck("watch.interval 脏值退默认 2", wc["interval"], 2.0)
ck("watch.settle 负值钳到 0", wc["settle"], 0.0)
ck("watch.cooldown 照实读", wc["cooldown"], 99999.0)
ck("health.interval 0 钳到下限 5", hc["interval"], 5.0)
ck("health.hb_stale 1 钳到下限 20", hc["hb_stale"], 20.0)
ck("health.busy 脏值退默认 600", hc["busy_max"], 600.0)
ck("health.grace 负值钳到 0", hc["grace"], 0.0)
ck("health.max_per_hour 0 钳到 1", hc["max_per_hour"], 1)
# 配置文件整个坏掉时也不能抛
fake_cfg.write_text("{ 这不是 json", encoding="utf-8")
try:
    daemon.watch_cfg()
    daemon.health_cfg()
    ck("配置损坏不抛异常", True, True)
except Exception as e:
    ck("配置损坏不抛异常", "抛了 %r" % e, True)
daemon.CONFIG_FILE = _orig_cfg


print("\n=== 3. 文件改动比对 ===")
ck("都没变", daemon.changed_files({"a": 1, "b": 2}, {"a": 1, "b": 2}), [])
ck("新增一个", daemon.changed_files({}, {"bot.py": 1.0}), ["bot.py"])
ck("改了一个", daemon.changed_files({"a": 1, "b": 2}, {"a": 1, "b": 3}), ["b"])
ck("消失一个", daemon.changed_files({"a": 1}, {}), ["a"])
ck("混合（改 + 消失 + 新增）",
   daemon.changed_files({"a": 1, "b": 1}, {"a": 2, "c": 1}), ["a", "b", "c"])

print("\n=== 4. 心跳判定（纯函数） ===")
CFG = {"enabled": True, "interval": 20.0, "hb_stale": 90.0, "busy_max": 600.0,
       "ws_down": 300.0, "grace": 120.0, "max_per_hour": 6}
HB = lambda **kw: dict({"pid": 4242, "boot": NOW - 3000, "ts": NOW,
                        "busy": 0.0, "pending": 0, "ws": True,
                        "ws_at": NOW - 3000}, **kw)
V = lambda hb, pid=4242: daemon.health_verdict(hb, expect_pid=pid, now=NOW, cfg=CFG)

ck("没有心跳文件", V(None), (False, "没有心跳文件（大脑没在写）"))
ck_has("心跳是旧进程留下的", V(HB(pid=111), pid=4242)[1], "旧进程")
ck("pid 未知时不挑刺", V(HB(), pid=0)[0], True)
ck("正常心跳 → 健康", V(HB()), (True, ""))
ck("心跳 89 秒前 → 还行", V(HB(ts=NOW - 89))[0], True)
ck_has("心跳 91 秒没更新 → 僵死", V(HB(ts=NOW - 91))[1], "没更新")
ck_has("没时间戳", V(HB(ts=0))[1], "没有时间戳")
ck_has("一条消息卡了 10 分钟", V(HB(busy=601))[1], "处理了")
ck("卡了 599 秒 → 还在容忍范围", V(HB(busy=599))[0], True)
ck_has("断连 301 秒", V(HB(ws=False, ws_at=NOW - 301))[1], "断了")
ck("断连 299 秒 → 还没到线", V(HB(ws=False, ws_at=NOW - 299))[0], True)
ck_has("从没连上过 + 起来超过 300 秒",
       V(HB(ws=False, ws_at=0, boot=NOW - 400))[1], "断了")
# 刚起来 30 秒：心跳还没写、还断着连，都不许判死（豁免期）
ck("豁免期内不判死", V(HB(boot=NOW - 30, ts=0, busy=9999, ws=False))[0], True)
ck("豁免期一过就开始判", V(HB(boot=NOW - 121, ts=0))[0], False)

# 带 alive_for / stale_pid_alive 的调用（守护进程实际用的形态）
VA = lambda hb, pid=4242, age=None, stale=False: daemon.health_verdict(
    hb, expect_pid=pid, now=NOW, cfg=CFG, alive_for=age, stale_pid_alive=stale)

# ── 新生豁免必须排在 pid 校验之前 ───────────────────────────────────────
# 一个刚被拉起的大脑要 import 一整套依赖、连 NapCat、加载语音模型，几十秒才写出
# 第一份心跳。此刻磁盘上躺着的是上一任的旧心跳（或已被清掉、干脆没有）。
# 如果这就算死刑，新大脑会在写出第一份心跳之前被杀 —— 杀了拉、拉了杀，40 秒
# 一轮的死循环。2026-09-24 晚的宕机就是这么来的。
ck("刚起来 10 秒 + pid 对不上 → 不判死", VA(HB(pid=111), pid=4242, age=10.0)[0], True)
ck("刚起来 10 秒 + 连心跳文件都没有 → 不判死", VA(None, pid=4242, age=10.0)[0], True)
ck("刚起来 10 秒 + 忙了 9999 秒 → 也先不判（豁免期说了算）",
   VA(HB(busy=9999), pid=4242, age=10.0)[0], True)
ck("活了 119 秒 → 还在豁免期内", VA(HB(pid=111), pid=4242, age=119.0)[0], True)
ck("活了 121 秒 → 出豁免期，pid 对不上照样判",
   VA(HB(pid=111), pid=4242, age=121.0)[0], False)
ck_has("出豁免期后措辞还是「旧进程」",
       VA(HB(pid=111), pid=4242, age=121.0)[1], "旧进程")
ck("不知道活了多久（alive_for=None）→ 按老规矩判，不豁免",
   VA(HB(pid=111), pid=4242, age=None)[0], False)
ck("alive_for 是负数（时钟回拨）→ 不豁免",
   VA(HB(pid=111), pid=4242, age=-5.0)[0], False)

# ── 关键分辨：心跳的 pid 是个"还活着的前任" ────────────────────────────
# 这时真正的病灶是那个前任（要杀的是它），不能把新大脑当替罪羊。
ck("前任还活着 → 仍判不健康（要动手，但动的是它）",
   VA(HB(pid=111), pid=4242, age=999.0, stale=True)[0], False)
ck_has("前任还活着 → 措辞点明「没死干净」，不是含糊的「旧进程」",
       VA(HB(pid=111), pid=4242, age=999.0, stale=True)[1], "没死干净")
ck_has("前任已死 → 保持老措辞",
       VA(HB(pid=111), pid=4242, age=999.0, stale=False)[1], "旧进程")


print("\n=== 4b. pid_age / 进程探测（不认识就说不认识，别硬猜） ===")
ck("pid=0 → None", daemon.pid_age(0), None)
_busy_pids = {p for p, _a, _b in daemon.list_processes()}
_ghost = max(_busy_pids) + 4242 if _busy_pids else 999999
ck("不存在的 pid → None（绝不拿文件 mtime 充数）", daemon.pid_age(_ghost), None)
ck_true("自己的 pid 活得 ≥ 0 秒", (daemon.pid_age(os.getpid()) or -1) >= 0,
        "pid_age(%d)=%r" % (os.getpid(), daemon.pid_age(os.getpid())))
ck_true("进程快照能拿到自己",
        any(p == os.getpid() for p, _pp, _n in daemon.list_processes()),
        "快照 %d 条" % len(daemon.list_processes()))
ck_true("proc_exe(自己) 指向 python 解释器",
        "python" in daemon.proc_exe(os.getpid()).lower(),
        daemon.proc_exe(os.getpid()))
ck("proc_exe(不存在的 pid) → 空串", daemon.proc_exe(_ghost), "")
ck("parent_of(不存在的 pid) → 0", daemon.parent_of(_ghost), 0)


print("\n=== 4c. 认出「没死干净的前任」 ===")
_save = {}
def _mon(name, val):
    _save.setdefault(name, getattr(daemon, name))
    setattr(daemon, name, val)

_mon("read_pid", lambda name: 0)
_mon("pid_alive", lambda pid: pid in (100, 200))
_mon("proc_create_time", lambda pid: {100: NOW - 9000, 200: NOW - 100}.get(pid, 0.0))

_mon("read_heartbeat", lambda: None)
ck("没有心跳 → 认不出前任", daemon._stray_brain_pid(200), 0)
_mon("read_heartbeat", lambda: {"pid": 100, "ts": NOW})
ck("心跳的 pid 就是当前这个 → 不是前任", daemon._stray_brain_pid(100), 0)
ck("心跳的 pid 比当前老、且还活着 → 认出来", daemon._stray_brain_pid(200), 100)
_mon("pid_alive", lambda pid: pid == 200)
ck("前任已经死了 → 认不出（没什么可清）", daemon._stray_brain_pid(200), 0)
_mon("pid_alive", lambda pid: pid in (100, 200))
_mon("proc_create_time", lambda pid: {100: NOW - 1, 200: NOW - 100}.get(pid, 0.0))
ck("pid 是被系统回收来的（比当前这个还新）→ 不动它",
   daemon._stray_brain_pid(200), 0)
_mon("proc_create_time", lambda pid: {100: NOW + 60, 200: NOW - 100}.get(pid, 0.0))
_mon("read_heartbeat", lambda: {"pid": 100, "ts": NOW - 9000})
ck("pid 在心跳写下时还不存在 → 不是写心跳的那个，不动它",
   daemon._stray_brain_pid(200), 0)
_mon("read_heartbeat", lambda: {"pid": os.getpid(), "ts": NOW})
ck("心跳的 pid 是守护进程自己 → 不动", daemon._stray_brain_pid(200), 0)
_mon("read_pid", lambda name: os.getpid() if name == "daemon" else 0)
ck("心跳的 pid 就是在册的守护进程 → 不动", daemon._stray_brain_pid(200), 0)
for _k, _v in _save.items():
    setattr(daemon, _k, _v)


print("\n=== 5. 重启策略 ===")
p = daemon.RestartPolicy(max_per_hour=2)
ck("还没启动过 → 不计数", p.note_dead(10.0), False)
p.note_start(10.0)
ck("起来就死 第 1 次 → 不停手", p.note_dead(20.0), False)
p.note_start(20.0)
ck("起来就死 第 2 次 → 不停手", p.note_dead(30.0), False)
p.note_start(30.0)
ck("起来就死 第 3 次 → 停手", p.note_dead(40.0), True)
ck("停手期间不许重启", p.allow(40.0, "health")[0], False)
ck_has("并且说明原因", p.allow(40.0, "health")[1], "暂停自动重启")
ck("停手时间过后放行", p.allow(400.0, "health")[0], True)

p2 = daemon.RestartPolicy(max_per_hour=2)
ck("配额内第 1 次", p2.allow(1000.0, "health")[0], True)
p2.note_restart(1000.0, "health")
ck("配额内第 2 次", p2.allow(1100.0, "health")[0], True)
p2.note_restart(1100.0, "health")
ck("超出一小时上限 → 拦住", p2.allow(1200.0, "health")[0], False)
ck_has("说明是一小时上限", p2.allow(1200.0, "health")[1], "一小时")
ck("改完自动重启不受这个上限管", p2.allow(1200.0, "watch")[0], True)
ck("滑出窗口后恢复", p2.allow(4601.0, "health")[0], True)

p3 = daemon.RestartPolicy(max_per_hour=6)
p3.note_start(0.0)
p3.note_dead(0.5)
p3.note_start(0.5)
p3.note_dead(1.0)
p3.note_start(1.0)
p3.note_dead(1.5)                   # 连着三次 → 停手
p3.note_start(2.0)
p3.note_alive(70.0)                 # 活够 60 秒了 → 恢复
ck("活得够久就解除停手", p3.blocked_until, 0.0)
ck("恢复后能再重启", p3.allow(80.0, "health")[0], True)


print("\n=== 6. 闭环：bot.py 产出 → daemon.py 判定 ===")
# 用 __new__ 绕开 __init__：那会建 LLM / OneBot / 工具链，测试不需要
f = bot.CatBot.__new__(bot.CatBot)
f._boot_at = NOW - 3000
f._busy_lock = threading.Lock()
f._inflight = []
f._submitted = 5
f._finished = 5
f._ws_connected = True
f._ws_since = NOW - 3000

hb = f._heartbeat_payload()
for key in ("pid", "boot", "ts", "busy", "pending", "done", "ws", "ws_at"):
    ck_true("心跳里有 %s" % key, key in hb, "实际字段: %r" % (list(hb),))
ck("空闲时 busy = 0", hb["busy"], 0.0)
ck("空闲时没排队", hb["pending"], 0)
ck("心跳的 pid 是本进程", hb["pid"], os.getpid())

# 让 daemon 用真实时间来判：心跳是新写的，必须判为健康
_real_now = time.time()
f._boot_at = _real_now - 3000
f._ws_since = _real_now - 3000
hb = f._heartbeat_payload()
ck("bot 现写的心跳 → daemon 判为健康",
   daemon.health_verdict(hb, expect_pid=os.getpid(), now=_real_now,
                         cfg=daemon.health_cfg()), (True, ""))

# 模拟"两条任务都卡住"：在跑的那条已经跑了 20 分钟
with f._busy_lock:
    f._inflight.append(_real_now - 1200)
hb2 = f._heartbeat_payload()
ck_true("忙了 20 分钟 → busy 反映出来", hb2["busy"] >= 1199.0, "busy=%r" % hb2["busy"])
ck_has("daemon 判到卡死",
       daemon.health_verdict(hb2, expect_pid=os.getpid(), now=_real_now,
                             cfg=daemon.health_cfg())[1], "处理了")
with f._busy_lock:
    f._inflight.clear()
    f._finished += 1
ck("跑完之后 busy 归零", f._heartbeat_payload()["busy"], 0.0)

# 真跑一遍写心跳的线程，确认落地的是能读的 JSON、而且不留 .tmp
_orig_hb, _orig_every = bot.HEARTBEAT, bot.HEARTBEAT_EVERY
bot.HEARTBEAT = TMP / "bot.heartbeat"
bot.HEARTBEAT_EVERY = 0.2
f._running = True
f._submitted = f._finished = 0
th = threading.Thread(target=f._heartbeat_worker, daemon=True)
th.start()
ok = False
for _ in range(40):
    time.sleep(0.05)
    if bot.HEARTBEAT.exists() and bot.HEARTBEAT.stat().st_size > 0:
        ok = True
        break
ck_true("心跳文件写出来了", ok, "等了 2 秒还没有")
f._running = False
th.join(timeout=1.0)

_orig_daemon_hb = daemon.HB_FILE
daemon.HB_FILE = bot.HEARTBEAT
ck_true("daemon 读得懂这份心跳", isinstance(daemon.read_heartbeat(), dict),
        "读到: %r" % (daemon.read_heartbeat(),))
ck("心跳文件不留 .tmp 残渣",
   sorted(p.name for p in TMP.iterdir() if p.name.endswith(".tmp")), [])
daemon.HB_FILE = _orig_daemon_hb

# 写不出去的时候：线程绝不能死。它一死，守护进程就永远等不到心跳，
# 于是把她当僵死一直重启 —— 自愈功能反把自己带崩。
bot.HEARTBEAT = TMP / "不存在的目录" / "bot.heartbeat"
bot.HEARTBEAT_EVERY = 0.05
f._running = True
_th = threading.Thread(target=f._heartbeat_worker, daemon=True)
_th.start()
time.sleep(0.3)
_alive = _th.is_alive()
f._running = False
_th.join(timeout=1.0)
ck_true("心跳写不出去也不许把线程弄死", _alive, "线程死了")

bot.HEARTBEAT, bot.HEARTBEAT_EVERY = _orig_hb, _orig_every

_bad = TMP / "broken.heartbeat"
_bad.write_text("{半截 json", encoding="utf-8")
daemon.HB_FILE = _bad
ck("半截 JSON 读成 None（不抛）", daemon.read_heartbeat(), None)
daemon.HB_FILE = _orig_daemon_hb


print("\n=== 6b. ensure_bot：三条防「自杀」的规矩 ===")
_s2 = {}
def _mon2(name, val):
    _s2.setdefault(name, getattr(daemon, name))
    setattr(daemon, name, val)

_killed, _started, _marks = [], [], []
_mon2("BOT_RESTART_GAP", 0.0)
_mon2("kill_pid", lambda pid: _killed.append(pid))
_mon2("clear_pid", lambda name: _marks.append(("clear_pid", name)))
_mon2("clear_heartbeat", lambda: _marks.append(("clear_hb",)))

def _reset_bot(*, botpid, alive, hb, ages):
    _killed.clear(); _started.clear(); _marks.clear()
    daemon._policy = None
    daemon.read_pid = lambda name: botpid if name == "bot" else 0
    daemon.pid_alive = lambda pid: pid in alive
    daemon.bot_alive = lambda: botpid in alive
    daemon.read_heartbeat = lambda: hb
    daemon.proc_create_time = lambda pid: ages.get(pid, 0.0)
    daemon.pid_age = lambda pid, now=None: ages.get(pid, 0.0)

_mon2("start_bot", lambda: [_started.append(1), True][1])
_mon2("health_cfg", lambda: CFG)
_NOW2 = time.time()

# ① 前任没死干净：该杀的是它，当前这个不许动
#    这就是 2026-09-24 晚那个 40 秒死循环的真身 —— 心跳是 100 写的，而 100 还活着。
_reset_bot(botpid=200, alive={100, 200},
           hb={"pid": 100, "ts": _NOW2, "boot": _NOW2 - 9000, "busy": 0.0,
               "pending": 0, "ws": True, "ws_at": _NOW2 - 9000},
           ages={100: _NOW2 - 9000, 200: _NOW2 - 100})
ck("前任活着 → 清掉它", daemon.ensure_bot("测试：前任还在写心跳", "health"),
   "已清掉上一任大脑 pid=100")
ck("清的就是前任 100", _killed, [100])
ck("没有重启当前这个", _started, [])
ck_true("顺手把旧心跳清了（免得下一轮又认错）", ("clear_hb",) in _marks, _marks)

# ② 「判死」结论已过期（它刚被别人换新过）→ 不许动手
_reset_bot(botpid=300, alive={300},
           hb={"pid": 300, "ts": _NOW2, "boot": _NOW2 - 5, "busy": 0.0,
               "pending": 0, "ws": True, "ws_at": _NOW2 - 5},
           ages={300: 5.0})
ck("结论过期 → 不重启", daemon.ensure_bot("健康探针：心跳是旧进程", "health"), "")
ck("没杀它", _killed, [])
ck("没起第二个", _started, [])

# ③ 真的卡死（活了很久、心跳几百秒没更新）→ 照常重启
_reset_bot(botpid=400, alive={400},
           hb={"pid": 400, "ts": _NOW2 - 500, "boot": _NOW2 - 5000, "busy": 0.0,
               "pending": 0, "ws": True, "ws_at": _NOW2 - 5000},
           ages={400: 5000.0})
ck("真卡死 → 重启", daemon.ensure_bot("健康探针：心跳已经 500 秒没更新", "health"),
   "已重启")
ck("杀了旧的 400", _killed, [400])
ck_true("清了 pid 文件", ("clear_pid", "bot") in _marks, _marks)
ck_true("清了心跳", ("clear_hb",) in _marks, _marks)
ck("起了新的", _started, [1])

daemon._policy = None
for _k, _v in _s2.items():
    setattr(daemon, _k, _v)


print("\n=== 6c. 清场函数：只认自己人，不误伤别人的 Python ===")
_live_pids = daemon.project_pids()
ck_true("项目进程清单取得到", isinstance(_live_pids, list), _live_pids)
ck_true("清单里全是 pythonw（协议层 node 不在内）",
        all("pythonw" in daemon.proc_exe(p).lower() for p in _live_pids),
        [(p, daemon.proc_exe(p)) for p in _live_pids])

# 防回归：kill_pid 一旦带 /T，就会把 NapCat（守护进程的亲儿子）一起带走。
# 2026-09-25 踩过一次，端口 3000/3001 当场没了。
_src = Path(daemon.__file__).read_text(encoding="utf-8")
_body = _src[_src.index("def kill_pid"):_src.index("def ensure_bot")]
# 只看代码行：注释里提到这个选项是在解释"为什么不能用"，不算违规
_code = "\n".join(ln for ln in _body.splitlines() if not ln.strip().startswith("#"))
ck_true("kill_pid 的代码里不许出现 taskkill /T", "/T" not in _code, _code.strip())
ck_true("kill_pid 仍然是 taskkill /F", '"/F"' in _code, _code.strip())

_mon2("kill_pid", lambda pid: _killed.append(pid))
_mon2("project_pids", lambda: [4242])
_mon2("proc_create_time", lambda pid: time.time())
_killed.clear()
ck("刚创建的进程先不动（可能正被拉起来）", daemon.sweep_strays(keep=set()), [])
ck("也就真的没杀", _killed, [])
_mon2("proc_create_time", lambda pid: time.time() - 9999)
ck("够老的才清", daemon.sweep_strays(keep=set()), [4242])
_killed.clear()
ck("keep 名单里的一律不动", daemon.sweep_strays(keep={4242}), [])
ck("也就真的没杀", _killed, [])
for _k, _v in _s2.items():
    setattr(daemon, _k, _v)


print("\n=== 7. 崩溃日志尾巴（混合编码） ===")
_ct = TMP / "console.log"
_ct.write_bytes("13:00:00 [INFO] 旧的一行（GBK 写的）\n".encode("gbk")
                + "13:00:01 [INFO] 新的一行（UTF-8 写的）\n".encode("utf-8")
                + "SyntaxError: invalid syntax\n".encode("utf-8"))
_orig_ct = daemon.BOT_CONSOLE
daemon.BOT_CONSOLE = _ct
_tail = daemon._crash_tail()
ck_true("GBK 老行不乱码", "旧的一行" in _tail, _tail)
ck_true("UTF-8 新行不乱码", "新的一行" in _tail, _tail)
ck_true("崩溃原因带得出来", "SyntaxError" in _tail, _tail)
daemon.BOT_CONSOLE = TMP / "不存在的日志.log"
ck("日志不存在时返回空串（不抛）", daemon._crash_tail(), "")
daemon.BOT_CONSOLE = _orig_ct


print("\n=== 8. 监听清单 ===")
for name in ("bot.py", "tools.py", "voice.py", "corpus.py", "config.json"):
    ck_true("盯 %s" % name, name in daemon.WATCH_BOT_FILES)
ck("不盯 persona.md（存盘即生效）",
   "persona.md" in daemon.WATCH_BOT_FILES, False)
ck("不盯 memes.md（存盘即生效）",
   "memes.md" in daemon.WATCH_BOT_FILES, False)
ck("daemon.py 单独一类（换了要重启自己）", daemon.WATCH_DAEMON_FILES, ("daemon.py",))
ck("实际扫到的文件 == 清单",
   sorted(daemon.watch_files()), sorted(daemon.WATCH_BOT_FILES + daemon.WATCH_DAEMON_FILES))
ck_true("mtime 是数字",
        all(isinstance(v, float) for v in daemon.watch_files().values()))

print("\n=== 9. 主循环必须能被叫停（换班时旧守护进程得真的退出） ===")
# 这条是 2026-09-25「僵尸守护进程链」的根：主循环原先写的是 while True +
# time.sleep，看门线程 set(_stop) 对它毫无作用 —— 每一代守护进程都永远活着，
# 十几代叠在一起抢着重启同一个大脑。
_s3 = {}
def _mon3(name, val):
    _s3.setdefault(name, getattr(daemon, name))
    setattr(daemon, name, val)

_mon3("write_pid", lambda name, pid: None)
_mon3("pid_alive", lambda pid: False)
_mon3("read_pid", lambda name: 0)
_mon3("tick", lambda: None)
_mon3("everything_ready", lambda: False)
_mon3("sweep_strays", lambda keep=None: [])
_mon3("_watch_worker", lambda: None)
_mon3("_health_worker", lambda: None)
_mon3("HEARTBEAT", TMP / "daemon.heartbeat")
_mon3("POLL_FAST", 0.05)
_mon3("POLL_SECONDS", 0.05)
daemon._handed_over = True          # 别让它去删真的 run/daemon.pid
daemon._stop.clear()
_loop = threading.Thread(target=daemon.main, kwargs={"takeover": True}, daemon=True)
_loop.start()
time.sleep(0.4)
ck_true("主循环跑起来了", _loop.is_alive(), "线程压根没起来")
daemon._stop.set()
_loop.join(timeout=3.0)
ck_true("_stop 一置位就立刻退出（否则又叠出一代僵尸守护进程）",
        not _loop.is_alive(), "3 秒了还在跑")
daemon._handed_over = False
for _k, _v in _s3.items():
    setattr(daemon, _k, _v)

shutil.rmtree(TMP, ignore_errors=True)

print("\n%s" % ("=" * 50))
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 50)
sys.exit(1 if FAIL else 0)
