# -*- coding: utf-8 -*-
"""
小柚 · 常驻守护进程（无窗口）
==============================

开机后无人值守地维持整条链路：

    [本地模式] Ollama(11434) → NapCat 协议层 (3000/3001) → 猫娘大脑 (bot.py)
    [云端模式]                NapCat 协议层 (3000/3001) → 猫娘大脑 (bot.py)

推理后端走本地还是云端由 config.json 的 `llm.provider` 决定，守护进程每轮
巡检都会重读它：写 `ollama` 就盯 11434，写 `openai` 就把 Ollama 当无关组件
跳过。**切到云端后不需要改这个文件，也不需要重启守护进程。**

**采用 NapCat 独立模式**，这是关键选择：不需要注入 QQ 客户端、不需要 QQ
正在运行，因此
  - 不会打扰你正常使用 QQ
  - 不会因为 QQ 自动升级而失效
  - 不需要杀掉电脑上的 QQ
  - 冷启动只要 5 秒（注入模式要 30 秒以上）

本进程自身无窗口，所以"关掉某个窗口机器人就下线"这件事不会再发生。

除了"掉了就补"，它还管两件以前要人手动做的事（都能用 config.json 关掉）：

  自愈一「改完自动重启」 bot.py / tools.py / voice.py / corpus.py / config.json 一变，
                        自动重启大脑让改动生效 —— 不用再点「保存并重启」。
                        daemon.py 自己变了则换成新的守护进程。
  自愈二「卡死自动重启」 按大脑写的心跳判断它是不是**活着但不干活**
                        （进程在、线程池却卡死，表现是收得到消息永远不回话），
                        判到就自动重启，并带崩溃循环保护与每小时次数上限。

配套 service_ctl.py 提供 start / stop / status / install / uninstall。
"""

from __future__ import annotations

import ast
import ctypes
from ctypes import wintypes
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
RUN_DIR = BASE_DIR / "run"
LOG_DIR = BASE_DIR / "logs"
NAP_DIR = BASE_DIR / "napcat"          # NapCat 主体：node.exe / index.js / wrapper.node
NAP_CONF_DIR = NAP_DIR / "napcat"      # 配置与缓存

RUN_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

NAPCAT_NODE = NAP_DIR / "node.exe"
NAPCAT_ENTRY = "index.js"
ACCOUNT_FILE = BASE_DIR / "napcat_account.txt"

# Ollama：本地推理后端。它挂了会出现"能收消息但永远不回"，
# 而守护进程以前只盯协议层和大脑，完全不管它 —— 那一环缺失就是卡死的原因。
#
# 但要注意：大脑不一定跑在本地。config.json 里 llm.provider 改成 openai 之后
# 推理在云端，Ollama 就成了无关组件 —— 再去拉它既白白多一个进程，又会让
# 状态面板谎报"推理后端已就绪"。所以下面的判断都先问 llm_provider()。
OLLAMA_PORT = 11434
CONFIG_FILE = BASE_DIR / "config.json"


def llm_provider() -> str:
    """大脑的推理后端是本地 Ollama，还是云端 API。"""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return str((cfg.get("llm") or {}).get("provider", "ollama")).strip().lower()
    except Exception:
        return "ollama"      # 读不到就按老行为（本地）处理，保持兼容


def backend_is_local() -> bool:
    """True = 需要守护进程盯着 11434；False = 云端，Ollama 与本次运行无关。

    每次调用都重读 config.json：改完配置不用重启守护进程就能生效，
    而读一个小 JSON 的代价可以忽略（最长 45 秒才读一次）。
    """
    return llm_provider() == "ollama"


def find_ollama() -> Path | None:
    """定位 ollama.exe：优先 PATH，其次两个常见安装目录。"""
    hit = shutil.which("ollama")
    if hit:
        return Path(hit)
    for cand in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(r"C:\Program Files\Ollama\ollama.exe"),
    ):
        if cand.exists():
            return cand
    return None

# bot.py 的 requests / websocket-client 装在 catbot 这个 venv 里，必须用它
VENV_PYTHONW = Path(r"C:\Users\26415\.workbuddy\binaries\python\envs\catbot\Scripts\pythonw.exe")

HEARTBEAT = RUN_DIR / "daemon.heartbeat"
POLL_SECONDS = 45          # 稳态巡检间隔：链路稳了就不用频繁打扰
POLL_FAST = 10             # 未就绪时的间隔：让它尽快把整条链补齐

# 协议层「进程活着、端口却一直不开」的持续时长。
# 等扫码登录 / 被风控拒绝时就是这个状态。守护进程这时只能干等
# （重启只会让二维码作废），但必须【说出来】—— 否则日志一片安静，
# 用户看到的现象是「机器人没反应」却毫无线索。
STUCK_WARN_AFTER = 120     # 卡住超过 2 分钟才开口，避免冷启动时误报
STUCK_LOG_EVERY = 300      # 之后每 5 分钟提醒一次
_stuck_since: float | None = None
_stuck_last_log = 0.0

# ---- 子进程标志：这是"桌面不出现任何窗口"的关键 ------------------------
#
# 本进程由 pythonw.exe 启动，自己没有控制台。此时若不带任何标志去启动
# node.exe 这类「控制台程序」，Windows 会**为子进程新建一个控制台窗口**；
# 而子进程的输出已经被重定向到日志文件，于是那个窗口全黑、什么都不显示。
#
# 后果链：用户关掉黑窗口 → node 收到关闭事件被杀 → NapCat 掉线 →
#         守护进程下一轮（30 秒）又把它拉起来 → 黑窗口重新出现。
#
# CREATE_NO_WINDOW     给子进程一个隐藏控制台，桌面上看不见
# CREATE_NEW_PROCESS_GROUP  子进程自成一个进程组，不接收父进程的 Ctrl 信号
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
CHILD_FLAGS = NO_WINDOW | CREATE_NEW_GROUP
# 守护进程自己脱离控制台（供 service_ctl.py 使用）
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

# --------------------------------------------------------------------------
# 日志（pythonw 下没有 stdout，只能写文件）
# --------------------------------------------------------------------------

log = logging.getLogger("daemon")
log.setLevel(logging.INFO)

# 和 bot.py 同一条规矩：跑测试时别往生产日志里写。
# 测试会故意让组件"看起来崩掉"来验证自愈逻辑，那些记录写进 daemon.log
# 会让人以为守护进程真的反复重启过。判据同样是 sys.argv[0] 是否在 _test/ 下。
_NO_FILE_LOG = ("_test" in Path(sys.argv[0]).parts
                or os.environ.get("XIAOYOU_NO_FILE_LOG") == "1")

if not _NO_FILE_LOG:
    _handler = RotatingFileHandler(LOG_DIR / "daemon.log", maxBytes=1_000_000,
                                   backupCount=3, encoding="utf-8")
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_handler)


def pythonw() -> Path:
    """优先 catbot venv 的解释器，否则退回当前解释器旁边的 pythonw。"""
    if VENV_PYTHONW.exists():
        return VENV_PYTHONW
    cand = Path(sys.executable).with_name("pythonw.exe")
    return cand if cand.exists() else Path(sys.executable)


# --------------------------------------------------------------------------
# 自愈：改完自动重启 + 卡死自动重启
# --------------------------------------------------------------------------
#
# 守护进程本来就保证"进程掉了就补"，但有两种情况它一直管不到：
#
#   ① 进程活得好好的，可线程池里两条任务都卡住了 —— 她收得到消息、
#      永远不回话。这时 pid 查起来一切正常，只能靠心跳里的忙闲数字。
#   ② 你改完代码 / 配置，改动躺在硬盘上，得等重启才生效。
#
# 两条路都汇到 ensure_bot()，共用一把锁 —— 否则主巡检和自愈线程会同时
# 看到"它死了"、同时拉起两个大脑，两个进程抢同一个 QQ 号。

