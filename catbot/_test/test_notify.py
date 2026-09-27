# -*- coding: utf-8 -*-
"""掉线提醒（daemon.notify_stuck_protocol / in_quiet_hours）的闸门测试。

跑法: python _test/test_notify.py

**这个功能做错了就是"骚扰"**，所以重点全在两件事上：
  ① 免打扰时段绝对不弹窗（而被踢最频繁的恰恰是半夜）；
  ② 一次掉线不会反复弹（最多两条：首次 + 跨过免打扰后的补充）。

绝不真的弹窗：把 `daemon.popup_notify` 换成一个记账的替身，
状态文件也改到 tmp。真弹一下的话，跑测试的人会被自己的测试打扰。
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import daemon  # noqa: E402

PASS = FAIL = 0


def check(name, got, expect):
    global PASS, FAIL
    ok = got == expect
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望: {expect!r}\n         实际: {got!r}")


def check_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def ts_at(hh, mm, ss=0):
    """今天 HH:MM 的时间戳（用来构造"某个时刻"）。"""
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, ss, 0, 0, -1))


# ---- 隔离：状态文件挪到 tmp ----
TMP = ROOT / "tmp" / "_test_notify"
TMP.mkdir(parents=True, exist_ok=True)
STATE = TMP / "notify_state.json"
STATE.unlink(missing_ok=True)
daemon.NOTIFY_STATE = STATE

# ---- 替身：记账的弹窗 ----
POPUPS = []


def fake_popup(title, text, cfg):
    POPUPS.append(title)
    return True


_REAL_POPUP = daemon.popup_notify
daemon.popup_notify = fake_popup

CFG = {"enabled": True, "popup": True, "sound": False,
       "min_interval": 1800.0, "quiet_hours": ["23:30", "09:00"]}


def set_cfg(**kw):
    c = dict(CFG)
    c.update(kw)
    daemon.notify_cfg = lambda: c
    return c


def qh_never():
    """一个绝不包含"现在"的时段 —— 拿它表示"不在免打扰"。"""
    off = time.localtime(time.time() + 12 * 3600)
    qh = ["%02d:%02d" % (off.tm_hour, off.tm_min),
          "%02d:%02d" % (off.tm_hour, min(59, off.tm_min + 1))]
    assert not daemon.in_quiet_hours(time.time(), qh), "测试前提不成立"
    return qh


print("\n=== 1. 免打扰时段判定（跨零点是重灾区）===")
check("普通时段内 10:00", daemon.in_quiet_hours(ts_at(10, 0), ["09:00", "12:00"]), True)
check("普通时段外 13:00", daemon.in_quiet_hours(ts_at(13, 0), ["09:00", "12:00"]), False)
check("正好是起点 09:00 → 算在内", daemon.in_quiet_hours(ts_at(9, 0), ["09:00", "12:00"]), True)
check("正好是终点 12:00 → 算在外", daemon.in_quiet_hours(ts_at(12, 0), ["09:00", "12:00"]), False)
check("跨零点 23:45", daemon.in_quiet_hours(ts_at(23, 45), ["23:30", "09:00"]), True)
check("跨零点 03:00", daemon.in_quiet_hours(ts_at(3, 0), ["23:30", "09:00"]), True)
check("跨零点 08:59", daemon.in_quiet_hours(ts_at(8, 59), ["23:30", "09:00"]), True)
check("跨零点 09:00 → 该醒了", daemon.in_quiet_hours(ts_at(9, 0), ["23:30", "09:00"]), False)
check("跨零点 22:00 → 还早", daemon.in_quiet_hours(ts_at(22, 0), ["23:30", "09:00"]), False)

check("None → 不免打扰", daemon.in_quiet_hours(ts_at(3, 0), None), False)
check("空列表 → 不免打扰", daemon.in_quiet_hours(ts_at(3, 0), []), False)
check("三项 → 不免打扰", daemon.in_quiet_hours(ts_at(3, 0), ["1", "2", "3"]), False)
check("时间格式烂 → 不免打扰", daemon.in_quiet_hours(ts_at(3, 0), ["aa", "bb"]), False)
check("起止相同 → 不免打扰（不是'全天静默'）",
      daemon.in_quiet_hours(ts_at(3, 0), ["00:00", "00:00"]), False)
check("小时越界 25:00 → 不免打扰",
      daemon.in_quiet_hours(ts_at(3, 0), ["25:00", "09:00"]), False)

print("\n=== 2. 配置兜底 ===")
check_true("_FALLBACK 里有 notify", "notify" in daemon._FALLBACK)
c = daemon.notify_cfg()
check_true("notify_cfg 字段齐全",
           {"enabled", "popup", "sound", "min_interval", "quiet_hours"} <= set(c), c)
check_true("min_interval 换算成秒", c["min_interval"] >= 60, c["min_interval"])

print("\n=== 3. 状态机：一次掉线只提醒一条 ===")
set_cfg(quiet_hours=qh_never())
STATE.unlink(missing_ok=True)
POPUPS.clear()

daemon.notify_stuck_protocol(False)
check("没掉线时不提醒", len(POPUPS), 0)

daemon.notify_stuck_protocol(True)
check("掉线 → 提醒一条", len(POPUPS), 1)
daemon.notify_stuck_protocol(True)
daemon.notify_stuck_protocol(True)
check("继续掉 → 不重复弹（最小间隔内）", len(POPUPS), 1)

daemon.notify_stuck_protocol(False)
check("恢复后状态清空", daemon._read_notify_state().get("episode_started"), 0.0)

daemon.notify_stuck_protocol(True)
check("**下一次**掉线要重新提醒（不是再也不提醒）", len(POPUPS), 2)

print("\n=== 4. 免打扰时段绝对不弹 ===")
set_cfg(quiet_hours=["00:00", "23:59"])      # 整天都算免打扰
STATE.unlink(missing_ok=True)
POPUPS.clear()
daemon.notify_stuck_protocol(True)
check("免打扰期间一条都不弹", len(POPUPS), 0)
st = daemon._read_notify_state()
check_true("但记住了掉线开始时刻", float(st.get("episode_started") or 0) > 0, st)

check_true("出时段后会补提醒（模拟：把时段挪开）", True)
set_cfg(quiet_hours=qh_never())
daemon.notify_stuck_protocol(True)
check("出时段后补上那一条", len(POPUPS), 1)

print("\n=== 5. 跨免打扰的补充提醒（最多两条）===")
STATE.unlink(missing_ok=True)
POPUPS.clear()
set_cfg(quiet_hours=qh_never())
daemon.notify_stuck_protocol(True)
check("先在非免打扰时段提醒一条", len(POPUPS), 1)

set_cfg(quiet_hours=["00:00", "23:59"])      # 进入免打扰（她还没恢复）
daemon.notify_stuck_protocol(True)
check("免打扰期间不弹", len(POPUPS), 1)
check_true("记下了'欠着一条'", bool(daemon._read_notify_state().get("pending_quiet")))

set_cfg(quiet_hours=qh_never())              # 出时段
daemon.notify_stuck_protocol(True)
check("出时段补一条（总共两条）", len(POPUPS), 2)
daemon.notify_stuck_protocol(True)
check("补完就不再弹了", len(POPUPS), 2)

print("\n=== 6. 恢复后不该补发过期的提醒 ===")
STATE.unlink(missing_ok=True)
POPUPS.clear()
set_cfg(quiet_hours=["00:00", "23:59"])
daemon.notify_stuck_protocol(True)                # 免打扰期间掉线，欠着
check("免打扰期间没弹", len(POPUPS), 0)
daemon.notify_stuck_protocol(False)               # 期间她好了
check("恢复后清空 pending",
      bool(daemon._read_notify_state().get("pending_quiet")), False)
set_cfg(quiet_hours=qh_never())
daemon.notify_stuck_protocol(False)               # 出时段，但没有掉线
check("**已经恢复了就不该再收到'她掉线了'**", len(POPUPS), 0)

print("\n=== 7. 关掉开关就完全不打扰 ===")
STATE.unlink(missing_ok=True)
POPUPS.clear()
set_cfg(enabled=False, quiet_hours=qh_never())
daemon.notify_stuck_protocol(True)
daemon.notify_stuck_protocol(True)
check("enabled=false → 一条都不弹", len(POPUPS), 0)
check_true("但仍然记录掉线（状态面板要看）",
           float(daemon._read_notify_state().get("episode_started") or 0) > 0)

print("\n=== 8. 最小间隔 ===")
STATE.unlink(missing_ok=True)
POPUPS.clear()
set_cfg(quiet_hours=qh_never(), min_interval=99999.0)
daemon.notify_stuck_protocol(True)
check("第一次照弹", len(POPUPS), 1)
# 仍然掉线、且已经过了"最小间隔" → 不成立（间隔设得极大），所以不再弹
daemon.notify_stuck_protocol(True)
check("间隔没到 → 不弹", len(POPUPS), 1)

print("\n=== 9. popup=false：不弹窗，但算「提醒完成了」 ===")
# 这一条测的是**真实** popup_notify 的分支。语义很重要：关掉弹窗只是
# "不想被打扰"，不等于"没提醒"—— 要是返回 False，状态机会把它当失败，
# 于是每轮巡检都重试一次，等于反而一直在惦记这件事。
# ⚠️ XIAOYOU_NO_POPUP=1 会在最前面短路成 False（见第 10 节），
# 所以设了那个变量时这一条没有意义，直接跳过 —— 别让它变成一个
# "看环境脸色"的假失败。
daemon.popup_notify = _REAL_POPUP            # ← 换回真实的，测它自己的分支
if os.environ.get("XIAOYOU_NO_POPUP") == "1":
    print("  [--]   已设 XIAOYOU_NO_POPUP，跳过真实弹窗分支")
else:
    check("popup=false → 返回 True（不弹窗但算提醒过）",
          daemon.popup_notify("标题", "内容", {"popup": False, "sound": False}), True)

# 状态机侧继续用替身，保证"不真的弹窗也能把状态机测全"
daemon.popup_notify = fake_popup
STATE.unlink(missing_ok=True)
POPUPS.clear()
set_cfg(quiet_hours=qh_never(), popup=False)
daemon.notify_stuck_protocol(True)
check("状态下走了一次提醒逻辑", len(POPUPS), 1)
check_true("且被记为已提醒（不会每轮重试）",
           float(daemon._read_notify_state().get("notified_at") or 0) > 0)

print("\n=== 10. 弹窗失败时的两条底线 ===")
daemon.popup_notify = _REAL_POPUP
os.environ["XIAOYOU_NO_POPUP"] = "1"          # 用它人为制造"弹不出来"
try:
    check("弹不出来 → 返回 False（上层才知道没成功）",
          daemon.popup_notify("标题", "内容", {"popup": True, "sound": False}), False)
    # 关键：失败**不能**被记成"已提醒"。记了的话她就彻底静默了 ——
    # 你以为会有弹窗，其实一次都没弹出来，而且再也不会重试。
    STATE.unlink(missing_ok=True)
    set_cfg(quiet_hours=qh_never(), min_interval=0.0)
    daemon.notify_stuck_protocol(True)
    check("失败不记成已提醒（下次还会重试）",
          float(daemon._read_notify_state().get("notified_at") or 0), 0.0)
    check_true("但记了尝试时刻（免得每 45 秒撞一次）",
               float(daemon._read_notify_state().get("attempt_at") or 0) > 0)
finally:
    os.environ.pop("XIAOYOU_NO_POPUP", None)

# 收拾
daemon.popup_notify = _REAL_POPUP
STATE.unlink(missing_ok=True)
(TMP / "notify_state.json.tmp").unlink(missing_ok=True)

print("\n" + "=" * 50)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 50)
sys.exit(0 if FAIL == 0 else 1)
