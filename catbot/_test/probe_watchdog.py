# -*- coding: utf-8 -*-
"""
自愈活体探针 —— 真把看门线程 / 体检线程跑起来，看它们动不动手。

与 test_watchdog.py 的分工：那边验纯函数，这边验"真的跑起来之后的行为"
（会不会漏、会不会重复动手、改动不相关的文件会不会误触发）。

**全程不碰真机器人**：base 目录、配置文件、进程探测全部被替成临时的。
运行:
    python _test/probe_watchdog.py
"""

import json
import logging
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import daemon  # noqa: E402

daemon.log.handlers.clear()
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("    %(levelname)s %(message)s"))
daemon.log.addHandler(_h)
daemon.log.setLevel(logging.INFO)

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def wait_for(fn, timeout=4.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


TMP = Path(tempfile.mkdtemp(prefix="xiaoyou_wd_"))
TMP_SRC = Path(tempfile.mkdtemp(prefix="xiaoyou_wd_src_"))

# ---- 把整个环境搬到临时目录：绝不碰真的 catbot 目录 ----------------------
daemon.BASE_DIR = TMP
daemon.RUN_DIR = TMP / "run"
daemon.RUN_DIR.mkdir(exist_ok=True)
daemon.WATCH_BOT_FILES = ("bot.py", "config.json")
daemon.WATCH_DAEMON_FILES = ()
for _n in ("bot.py", "config.json", "persona.md"):
    (TMP / _n).write_text("# v1\n", encoding="utf-8")

calls = []
_real_ensure_bot = daemon.ensure_bot
daemon.ensure_bot = lambda reason, kind="missing": (
    calls.append((kind, reason)), "已重启")[1]
daemon.proc_create_time = lambda pid: 0.0          # 假装大脑是旧的/不在
daemon.watch_cfg = lambda: {"enabled": True, "interval": 0.15,
                            "settle": 0.3, "cooldown": 0.3}
daemon._stop.clear()

print("\n=== 1. 看门线程：改了该盯的文件 ===")
threading.Thread(target=daemon._watch_worker, daemon=True).start()
time.sleep(0.4)
ck("启动时不该有任何动作（只建立基线）", calls == [], calls)

(TMP / "bot.py").write_text("# v2\n", encoding="utf-8")
ck("改动被认出来了", wait_for(lambda: len(calls) >= 1, 3.0), calls)
ck("动作是 watch / 原因写到文件名",
   calls and calls[0][0] == "watch" and "bot.py" in calls[0][1], calls)
n1 = len(calls)
time.sleep(1.0)
ck("同一次改动只重启一次（不会反复触发）", len(calls) == n1, calls)

print("\n=== 2. 看门线程：不该理的文件 ===")
(TMP / "persona.md").write_text("# v2 —— 改完即生效，不该重启\n", encoding="utf-8")
time.sleep(1.2)
ck("persona.md 改了 → 不重启（它是热重载的）", len(calls) == n1, calls)

print("\n=== 3. 看门线程：一口气改好几个（编辑器存盘常见） ===")
(TMP / "config.json").write_text('{"a": 2}\n', encoding="utf-8")
(TMP / "bot.py").write_text("# v3\n", encoding="utf-8")
ck("会动手", wait_for(lambda: len(calls) > n1, 3.0), calls)
time.sleep(1.0)
ck("两个文件合起来只触发一次", len(calls) == n1 + 1, calls)
n2 = len(calls)

print("\n=== 4. 看门线程：大脑比文件新就跳过 ===")
# 模拟"设置页刚替你重启过"：进程启动时刻比文件改动更晚
daemon.proc_create_time = lambda pid: time.time() + 5
(TMP / "config.json").write_text('{"a": 3}\n', encoding="utf-8")
time.sleep(1.5)
ck("已经生效的改动不再多重启一次", len(calls) == n2, calls)
daemon.proc_create_time = lambda pid: 0.0

print("\n=== 5. 看门线程：连续改，靠冷却拉开 ===")
(TMP / "config.json").write_text('{"a": 4}\n', encoding="utf-8")
ck("第 1 次", wait_for(lambda: len(calls) > n2, 3.0), calls)
(TMP / "config.json").write_text('{"a": 5}\n', encoding="utf-8")
ck("第 2 次（冷却已过）", wait_for(lambda: len(calls) > n2 + 1, 3.0), calls)

daemon._stop.set()
time.sleep(0.5)

print("\n=== 6. 体检线程：活着但不干活 ===")
calls.clear()
daemon._policy = None
daemon.bot_alive = lambda: True
daemon.read_pid = lambda name: 4242
_now = time.time()
# 心跳 500 秒没更新 = 整个进程冻住了
daemon.read_heartbeat = lambda: {"pid": 4242, "boot": _now - 5000,
                                 "ts": _now - 500, "busy": 0.0,
                                 "pending": 0, "ws": True, "ws_at": _now - 5000}
daemon.health_cfg = lambda: {"enabled": True, "interval": 0.3, "hb_stale": 90.0,
                             "busy_max": 600.0, "ws_down": 300.0,
                             "grace": 120.0, "max_per_hour": 6}
daemon._stop.clear()
threading.Thread(target=daemon._health_worker, daemon=True).start()
time.sleep(0.4)                      # 只够跑完第 1 次体检
ck("一次不健康不急着动手（要连续两次）", calls == [], calls)
ck("第二次判到 → 动手", wait_for(lambda: calls, 3.0), calls)
ck("动作是 health", calls and calls[0][0] == "health", calls)
ck("原因写清楚了", calls and "没更新" in calls[0][1], calls)

print("\n=== 7. 体检线程：健康时不许乱动 ===")
calls.clear()
daemon.read_heartbeat = lambda: {"pid": 4242, "boot": _now - 5000,
                                 "ts": time.time(), "busy": 0.5,
                                 "pending": 0, "ws": True, "ws_at": _now - 5000}
time.sleep(1.2)
ck("一切正常 → 一次都不动手", calls == [], calls)

print("\n=== 8. 体检线程：它压根不在时不管（那是主巡检的活） ===")
calls.clear()
daemon.bot_alive = lambda: False
time.sleep(1.0)
ck("进程不在 → 体检线程不插手", calls == [], calls)
daemon._stop.set()
time.sleep(0.4)

print("\n=== 9. ensure_bot：同一把锁，防两个大脑 ===")
daemon.ensure_bot = _real_ensure_bot     # 前面几节用的是替身，这里换回真的
daemon._policy = None
daemon._stop.set()          # 确保前面两个线程都退干净
time.sleep(0.3)
order = []
active = 0
lock_held = []


def fake_start_bot():
    global active
    lock_held.append(daemon.BOT_LOCK.locked())
    active += 1
    order.append(active)
    time.sleep(0.35)
    active -= 1
    return True


daemon.start_bot = fake_start_bot
daemon.pid_alive = lambda pid: False
daemon.read_pid = lambda name: 0
daemon.bot_alive = lambda: False
ths = [threading.Thread(target=daemon.ensure_bot, args=("并发测试", "missing"))
       for _ in range(4)]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=5)
ck("start_bot 一定是拿着锁调的", lock_held and all(lock_held), lock_held)
ck("并发时最多只有一个在启动", max(order) == 1 if order else False, order)

print("\n=== 10. 崩溃循环保护（真跑一遍） ===")
daemon._policy = None
daemon.start_bot = lambda: True
daemon.pid_alive = lambda pid: False
daemon.read_pid = lambda name: 0
daemon.bot_alive = lambda: False
daemon._crash_tail = lambda n=6: "(假装大脑崩了)"
res = []
for i in range(6):
    res.append(daemon.ensure_bot("第 %d 轮" % (i + 1), "missing"))
    time.sleep(0.05)
ck("前几次会拉起", res[0] == "已重启" and res[1] == "已重启", res)
ck("连续起来就死 → 第 4 次起停手",
   res[2] == "已重启" and res[3:] == ["", "", ""], res)
ck("停手后确实不再启动", daemon.policy().blocked_until > time.time())

shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(TMP_SRC, ignore_errors=True)

print("\n%s" % ("=" * 50))
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 50)
sys.exit(1 if FAIL else 0)