HB_FILE = RUN_DIR / "bot.heartbeat"
BOT_CONSOLE = LOG_DIR / "bot_console.log"
BOT_LOCK = threading.Lock()
BOT_RESTART_GAP = 2.5        # 杀掉旧进程后等它松开 WS 与文件句柄，再起新的
HEALTH_FAILS_TO_ACT = 2      # 连续两次判到不健康才动手，躲开偶发的读盘卡顿
# 「进程创建时间」与「pid 文件写入时间」允许差多少秒。
# 双侧容差：venv 的 pythonw.exe 是启动器，真正跑 bot.py 的是它拉起的子进程，
# 子进程写 pid 文件会比它自己被创建晚几秒（要 import 一整套依赖）。
# 见 bot_alive() 的注释。
PID_STAMP_TOLERANCE = 300.0

# 改了这些就得让大脑换一份代码（persona.md / memes.md 不在其中：
# 它们存盘即生效，本来就按 mtime 热重载，重启纯属白折腾）
WATCH_BOT_FILES = ("bot.py", "tools.py", "voice.py", "corpus.py", "config.json")
# 这个改了要换的是守护进程自己，不是大脑
WATCH_DAEMON_FILES = ("daemon.py",)

# 内置兜底默认值。正常情况下从 bot.py 的 DEFAULT_CONFIG 里抽（单一来源），
# 抽不到才用它 —— 避免自愈功能因为一个正则失配就整个失效。
_FALLBACK = {
    "watch": {"enabled": True, "interval_sec": 2, "settle_sec": 3, "cooldown_sec": 20},
    "health": {"enabled": True, "interval_sec": 20, "hb_stale_sec": 90,
               "busy_sec": 600, "ws_down_sec": 300, "grace_sec": 120,
               "max_per_hour": 6},
    "notify": {"enabled": True, "popup": True, "sound": True,
               "min_interval_min": 30, "quiet_hours": ["23:30", "09:00"]},
}

_BOT_DEFAULTS: dict | None = None
_stop = threading.Event()
_handed_over = False         # 是否把守护进程的班交给了新实例
_policy = None


def _defaults_from_bot() -> dict:
    """从 bot.py 里抽 DEFAULT_CONFIG 当默认值。

    用 ast 而不是 import bot —— 那会把 requests / websocket / voice 一整条
    依赖链拖进来，守护进程就非得跑在装着那些东西的解释器下不可。跟设置页
    是同一套理由。
    """
    try:
        src = (BASE_DIR / "bot.py").read_text(encoding="utf-8")
        m = re.search(r"^DEFAULT_CONFIG\s*=\s*(\{.*?^\})\s*$", src, re.S | re.M)
        if m:
            data = ast.literal_eval(m.group(1))
            if isinstance(data, dict) and data:
                return data
    except Exception:
        log.warning("从 bot.py 抽默认值失败，自愈配置改用内置兜底", exc_info=True)
    return _FALLBACK


def bot_defaults() -> dict:
    """抽一次就缓存 —— 每 2 秒重读一个文件没必要。"""
    global _BOT_DEFAULTS
    if _BOT_DEFAULTS is None:
        _BOT_DEFAULTS = _defaults_from_bot()
    return _BOT_DEFAULTS


def _num(v, default: float) -> float:
    """配置值软着陆：'abc' / None / NaN / inf 一律退回默认值。

    这里绝不能抛异常 —— 抛出去会让自愈线程安安静静地死掉，而"安安静静地
    死掉"正是这个功能要消灭的东西。
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n != n or n in (float("inf"), float("-inf")):
        return default
    return n


def _section(name: str) -> dict:
    """「bot.py 里的默认值」叠上「config.json 里的实际值」。"""
    base = dict((bot_defaults().get(name) or _FALLBACK.get(name) or {}))
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        got = cfg.get(name)
        if isinstance(got, dict):
            base.update(got)
    except Exception:
        pass
    return base


def watch_cfg() -> dict:
    s = _section("watch")
    return {
        "enabled": bool(s.get("enabled", True)),
        "interval": max(1.0, _num(s.get("interval_sec"), 2)),
        "settle": max(0.0, _num(s.get("settle_sec"), 3)),
        "cooldown": max(0.0, _num(s.get("cooldown_sec"), 20)),
    }


def health_cfg() -> dict:
    s = _section("health")
    return {
        "enabled": bool(s.get("enabled", True)),
        "interval": max(5.0, _num(s.get("interval_sec"), 20)),
        "hb_stale": max(20.0, _num(s.get("hb_stale_sec"), 90)),
        "busy_max": max(30.0, _num(s.get("busy_sec"), 600)),
        "ws_down": max(30.0, _num(s.get("ws_down_sec"), 300)),
        "grace": max(0.0, _num(s.get("grace_sec"), 120)),
        "max_per_hour": max(1, int(_num(s.get("max_per_hour"), 6))),
    }


# --------------------------------------------------------------------------
# 掉线提醒
# --------------------------------------------------------------------------
#
# 被腾讯踢下线之后，**必须有人扫一次码**（或完成一次短信验证）才能恢复；
# 在那之前她一句话都说不了。2026-09-26 实测：07:15 被踢、11:39 才有人扫码 ——
# 她哑了 4 小时 24 分，而**没有任何途径让主人知道**（守护进程每 5 分钟往
# daemon.log 写一条警告，可那是给翻日志的人看的）。
#
# 社区那类"保活脚本"解决不了这件事：它们只会重启，而重启会作废验证会话
# （见 inspect_handler 里"卡在登录时不重启"那段注释），于是既救不活也报不出信。
# 所以这里补的是**可见性**，不是自动化。
#
# 两条硬约束，缺一条就从"提醒"变成"骚扰"：
#   ① **免打扰时段绝对不弹** —— 而被踢最频繁的恰恰是半夜（00:40、07:15 都撞上），
#      半夜把人吵醒比掉线本身更糟。改成"时段结束后再补一条"。
#   ② **一次掉线最多提醒两条**（首次 + 跨过免打扰后的补充），外加最小间隔兜底。
NOTIFY_STATE = RUN_DIR / "notify_state.json"

_NOTIFY_TITLE = "小柚掉线了 —— 需要你扫一次码"
_NOTIFY_BODY = (
    "她被腾讯踢下线，现在卡在 QQ 登录，不会回任何消息。\n"
    "\n"
    "双击「扫码登录.bat」扫一下就能恢复。\n"
    "\n"
    "想知道具体原因：跑「查看状态.bat」，看「协议层」那一行。\n"
    "（这个提醒每个免打扰时段最多一条，不会一直弹。）"
)


def notify_cfg() -> dict:
    s = _section("notify")
    return {
        "enabled": bool(s.get("enabled", True)),
        "popup": bool(s.get("popup", True)),
        "sound": bool(s.get("sound", True)),
        "min_interval": max(0.0, _num(s.get("min_interval_min"), 30)) * 60.0,
        "quiet_hours": s.get("quiet_hours") or ["23:30", "09:00"],
    }


def _hhmm_min(v) -> int | None:
    """把 "23:30" 变成 1410；格式不对返回 None。"""
    try:
        h, m = str(v).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        pass
    return None


def in_quiet_hours(ts: float, qh) -> bool:
    """`ts` 这个时刻在不在免打扰时段里。qh=["23:30","09:00"] = 这段时间闭嘴。

    纯函数，单独拆出来是为了**能测** —— 跨零点是这里最容易写错的地方，
    而写错的表现是"半夜弹窗"或"永远不提醒"，两种都得靠肉眼才发现。

    注：bot.py 里有同名的 `_quiet_now`，但那边**故意不共用** ——
    守护进程是用 ast 读 bot.py 的默认值（`_defaults_from_bot`），从不 import 它
    （import 会把 requests/websocket 那一串拖进来）。二十行的判定，
    不值得为它建一条模块依赖。
    """
    if not isinstance(qh, (list, tuple)) or len(qh) != 2:
        return False
    a, b = _hhmm_min(qh[0]), _hhmm_min(qh[1])
    if a is None or b is None or a == b:
        return False
    t = time.localtime(ts)
    cur = t.tm_hour * 60 + t.tm_min
    if a < b:
        return a <= cur < b
    return cur >= a or cur < b          # 跨零点，比如 23:30~09:00


def _read_notify_state() -> dict:
    try:
        d = json.loads(NOTIFY_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}                       # 不在 / 坏了 → 当全新一段，不报错


def _write_notify_state(st: dict) -> None:
    try:
        RUN_DIR.mkdir(exist_ok=True)
        tmp = NOTIFY_STATE.with_name(NOTIFY_STATE.name + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, NOTIFY_STATE)
    except Exception:
        log.exception("掉线提醒状态写入失败")


def popup_notify(title: str, text: str, cfg: dict) -> bool:
    """弹一个 Windows 消息框提醒主人。成功返回 True。

    用 ctypes 直接调 user32 是刻意的：守护进程跑在 pythonw 下（没有控制台），
    而 catbot 那个 venv **不带 tkinter**（见 MEMORY.md 的组件说明），
    所以没有任何现成 UI 可用 —— 但"提醒"这件事不该依赖外部工具。

    ⚠️ 必须丢到**独立线程**里弹：MessageBoxW 会阻塞到用户点掉为止，
    在主巡检线程里直接调会让整个守护进程停摆 —— 那比掉线本身更糟。
    """
    if os.environ.get("XIAOYOU_NO_POPUP") == "1":
        log.info("（已禁止弹窗）本该提醒主人：%s", title)
        return False
    if not cfg.get("popup", True):
        # 关掉弹窗 = 只想在日志/面板里留痕，不想被打扰 —— 那也是"提醒成功了"
        log.warning("掉线提醒（popup=false，未弹窗）：%s", title)
        return True
    if cfg.get("sound"):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass                        # 没声音也要把窗弹出来
    try:
        # 先取符号：拿不到就没法弹，这时要老实返回 False，
        # 让上层别把这次记成"提醒过了"（那样就彻底静默了）。
        mb = ctypes.windll.user32.MessageBoxW
    except Exception:
        log.exception("拿不到 MessageBoxW，弹窗提醒不可用")
        return False

    MB_OK, MB_ICONWARNING, MB_SETFOREGROUND = 0x0, 0x30, 0x10000
    flags = MB_OK | MB_ICONWARNING | MB_SETFOREGROUND

    def _show() -> None:
        """在独立线程里弹。返回值只有 0 才说明窗压根没建出来。"""
        try:
            rc = mb(None, text, title, flags)
            if not rc:
                log.warning("弹窗返回 0 —— 窗可能没真正显示出来")
        except Exception:
            log.exception("弹窗线程里出错（不影响守护进程）")

    threading.Thread(target=_show, daemon=True).start()
    return True


def notify_stuck_protocol(stuck: bool) -> None:
    """协议层是不是卡在登录 —— 是就提醒主人一次。

    `stuck=True` =「进程在、3000 端口不开」，典型就是卡在 QQ 登录等人工操作。
    它自己好了就把整段状态清空，**免得把一条已经过期的提醒补发给主人**
    （那才是最烦的：她都恢复了，你还被告知"她掉线了"）。
    """
    st = _read_notify_state()
    # (掉线开始, 提醒成功时刻, 上次尝试时刻, 免打扰期间欠着一条)
    old = (float(st.get("episode_started") or 0),
           float(st.get("notified_at") or 0),
           float(st.get("attempt_at") or 0),
           bool(st.get("pending_quiet")))

    if not stuck:
        new = (0.0, 0.0, 0.0, False)
        if new != old:
            log.info("协议层已恢复，掉线提醒状态清空")
    else:
        cfg = notify_cfg()
        now = time.time()
        started, notified, attempt, pending = old
        if not started:
            started = now
            log.warning("协议层卡在登录 —— 已开始计时，需要人工扫码；"
                        "免打扰时段会在时段结束后提醒")
        if cfg["enabled"]:
            if in_quiet_hours(now, cfg["quiet_hours"]):
                # 免打扰时段不弹。但要是之前已经提醒过（事情还没解决），
                # 记一笔"欠着"，等出了时段补一条 —— 否则主人一觉醒来
                # 只知道"她怎么没说话"，不知道"她掉了一整晚"。
                if notified and not pending:
                    pending = True
            elif pending:
                # 免打扰期间欠下的那条：是**刻意推迟**的一次性提醒，
                # 不再受最小间隔约束 —— 那个间隔是防"重复轰炸"的，不是防"补发"的。
                if popup_notify(_NOTIFY_TITLE, _NOTIFY_BODY, cfg):
                    notified, pending = now, False
                attempt = now
            elif not notified and now - attempt >= cfg["min_interval"]:
                # 弹失败也要记 attempt：否则每轮巡检（45 秒）都会再撞一次。
                if popup_notify(_NOTIFY_TITLE, _NOTIFY_BODY, cfg):
                    notified = now
                attempt = now
        new = (started, notified, attempt, pending)

    if new != old:
        _write_notify_state({"episode_started": new[0], "notified_at": new[1],
                             "attempt_at": new[2], "pending_quiet": new[3]})


def read_heartbeat() -> dict | None:
    try:
        data = json.loads(HB_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_heartbeat() -> None:
    """删掉那份心跳。

    杀掉旧大脑之后**必须**做这一步：旧心跳留在磁盘上，健康探针读到它会以为
    "pid 对不上 → 是旧进程"，于是拿刚拉起的新生儿顶罪。删掉之后，探针看到的是
    "还没有心跳" —— 配合新生豁免，新大脑才有机会写出自己的第一份心跳。
    """
    try:
        HB_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        log.warning("清理旧心跳失败 —— 健康探针可能把上一个进程的 pid 误判一次")


def pid_age(pid: int, now: float | None = None) -> float | None:
    """这个进程已经活了多久（秒）。

    查不出来（进程不在、权限不够）就返回 None = "不知道"，调用方按老规矩判 ——
    绝不用 run/bot.pid 的 mtime 兜底：那是文件时间，跟进程活得久不久没关系，
    拿它当"新生豁免"的依据会让一个僵死的老进程被一直豁免下去。
    """
    if not pid:
        return None
    created = proc_create_time(pid)
    if not created:
        return None
    return max(0.0, (time.time() if now is None else now) - created)


def _stray_brain_pid(keep_pid: int) -> int:
    """心跳文件里记着的那个 pid —— 若它还活着、且确实像是"上一任大脑"，返回它。

    只认「有据可查」的 pid（心跳自己写下来的那个），绝不按进程名扫一片 ——
    那会误伤用户机器上别的 Python 脚本。

    两道证明材料，缺一不可：
      1. 它比当前这个大脑**老**（是它前面那一任）；
      2. 它在心跳写下那一刻就已经存在（`created <= ts`）—— 否则说明这个 pid
         是被系统回收给了别人，跟我们的心跳没关系。
    """
    hb = read_heartbeat() or {}
    pid = int(_num(hb.get("pid"), 0))
    if not pid or pid == keep_pid or pid == os.getpid() or pid == read_pid("daemon"):
        return 0
    if not pid_alive(pid):
        return 0

    created = proc_create_time(pid)
    ts = _num(hb.get("ts"), 0.0)
    if created and ts and created > ts + 2:
        return 0
    cur = proc_create_time(keep_pid) if keep_pid else 0.0
    if cur and created and created >= cur:
        return 0
    return pid


def health_verdict(hb, *, expect_pid: int, now: float, cfg: dict,
                   alive_for: float | None = None,
                   stale_pid_alive: bool = False) -> tuple[bool, str]:
    """纯函数：拿一份心跳内容判断大脑健不健康 → (是否正常, 原因)。

    刻意不碰文件、不碰进程：测试可以直接喂各种心跳进来，不用真起一个机器人。

    alive_for：当前这个大脑进程已经活了多久（秒），由 `pid_age()` 算好喂进来。
               None = 不知道，按老规矩判（不豁免）。

    stale_pid_alive：心跳里那个 pid 现在还活着吗？由调用方探测。
               心跳的 pid 是个**还活着的"前任"**时，真正的病灶是那个前任，
               **不是**当前进程 —— 必须让调用方分得清，否则它会把刚拉起的新任
               当替罪羊一轮一轮杀下去（2026-09-24 晚的死循环正是如此）。
    """
    # ① 新生豁免 —— 必须排在最前面。
    #
    # 一个刚被拉起的大脑要 import 一整条链表、连 NapCat、加载语音模型，几十秒
    # 才写出第一份心跳很正常。而此刻磁盘上躺着的是上一个进程留下的旧心跳
    # （或者刚被我们清掉、干脆没有）。如果这就判死，新大脑会在写出第一份心跳
    # 之前被杀掉 —— 杀了拉、拉了杀，40 秒一轮，永远起不来。
    if alive_for is not None and 0 <= alive_for < cfg["grace"]:
        return True, ""

    if not isinstance(hb, dict):
        return False, "没有心跳文件（大脑没在写）"

    hb_pid = int(_num(hb.get("pid"), 0))
    if expect_pid and hb_pid and hb_pid != expect_pid:
        if stale_pid_alive:
            return False, ("心跳是另一个还活着的大脑（pid %d）写的，当前是 pid %d"
                           " —— 上一任没死干净" % (hb_pid, expect_pid))
        return False, "心跳是旧进程（pid %d）留下的，当前是 pid %d" % (hb_pid, expect_pid)

    boot = _num(hb.get("boot"), 0.0)
    if boot and (now - boot) < cfg["grace"]:
        return True, ""               # 刚启动的，还没到审判它的时候

    ts = _num(hb.get("ts"), 0.0)
    if not ts:
        return False, "心跳里没有时间戳"
    age = now - ts
    if age > cfg["hb_stale"]:
        return False, "心跳已经 %.0f 秒没更新（超过 %.0f 秒就算僵死）" % (age, cfg["hb_stale"])

    busy = _num(hb.get("busy"), 0.0)
    if busy > cfg["busy_max"]:
        return False, "有一条消息已经处理了 %.1f 分钟还没完" % (busy / 60.0)

    if not hb.get("ws"):
        ref = max(boot, _num(hb.get("ws_at"), boot))
        down = now - ref
        if down > cfg["ws_down"]:
            return False, "和协议层的连接断了 %.0f 秒还没连上" % down
    return True, ""


def watch_files() -> dict:
    """要盯的文件 → mtime。文件不在就跳过（比如被删了）。"""
    out = {}
    for name in WATCH_BOT_FILES + WATCH_DAEMON_FILES:
        try:
            out[name] = (BASE_DIR / name).stat().st_mtime
        except OSError:
            pass
    return out


def changed_files(prev: dict, cur: dict) -> list[str]:
    """纯函数：比两次快照，列出新增 / 改动 / 消失的文件名。"""
    names = set(prev) | set(cur)
    return sorted(n for n in names if prev.get(n) != cur.get(n))


class RestartPolicy:
    """决定「还要不要再自动重启一次」。

    两条自我约束，都是为了让自愈别变成新的故障源：

      1. 崩溃循环保护 —— 刚起来就死，连着 CRASH_LIMIT 次就停手歇一会儿。
         没有它，一个写错了的 bot.py 会让守护进程每 10 秒重启一次，日志刷爆、
         CPU 白烧，而用户完全看不出哪儿不对。
      2. 频次上限 —— 一小时内最多自动重启 max_per_hour 次。防的是"重启也治
         不好"的故障（比如 Key 填错、连接一直被拒）被无限重启下去。

    纯逻辑、不碰文件，测试可以直接快进时间喂它。
    """

    CRASH_WINDOW = 60.0     # "起来就死"的判定窗口
    CRASH_LIMIT = 3         # 连续几次就停手
    BLOCK_SEC = 300.0       # 停手多久

    def __init__(self, max_per_hour: int = 6):
        self.max_per_hour = max(1, int(max_per_hour))
        self._last_start = 0.0
        self._quick_crashes = 0
        self._health_restarts = deque()
        self.blocked_until = 0.0
        self.warned = False

    def note_start(self, now: float) -> None:
        self._last_start = now

    def note_alive(self, now: float) -> None:
        """它活着、而且活得比判定窗口久 → 上次启动不算"起来就死"。"""
        if self._last_start and (now - self._last_start) >= self.CRASH_WINDOW:
            self._quick_crashes = 0
            self.blocked_until = 0.0

    def note_dead(self, now: float) -> bool:
        """发现它已经不在、且启动没多久 → 记一次崩溃。返回是否刚触发停手。"""
        if self._last_start and (now - self._last_start) < self.CRASH_WINDOW:
            self._quick_crashes += 1
            if self._quick_crashes >= self.CRASH_LIMIT:
                self._quick_crashes = 0
                self.blocked_until = now + self.BLOCK_SEC
                return True
        return False

    def allow(self, now: float, kind: str) -> tuple[bool, str]:
        if now < self.blocked_until:
            return False, "连续崩溃，暂停自动重启（还要 %.0f 秒）" % (self.blocked_until - now)
        if kind == "health":
            while self._health_restarts and now - self._health_restarts[0] > 3600:
                self._health_restarts.popleft()
            if len(self._health_restarts) >= self.max_per_hour:
                return False, "一小时内已经自动重启 %d 次，先停手" % len(self._health_restarts)
        return True, ""

    def note_restart(self, now: float, kind: str) -> None:
        if kind == "health":
            self._health_restarts.append(now)


def policy() -> RestartPolicy:
    global _policy
    if _policy is None:
        _policy = RestartPolicy()
    return _policy


def clear_pid(name: str) -> None:
    try:
        (RUN_DIR / f"{name}.pid").unlink(missing_ok=True)
    except Exception:
        pass


def _crash_tail(n: int = 6) -> str:
    """把大脑控制台日志的最后几行捞出来 —— 崩溃原因一般就写在那儿。

    这些东西以前只落在一个没人会主动打开的文件里，守护进程重启完一声不吭，
    用户感受是"它莫名其妙又重启了"。顺手贴进 daemon.log 才算交代清楚。
    """
    try:
        with open(BOT_CONSOLE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            raw = f.read()
    except OSError:
        return ""

    # **逐行**解码，先 UTF-8 后 GBK。
    # 这个文件是追加写的，历史内容可能是 GBK（旧版没设 PYTHONIOENCODING 时的
    # 遗留），新内容才是 UTF-8。整段套一个编码去解会把最后几行糊成一片问号，
    # 而崩溃原因恰好就在最后几行 —— 那就等于没写。
    lines = []
    for chunk in raw.splitlines():
        for enc in ("utf-8", "gbk"):
            try:
                lines.append(chunk.decode(enc).strip())
                break
            except UnicodeDecodeError:
                continue
        else:
            # 从字节偏移处切进来可能截断了半个字，兜底替换掉就行
            lines.append(chunk.decode("utf-8", "replace").strip())
    return " / ".join(x for x in lines[-n:] if x)


# --------------------------------------------------------------------------
# 探测工具
# --------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259                      # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def read_pid(name: str) -> int:
    try:
        return int((RUN_DIR / f"{name}.pid").read_text(encoding="ascii").strip())
    except Exception:
        return 0


def write_pid(name: str, pid: int) -> None:
    try:
        (RUN_DIR / f"{name}.pid").write_text(str(pid), encoding="ascii")
    except Exception:
        log.warning("写 pid 文件失败: %s", name)


# 协议层判活 —— **单一真源在 service_ctl.py**（`napcat_alive()` 等）。
# 这里只做薄委托，绝不另写一份：这个项目已经栽过太多次「同一条判断写两处、
# 改了一处忘了另一处」的跟头（见 SKILL.md 铁律 7）。
def _sc():
    import service_ctl
    return service_ctl


def napcat_alive() -> bool:
    """协议层进程还活着吗（认全部已知 pid，不是只认 pid 文件那一个）。"""
    try:
        return _sc().napcat_alive()
    except Exception:
        log.exception("协议层判活失败，退回只认 pid 文件")
        return pid_alive(read_pid("napcat"))


def remember_napcat_pid(pid: int) -> None:
    """把新拉起的 NapCat pid 记进清单（不覆盖已有，见 service_ctl 里的说明）。"""
    try:
        _sc().remember_napcat_pid(pid)
    except Exception:
        log.exception("记录 NapCat pid 失败")
        write_pid("napcat", pid)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def read_account() -> str:
    try:
        return ACCOUNT_FILE.read_text(encoding="ascii", errors="ignore").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# BoringSSL 依赖（独立模式必需，从 QQ 安装目录取现成的）
# --------------------------------------------------------------------------

def ensure_boringssl() -> bool:
    need = ("ssl.dll", "crypto.dll")
    if all((NAP_DIR / n).exists() for n in need):
        return True

    roots = [Path(r"C:\Program Files\Tencent\QQNT"),
             Path(r"C:\Program Files (x86)\Tencent\QQNT")]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(sorted(root.glob("versions/*/resources/app")))
        candidates.append(root)

    for name in need:
        if (NAP_DIR / name).exists():
            continue
        for cand in reversed(candidates):           # 后找的先试，版本目录优先
            src = cand / name
            if src.exists():
                try:
                    shutil.copy2(src, NAP_DIR / name)
                    log.info("已从 QQ 目录补齐 %s", name)
                except Exception:
                    log.exception("复制 %s 失败", name)
                break
        else:
            log.error("找不到 %s —— 独立模式无法启动", name)

    return all((NAP_DIR / n).exists() for n in need)


# --------------------------------------------------------------------------
# 启动动作
# --------------------------------------------------------------------------

# 协议层启动冷却：两次拉起之间至少隔这么久。
#
# 为什么必须有（2026-09-28 实测的代价）：
#   `start_protocol()` 原先**没有任何节流**，只要判活认为"进程不在"就拉。
#   而一旦判活出错（旧版只看 pid 文件那一个 pid，很容易被替死鬼顶掉），
#   就会一轮接一轮地拉 —— 实测 15:32/15:33/15:34 三分钟内拉了三次。
#
#   **每多拉一次 = WebUI 多一个登录会话 = 上一个验证链接/二维码当场作废。**
#   用户那边看到的就是「二维码怎么又过期了」「怎么点都点不上」。
#   冷却期的作用是：就算判活偶尔抽风，也不会连着作废用户手上的验证会话。
NAPCAT_START_COOLDOWN = 120.0
_napcat_last_start = 0.0


# --------------------------------------------------------------------------
# 启动方式：带 `-q`（快速登录）还是不带（二维码）
# --------------------------------------------------------------------------
# 实测（2026-09-28）两条路的结果**完全不同**：
#
#   带 `-q <账号>`：快速登录 → 票据失效 → 密码回退 → **QQ 要短信验证码**
#                  → 卡在「等你完成验证」，二维码也不再生成。
#                  要救它必须：开 WebUI 登录页 → 触发短信 → 等短信 → 填码。
#
#   不带 `-q`     ：NapCat 直接进入**二维码登录**，手机扫一下就完事。
#                  而且它每 ~2 分钟自己刷新，扫到成功为止。
#
# 结论：**二维码这条路人做得更快**（扫一下 vs 开网页+等短信+填码），
# 所以在「上一轮已经卡在验证」时，应该主动降级成扫码，而不是一遍遍
# 重试那条注定要人肉验证的密码路。
#
# 注意：这里**不改**「配了免扫码密码」这件事本身 —— 它仍然是票据失效后
# 唯一的自动恢复路径。只是当它已经证明走不通（要验证码）时，别再死磕。
FORCE_QR_FLAG = RUN_DIR / "force_qr_login"
QR_FLAG_TTL = 6 * 3600.0        # 6 小时后自动忘记，回到默认的快速登录


def _use_quick_login(force_qr: bool | None = None) -> bool:
    """该不该带 `-q` 启动。纯函数（除了读那个小 flag 文件）。

    force_qr=True  → 一定不带 -q（走扫码）
    force_qr=None  → 看 flag 文件还在不在（在就扫码，过期/不存在就快速登录）
    """
    if force_qr is not None:
        return not force_qr
    try:
        age = time.time() - FORCE_QR_FLAG.stat().st_mtime
    except OSError:
        return True                    # 没 flag → 照常快速登录
    return age > QR_FLAG_TTL          # flag 太老 → 当它不存在


def mark_need_qr() -> None:
    """记下「快登这条路走不通了，下次改扫码」。"""
    try:
        FORCE_QR_FLAG.write_text(str(time.time()), encoding="ascii")
        log.warning("已切换为「扫码登录」模式 —— 下次启动协议层不再带 -q，"
                    "会直接出二维码（扫码比等短信验证码快得多）。"
                    "6 小时后自动恢复尝试快速登录。")
    except Exception:
        log.exception("写扫码 flag 失败")


def clear_need_qr() -> None:
    """登录成功了 —— 把 flag 清掉，下次回到正常的快速登录。"""
    try:
        if FORCE_QR_FLAG.exists():
            FORCE_QR_FLAG.unlink()
            log.info("登录已恢复，清除「扫码登录」标记")
    except Exception:
        pass


def _maybe_switch_to_qr() -> bool:
    """卡住的原因若是「等短信验证」，就立起 flag 让下次走扫码。

    只在**确实是验证码卡住**时才切换，别的情况（比如单纯等扫码、被风控）
    不该乱动 —— 那些走扫码本来就没用。
    """
    try:
        sc = _sc()
        issue = sc._napcat_login_issue(max_age_hours=1.0)
    except Exception:
        return False
    if not issue or issue[0] != "captcha":
        return False
    if not _use_quick_login():          # 已经是扫码模式了，不用再切
        return False
    mark_need_qr()
    return True


def start_protocol(force: bool = False, force_qr: bool | None = None) -> bool:
    """独立模式启动 NapCat：完全不碰 QQ 客户端。

    `force=True` 给用户主动点的「重启」用（跳过冷却）。
    `force_qr`   见 `_use_quick_login()`。
    """
    global _napcat_last_start
    now = time.time()
    if not force and _napcat_last_start and (now - _napcat_last_start) < NAPCAT_START_COOLDOWN:
        log.warning("协议层刚拉过（%.0f 秒前），冷却期内不重复启动 —— "
                    "避免作废你手上正在用的验证会话",
                    now - _napcat_last_start)
        return False
    if not NAPCAT_NODE.exists() or not (NAP_DIR / NAPCAT_ENTRY).exists():
        log.error("NapCat 文件缺失: %s", NAP_DIR)
        return False
    if not ensure_boringssl():
        return False

    cmd = [str(NAPCAT_NODE), NAPCAT_ENTRY]
    account = read_account()
    quick = _use_quick_login(force_qr)
    mode = "快速登录"
    if account and quick:
        cmd += ["-q", account]     # 独立模式下 -q 有效（注入模式下会被吞掉）
    elif account:
        mode = "扫码登录（上一轮卡在验证，主动降级）"

    console = None
    try:
        console = open(LOG_DIR / "napcat_console.log", "a", encoding="utf-8", errors="replace")
        console.write("\n===== %s 启动协议层（独立模式 · %s）=====\n"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), mode))
        console.flush()
    except Exception:
        if console is not None:
            console.close()
        console = None                  # 拿不到日志文件也得启动，退化成丢弃输出

    try:
        # creationflags 必须带上：否则 node.exe 会在桌面上开一个黑窗口
        proc = subprocess.Popen(cmd, cwd=str(NAP_DIR),
                                stdout=console or subprocess.DEVNULL,
                                stderr=subprocess.STDOUT,
                                creationflags=CHILD_FLAGS)
    except Exception:
        log.exception("拉起协议层失败")
        return False
    finally:
        # Popen 已经把句柄复制给子进程，父进程手上这份必须关掉。
        # 不关的话每拉起一次就漏一个文件句柄 —— 而「掉线就补」意味着
        # 这条路会被反复走到，句柄数会跟着跑很久的机器一起涨。
        if console is not None:
            console.close()

    write_pid("napcat", proc.pid)
    remember_napcat_pid(proc.pid)
    _napcat_last_start = time.time()
    log.info("已启动协议层 pid=%d %s", proc.pid,
             "（快速登录 %s）" % account if account else "（需要扫码登录）")
    return True


def start_bot() -> bool:
    py = pythonw()
    if not py.exists():
        log.error("找不到 pythonw.exe: %s", py)
        return False
    # 起新的之前先把上一份心跳删掉。它是上一个进程留下的遗物，留着只会让
    # 健康探针把"pid 对不上"算到新大脑头上 —— 见 clear_heartbeat() 的注释。
    # 放在这个唯一的"拉起大脑"出口上，任何一条路径都跑不掉这一步。
    clear_heartbeat()
    # 起新的之前先清场：把"多出来的自己人"（上一任的残骸）一并清掉。
    #
    # 闸门：只有**在册的那一个守护进程**才有资格判定谁是多余的。否则一个测试
    # 脚本跑进这条路径，就会把用户正在用的机器人连锅端了。
    if read_pid("daemon") == os.getpid():
        strays = sweep_strays(self_family())
        if strays:
            log.warning("起新大脑前清掉了 %d 个残留进程：%s",
                        len(strays), "、".join(str(p) for p in strays))
    console = None
    try:
        console = open(LOG_DIR / "bot_console.log", "a", encoding="utf-8", errors="replace")
        # 让子进程按 UTF-8 写 stdout / stderr。
        #
        # 不加这一句，Python 会按系统的 ANSI 代码页写（中文 Windows = GBK），
        # 而这个文件是按 UTF-8 打开的 —— 于是 bot_console.log 整片乱码。偏偏
        # 崩溃原因（SyntaxError 之类）就写在这里，_crash_tail() 要读它来告诉
        # 用户"为什么崩了"，乱码等于白搭。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen([str(py), "bot.py"], cwd=str(BASE_DIR),
                                stdout=console, stderr=subprocess.STDOUT,
                                env=env, creationflags=CHILD_FLAGS)
    except Exception:
        log.exception("拉起猫娘大脑失败")
        return False
    finally:
        # 同 start_protocol：子进程已拿到句柄副本，父进程这份要还回去
        if console is not None:
            console.close()
    write_pid("bot", proc.pid)
    log.info("已拉起猫娘大脑 pid=%d", proc.pid)
    return True


def start_ollama() -> bool:
    """确保 Ollama 服务在跑 —— 大脑的推理后端。"""
    if port_open(OLLAMA_PORT):
        return True

    exe = find_ollama()
    if not exe:
        log.error("找不到 ollama.exe，无法自动拉起（大脑会连不上模型）")
        return False

    try:
        proc = subprocess.Popen([str(exe), "serve"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=CHILD_FLAGS)
    except Exception:
        log.exception("拉起 Ollama 失败")
        return False

    write_pid("ollama", proc.pid)
    log.info("已启动 Ollama pid=%d（后端推理服务）", proc.pid)
    return True


def kill_pid(pid: int) -> None:
    # 只杀这一个，**不加 /T**。
    #
    # /T 会连子孙一起杀，而 NapCat 协议层正是守护进程的亲儿子 ——
    # 2026-09-25 加过一次 /T，清僵尸守护进程时顺手把 NapCat 也带走了
    # （端口 3000/3001 当场没了，得重新拉起协议层）。
    # 孤儿的风险改由两处兜住：bot.py 自己认领 pid、start_bot/开机时清场。
    if pid_alive(pid):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=NO_WINDOW)


def ensure_bot(reason: str, kind: str = "missing") -> str:
    """把大脑弄到「在跑」的状态。返回一句结果，调用方只管打日志。

    kind = missing（它不在） / watch（文件变了） / health（判到卡死）
    后两种带 force 语义：活着也要换一份新的。

    两个调用方（主巡检、自愈线程）都从这里走，锁在内部 —— 否则两边同时
    看到"它死了"，就会同时拉起两个实例，两个大脑抢同一个 QQ 号。
    """
    now = time.time()
    force = kind in ("watch", "health")
    p = policy()
    with BOT_LOCK:
        pid = read_pid("bot")
        alive = bot_alive()

        # ── 先把"前任"清出去 ────────────────────────────────────────────
        # 心跳是每 10 秒覆写一次的。如果写它的那个 pid 还活着、又不是当前这个
        # 大脑，那就是上一任没死干净（taskkill 失败、pid 被换过、重启脚本打架…）。
        # 它会把心跳文件一直改写成自己的 pid → 每一个新任都被判"心跳是旧进程"
        # 然后被杀，而它自己永远活着。要杀的从来都是它，不是新一代。
        # 2026-09-24 那晚 40 秒一轮、连杀 6 个大脑的死循环就是这么卡住的。
        stray = _stray_brain_pid(pid)
        if stray:
            log.warning("发现上一任大脑（pid=%d）没死干净、还在写心跳 —— 先清掉它，"
                        "当前这个（pid=%d）留着", stray, pid)
            kill_pid(stray)
            clear_heartbeat()
            time.sleep(BOT_RESTART_GAP)
            if pid_alive(pid):
                # 当前这个活得好好儿的（只是被前任的心跳冤枉了），不必折腾它。
                p.note_alive(now)
                return "已清掉上一任大脑 pid=%d" % stray

        # ── "判死"是锁外的旧结论，在锁里用最新数据复判一次 ──────────────
        # 从探针判到不健康，到真正拿到这把锁，中间可能隔了几十秒；这段时间里
        # 它也许已经被别人换新过了（改完自动重启、主巡检都会走这条路）。
        # 不复判的话，这一刀就砍在一个刚起来的新大脑身上 —— 那正是死循环的
        # 另一半成因。
        if alive and kind == "health":
            again, _why2 = health_verdict(read_heartbeat(), expect_pid=pid,
                                          now=time.time(), cfg=health_cfg(),
                                          alive_for=pid_age(pid))
            if again:
                log.info("健康探针的结论已经过期（它已被换新）—— 不重启")
                p.note_alive(now)
                return ""

        if alive and not force:
            p.note_alive(now)
            return ""

        if not alive:
            # 刚启动就没了 = 崩溃循环的信号。先把线索捞出来，再决定要不要停手。
            if p.note_dead(now):
                log.error("猫娘大脑连着 %d 次起来就死 —— 多半是代码/配置写错了，"
                          "暂停自动重启 %.0f 秒。上次的尾声：%s",
                          RestartPolicy.CRASH_LIMIT, RestartPolicy.BLOCK_SEC,
                          _crash_tail() or "(控制台日志是空的)")

        ok, why = p.allow(now, kind)
        if not ok:
            if not p.warned:            # 只喊一次，别每轮刷屏
                p.warned = True
                log.error("放弃自动重启 —— %s。修好之后双击「重启机器人.bat」；"
                          "原因：%s", why, reason)
            return ""
        p.warned = False

        if pid_alive(pid):
            log.warning("自动重启猫娘大脑 —— %s", reason)
            kill_pid(pid)
            clear_pid("bot")
            # 心跳也一并清掉。留着它，探针下次读到的就是"某个已经不存在的
            # pid"，措辞上像"旧进程"，新大脑反而背了锅。
            clear_heartbeat()
            time.sleep(BOT_RESTART_GAP)      # 等它松开 WS 与文件句柄
        else:
            tail = _crash_tail()
            log.warning("拉起猫娘大脑 —— %s%s", reason,
                        ("；上次的尾声：" + tail) if tail else "")
        if not start_bot():
            return "启动失败"
        p.note_start(time.time())
        p.note_restart(now, kind)
        return "已重启"


def restart_daemon() -> bool:
    """拉起一个新守护进程，**确认它活着之后**再让自己退场。

    顺序很关键：先确认接班人真的起来了，本进程才退出。反过来的话，新进程
    万一起不来就变成"一个守护进程都没有"，而且没有人会知道。

    之所以要 self 重启：daemon.py 的改动没法只靠重启大脑生效 —— 跑着的是
    旧代码。以前只能由用户手动跑「重启机器人.bat」。
    """
    py = pythonw()
    old = read_pid("daemon")
    try:
        subprocess.Popen([str(py), str(Path(__file__).resolve()), "--takeover"],
                         cwd=str(BASE_DIR),
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=DETACHED | CREATE_NEW_GROUP)
    except Exception:
        log.exception("拉起新守护进程失败，本实例继续值守")
        return False

    for _ in range(24):                  # 最多等 12 秒
        time.sleep(0.5)
        new = read_pid("daemon")
        if new and new != old and pid_alive(new):
            global _handed_over
            _handed_over = True
            log.warning("守护进程换班完成：旧 pid %d -> 新 pid %d", old, new)
            return True
    log.error("新守护进程没起来，本实例继续值守（这次改的 daemon.py 暂不生效）")
    return False


# --------------------------------------------------------------------------
# 巡检
# --------------------------------------------------------------------------

def proc_create_time(pid: int) -> float:
    """取进程创建时间（Unix 秒）；取不到返回 0。

    pid 会复用 —— 一个死掉的大脑留下的 pid 文件，可能正好撞上系统里
    新起的另一个进程。用创建时间就能识破。

    走 Win32 API 而不是 `wmic`：新版 Windows 已移除 wmic（Win11 24H2 起），
    调它必然 FileNotFoundError。GetProcessTimes 才是一直都在的那个。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0
    try:
        creation = wintypes.FILETIME()
        exit_t, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not k32.GetProcessTimes(handle, ctypes.byref(creation),
                                   ctypes.byref(exit_t), ctypes.byref(kernel),
                                   ctypes.byref(user)):
            return 0
        # FILETIME：1601-01-01 起的 100ns 数；换成 Unix 纪元秒
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ticks / 10_000_000 - 11644473600
    except Exception:
        return 0
    finally:
        k32.CloseHandle(handle)


class _PROCESSENTRY32(ctypes.Structure):
    """ToolHelp 快照里的一条进程记录（只取我们要的三样：pid / 父 pid / 名字）。"""
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


TH32CS_SNAPPROCESS = 0x00000002
STRAY_MIN_AGE = 3.0     # 比这还新的进程先不动 —— 可能正被人拉起来


def _k32():
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    k.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return k


def proc_exe(pid: int) -> str:
    """进程的可执行文件全路径；查不到返回空串。"""
    k32 = _k32()
    handle = k32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    except Exception:
        return ""
    finally:
        k32.CloseHandle(handle)


def list_processes() -> list:
    """全机进程快照 [(pid, 父pid, exe名)]；取不到就返回空表（调用方据此跳过）。

    走 ToolHelp32，不用 wmic / PowerShell：wmic 在新版 Windows 已被移除，
    PowerShell 也不一定可用（本机实测返回空）。
    """
    k32 = _k32()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID),
                        int(entry.th32ParentProcessID),
                        entry.szExeFile.decode("ascii", "replace")))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    except Exception:
        return out
    finally:
        k32.CloseHandle(snap)
    return out


def parent_of(pid: int) -> int:
    """父进程 pid；查不到返回 0。"""
    for p, ppid, _name in list_processes():
        if p == pid:
            return ppid
    return 0


def project_pids() -> list:
    """本项目自己起的 pythonw 进程（venv 启动器 + 它拉起来的真解释器）。

    识别只靠两样硬证据：
      1. venv `Scripts\\pythonw.exe` 这个启动器路径 —— 本机只有小柚在用；
      2. 父子关系 —— 顺着启动器把它的孩子收进来（那才是真正跑代码的解释器）。

    刻意**不**按"进程名是 pythonw"扫一片：用户机器上还有别的 Python 程序，
    误杀别人比漏杀严重得多。
    """
    launcher = str(pythonw()).lower()
    procs = list_processes()
    if not procs:
        return []
    kids: dict = {}
    for pid, ppid, _name in procs:
        kids.setdefault(ppid, []).append(pid)
    family = set()
    for pid, _ppid, _name in procs:
        if proc_exe(pid).lower() == launcher:
            family.add(pid)
            family.update(kids.get(pid, []))
    return sorted(family)


def self_family() -> set:
    """我自己 + 拉起我的那个启动器 —— 清场时把自己摘出去。"""
    me = os.getpid()
    return {me, parent_of(me)}


def sweep_strays(keep: set | None = None) -> list:
    """清掉"多出来的自己人"，返回被清掉的 pid 列表。

    只在确认自己是**在册的那一个守护进程**时才准动手（闸门见调用处）。
    这不是洁癖：一个大脑没死干净，就会每 10 秒把心跳改写成自己的 pid，
    于是每一任新大脑都被判"心跳是旧进程"然后被杀 —— 永远的 40 秒死循环。
    """
    keep = set(keep or ())
    now = time.time()
    victims = []
    for pid in project_pids():
        if pid in keep:
            continue
        created = proc_create_time(pid)
        if created and (now - created) < STRAY_MIN_AGE:
            continue                      # 太新，可能正被人拉起来
        victims.append(pid)
    for pid in victims:
        kill_pid(pid)
    return victims


def bot_alive() -> bool:
    """大脑进程是否真的在跑。

    `pid_alive(read_pid("bot"))` 单独用不够：pid 文件是我们自己写的，
    进程死掉后文件还在，于是守护进程会一直以为它还活着、永远不去重启。
    这里再核对一次进程创建时间。

    容差取**双侧**：Windows 上 venv 的 pythonw.exe 只是个启动器，它会再拉一个
    真解释器 —— 守护进程记下的是启动器的 pid，而真正跑 `bot.py`、写 pid 文件
    和心跳的是那个孩子。于是"pid 文件写入时间"会比"进程创建时间"晚几秒。
    原先只允许 made >= written - 5，正好把孩子写 pid 的情况判成"死了"。
    """
    pid = read_pid("bot")
    if not pid_alive(pid):
        return False
    made = proc_create_time(pid)
    if not made:
        return True            # 查不到就姑且信 pid 文件
    try:
        written = (RUN_DIR / "bot.pid").stat().st_mtime
    except Exception:
        return True
    return abs(made - written) <= PID_STAMP_TOLERANCE


def tick() -> None:
    global _stuck_since, _stuck_last_log

    # ① 推理后端：只有本地 Ollama 模式才需要它。
    #    云端模式下这一环不存在，直接视为就绪 —— 否则守护进程会一直去拉
    #    一个根本用不上的 Ollama，白白占着进程。
    local = backend_is_local()
    backend_ready = True
    if local:
        backend_ready = port_open(OLLAMA_PORT)
        if not backend_ready:
            if pid_alive(read_pid("ollama")):
                pass          # 进程在、端口还没起来 → 正在加载，别重复启动
            else:
                log.info("Ollama 未就绪，尝试启动")
                start_ollama()

    # ② 协议层没就绪：进程还在说明正在登录途中，别重复启动；
    #    不在就拉起来 —— 不等后续轮次，同一轮就把整条链补齐。
    if not port_open(3000):
        if not napcat_alive():
            _stuck_since = None
            log.info("协议层未就绪且进程不在，尝试启动")
            start_protocol()
        else:
            # 进程在、端口不开 —— 典型是「等扫码」或「账号被风控」。
            # 这里**不能**重启：重启会让已经刷出来的二维码作废，用户永远扫不上。
            # 但也不能一声不吭，否则日志全静音，故障就只能靠用户猜。
            now = time.time()
            if _stuck_since is None:
                _stuck_since = now
            waited = now - _stuck_since
            if waited >= STUCK_WARN_AFTER and now - _stuck_last_log >= STUCK_LOG_EVERY:
                _stuck_last_log = now
                log.warning(
                    "协议层进程在、端口 3000 已 %d 分钟不开 —— 多半卡在 QQ 登录。"
                    "跑「查看状态.bat」看具体原因（登录态失效 / 账号被风控）；"
                    "该扫码就双击「扫码登录.bat」。守护进程不会重启协议层，"
                    "以免作废当前二维码。",
                    int(waited // 60),
                )
            # 光写日志没用 —— 日志只有翻的人看得见。卡够时间就弹窗告诉主人。
            if waited >= STUCK_WARN_AFTER:
                notify_stuck_protocol(True)
                # 卡的是「等短信验证」还是「等扫码」？两者救法完全不同：
                #   等验证 → 必须去 WebUI 点，扫码没用（配了密码回退后它不出码）
                #   等扫码 → 手机扫一下就行
                # 如果是前者，就把「下次改走扫码」的 flag 立起来 —— 那意味着
                # 你只要重启一次（或者它下次自己拉），就会拿到一张能扫的二维码，
                # 省掉开网页 + 等短信 + 填验证码这一整套。
                _maybe_switch_to_qr()
        return

    _stuck_since = None        # 端口起来了，计数归零
    # 端口起来 = 有人扫上码 / 自动登录成功 → 把"欠着的提醒"清掉，
    # 免得她已经恢复了还去告诉主人"她掉线了"。
    notify_stuck_protocol(False)
    # 同理：登录既然恢复了，就把「下次走扫码」的 flag 撤掉 ——
    # 否则一次偶发的验证卡顿会让此后每次启动都退化成扫码，白丢免扫码能力。
    clear_need_qr()

    # ③ 协议层就绪 → 保证大脑在跑。
    #    本地模式下还要求 Ollama 也已就绪：否则大脑起来立刻会因连不上模型而报错，
    #    不如等后端准备好再拉，少一轮无谓的错误日志。
    #    云端模式没有这个前置条件（backend_ready 恒为 True）。
    if not backend_ready:
        log.info("协议层已就绪，但 Ollama 还没好，暂缓拉起大脑")
        return

    # ④ 它是不是"活着但不干活"，由健康探针线程负责，这里不管。
    ensure_bot("巡检发现它不在", "missing")


def _watch_worker() -> None:
    """盯着「改了就该重启」的那几个文件。

    只重启大脑、**不动协议层** —— 协议层重启有可能要重新扫码登录，
    为了一个函数改名去冒掉线的风险不划算。
    """
    prev = watch_files()
    pending = None                 # (第一次看到改动的时刻, 文件名列表)
    last_act = 0.0
    while True:
        cfg = watch_cfg()
        if _stop.wait(cfg["interval"]):
            return
        now = time.time()
        cur = watch_files()
        if not cfg["enabled"]:
            prev, pending = cur, None    # 关着也保持基线，免得一打开就误报
            continue

        changed = changed_files(prev, cur)
        if changed:
            prev = cur                   # 先收下基线，剩下的交给 settle 去等
            pending = (now, changed)
            continue
        if pending is None:
            continue
        at, names = pending
        if now - at < cfg["settle"] or now - last_act < cfg["cooldown"]:
            continue
        pending = None
        last_act = now

        bot_hits = [n for n in names if n in WATCH_BOT_FILES]
        daemon_hits = [n for n in names if n in WATCH_DAEMON_FILES]

        if bot_hits:
            newest = max((cur.get(n, 0.0) for n in bot_hits), default=0.0)
            started = proc_create_time(read_pid("bot"))
            if started and newest and started >= newest:
                # 大脑比文件还新 = 它启动的时候这份改动已经在里面了
                # （比如设置页刚替你重启过），别再折腾一次。
                log.info("检测到 %s 有改动，但大脑是改完之后才起来的，跳过重启",
                         "、".join(bot_hits))
            else:
                log.info("检测到 %s 有改动 —— 自动重启大脑，让改动生效",
                         "、".join(bot_hits))
                ensure_bot("改了 " + "、".join(bot_hits), "watch")

        if daemon_hits:
            log.warning("检测到 %s 有改动 —— 换成新的守护进程（大脑不受影响）",
                        "、".join(daemon_hits))
            if restart_daemon():
                _stop.set()              # 班交出去了，本线程与主循环一起收工
                return


def _health_worker() -> None:
    """按心跳判断大脑有没有卡死。

    只管「活着但不干活」这一种情形 —— 它要是压根不在，那是主巡检的事。
    """
    fails = 0
    while True:
        cfg = health_cfg()
        policy().max_per_hour = cfg["max_per_hour"]
        if _stop.wait(cfg["interval"]):
            return
        if not cfg["enabled"]:
            fails = 0
            continue
        if not bot_alive():
            fails = 0
            continue
        expect = read_pid("bot")
        ok, why = health_verdict(read_heartbeat(), expect_pid=expect,
                                 now=time.time(), cfg=cfg,
                                 alive_for=pid_age(expect),
                                 stale_pid_alive=bool(_stray_brain_pid(expect)))
        if ok:
            fails = 0
            continue
        fails += 1
        log.warning("健康探针：%s（第 %d/%d 次）", why, fails, HEALTH_FAILS_TO_ACT)
        if fails < HEALTH_FAILS_TO_ACT:
            continue
        fails = 0
        ensure_bot("健康探针：%s" % why, "health")


def everything_ready() -> bool:
    """整条链是否都就绪：协议层 / 大脑（+ 本地模式下的推理后端）。"""
    backend = port_open(OLLAMA_PORT) if backend_is_local() else True
    return backend and port_open(3000) and port_open(3001) and bot_alive()


def main(takeover: bool = False) -> int:
    if not takeover and pid_alive(read_pid("daemon")):
        log.info("已有守护进程在跑，本实例退出")
        return 0

    write_pid("daemon", os.getpid())
    log.info("守护进程启动 pid=%d 巡检间隔 %ds（未就绪时 %ds）%s",
             os.getpid(), POLL_SECONDS, POLL_FAST,
             "（接管模式：换班上来的一代）" if takeover else "")
    log.info("工作目录 %s", BASE_DIR)
    if backend_is_local():
        log.info("推理后端 本地 Ollama（11434）")
    else:
        log.info("推理后端 云端 API（provider=%s）—— 不巡检、不拉起 Ollama",
                 llm_provider())

    w = watch_cfg()
    h = health_cfg()
    log.info("自愈一：改完自动重启 %s —— 每 %g 秒扫一次 %s",
             "开" if w["enabled"] else "关", w["interval"],
             "、".join(WATCH_BOT_FILES))
    log.info("自愈二：卡死自动重启 %s —— 每 %g 秒体检一次（心跳 %g 秒不更新 / "
             "一条消息超过 %g 秒 / 断连 %g 秒 都算有事）",
             "开" if h["enabled"] else "关", h["interval"],
             h["hb_stale"], h["busy_max"], h["ws_down"])

    # 启动清场：把上一代留下的残骸清掉（僵尸大脑、没退干净的旧守护进程）。
    # 冷启动时最该做这一步 —— 留着它们，就会有别的进程跟你抢同一个 QQ 号、
    # 还会一直把心跳改写成自己的 pid，把新大脑一张一张冤枉死。
    strays = sweep_strays(self_family())
    if strays:
        log.warning("启动清场：清掉了 %d 个残留进程（%s）",
                    len(strays), "、".join(str(p) for p in strays))

    _stop.clear()
    threading.Thread(target=_watch_worker, name="watch", daemon=True).start()
    threading.Thread(target=_health_worker, name="health", daemon=True).start()

    was_ready = False
    try:
        # 注意这里是 `while not _stop.is_set()`，不是 `while True`。
        #
        # 换班（restart_daemon）时看门线程会 set 这个事件，主循环必须能醒过来
        # 退出。以前写的是 `while True` + `time.sleep` —— 于是"交班"只是嘴上
        # 说说：旧守护进程根本不会退出，一代一代全活着，一起抢着重启大脑。
        # 2026-09-25 上午叠出了 14 个进程的僵尸链，脑死循环就是它引发的。
        while not _stop.is_set():
            try:
                tick()
                HEARTBEAT.write_text(str(int(time.time())), encoding="ascii")

                # 自适应节奏：没就绪就快跑，全绿了就慢下来
                ready = everything_ready()
                if ready and not was_ready:
                    chain = ("Ollama / 协议层 / 大脑" if backend_is_local()
                             else "云端 API / 协议层 / 大脑")
                    log.info("链路已全部就绪（%s）", chain)
                elif was_ready and not ready:
                    log.warning("链路有组件掉线，转入快速巡检")
                was_ready = ready
            except Exception:
                log.exception("巡检出错（已跳过，下轮继续）")
                ready = False
            # 用 _stop.wait 睡，换班时能被立刻叫醒，不用等满这一轮
            if _stop.wait(POLL_FAST if not ready else POLL_SECONDS):
                break
    except KeyboardInterrupt:
        log.info("收到中断，守护进程退出")
    finally:
        _stop.set()
        if _handed_over:
            # 班已经交给新实例，pid 文件里写的是它的号 —— 这里一删，新守护
            # 进程就成了"没有 pid 文件的野进程"，状态面板会谎报"没在跑"。
            log.info("本实例退出（守护进程已由新实例接管）")
        else:
            try:
                (RUN_DIR / "daemon.pid").unlink(missing_ok=True)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main("--takeover" in sys.argv))
