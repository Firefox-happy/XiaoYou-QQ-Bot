# -*- coding: utf-8 -*-
"""
小柚 · 控制面板
================

    python service_ctl.py start       后台启动（无窗口常驻）
    python service_ctl.py stop        停止机器人
    python service_ctl.py status      查看运行状态
    python service_ctl.py install     安装开机自启
    python service_ctl.py uninstall   卸载开机自启
    python service_ctl.py logs        打开日志目录

所有逻辑都在这里，bat 只是薄壳 —— 与 launch.py 同一套约定。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

def _setup_console() -> None:
    """让中文能在 Windows 控制台里正常显示（框线字符也不至于变成乱码）。

    ⚠️ 这段**必须在 main() 里调，不能放在模块顶层** —— 放顶层的话，
    **任何 `import service_ctl` 的进程都会被顺手改掉 stdout 编码**，
    它自己的输出当场变 GBK 乱码。已经真踩过一次：测试脚本 import 了它，
    整份测试输出全成了乱码，排查半天才发现源头在这儿。
    模块被 import 时不该有这种副作用。
    """
    try:
        sys.stdout.reconfigure(encoding="gbk", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="gbk", errors="replace", line_buffering=True)
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
RUN_DIR = BASE_DIR / "run"
LOG_DIR = BASE_DIR / "logs"
DAEMON = BASE_DIR / "daemon.py"

VENV_PYTHONW = Path(r"C:\Users\26415\.workbuddy\binaries\python\envs\catbot\Scripts\pythonw.exe")

STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
AUTOSTART_NAME = "小柚QQ猫娘机器人.vbs"      # VBS：wscript 无窗口，开机连一闪都没有
AUTOSTART_LEGACY = "小柚QQ猫娘机器人.bat"    # 旧版 bat，安装时顺手清掉

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
BAR = "=" * 58


def pythonw() -> Path:
    if VENV_PYTHONW.exists():
        return VENV_PYTHONW
    cand = Path(sys.executable).with_name("pythonw.exe")
    return cand if cand.exists() else Path(sys.executable)


def head(text: str) -> None:
    print(BAR)
    print("  " + text)
    print(BAR)


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        k32.CloseHandle(handle)


def read_pid(name: str) -> int:
    try:
        return int((RUN_DIR / f"{name}.pid").read_text(encoding="ascii").strip())
    except Exception:
        return 0


# --------------------------------------------------------------------------
# 协议层「还活着吗」—— 单一真源（daemon / settings_page 都委托这里）
# --------------------------------------------------------------------------
# 为什么不能只认 `run/napcat.pid` 那一个 pid（2026-09-28 实测的代价）：
#
#   独立模式下 NapCat 是「主进程 + Fork Worker」**两个 node 进程**，
#   而 pid 文件只能记一个、且写入是无条件覆盖。于是只要有任何别的路径
#   （手动跑的脚本、旧的残留进程）往 pid 文件里写了一个很快退出的 pid，
#   守护进程下一轮巡检就会判定「协议层不在」→ 再拉一个。
#
#   **每多拉一次 = WebUI 多一个登录会话 = 上一个验证链接/二维码当场作废。**
#   用户看到的现象就是「二维码怎么又过期了」「怎么点都点不上」——
#   而根因只是 pid 文件指向了一个替死鬼。
#
# 所以改成记一份**清单**：所有活着、且确实是 node.exe 的都算数，
# 只要还剩任意一个，协议层就没死。
NAPCAT_PIDS_FILE = RUN_DIR / "napcat_pids.json"


def proc_name(pid: int) -> str:
    """取进程映像名（小写，不含路径）。取不到返回空串。

    ⚠️ 故意**不调 `tasklist`**：那是额外起一个进程，且输出受系统语言影响
    （中文系统表头是「映像名称」），又慢又脆。QueryFullProcessImageNameW
    是同一份数据，直接问内核拿。
    """
    if not pid or pid <= 0:
        return ""
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    except Exception:
        return ""
    finally:
        k32.CloseHandle(h)


def read_napcat_pids() -> list[int]:
    """NapCat 已知 pid 清单（含 pid 文件里那个）。读不到就退回只剩 pid 文件。"""
    out: list[int] = []
    try:
        data = json.loads(NAPCAT_PIDS_FILE.read_text(encoding="utf-8"))
        for x in (data.get("pids") or []):
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    one = read_pid("napcat")
    if one and one not in out:
        out.append(one)
    return out


def write_napcat_pids(pids: list[int]) -> None:
    """记下协议层的 pid 清单（去重、只留正整数）。"""
    uniq = sorted({int(p) for p in pids if int(p) > 0})
    try:
        NAPCAT_PIDS_FILE.write_text(
            json.dumps({"pids": uniq}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def remember_napcat_pid(pid: int) -> None:
    """新拉起一个 NapCat → 记进清单（**不覆盖**已有活着的那些）。

    ⚠️ 必须同时更新 pid 文件和清单：pid 文件是给 WebUI/别的工具看的"当前那个"，
    清单才是判活依据。只写其中一个都会退回到「单一 pid」的老坑。
    """
    if not pid or pid <= 0:
        return
    keep = [p for p in read_napcat_pids() if p != read_pid("napcat")]
    keep = [p for p in keep if pid_alive(p) and proc_name(p) in ("node.exe", "")]
    keep.append(pid)
    write_napcat_pids(keep)
    try:
        (RUN_DIR / "napcat.pid").write_text(str(pid), encoding="ascii")
    except Exception:
        pass


def napcat_alive() -> bool:
    """协议层进程还活着吗 —— 认**清单里任意一个**，不是只认 pid 文件那一个。

    顺带把清单里的死 pid 清掉，免得越攒越长、每轮巡检都要去 probe 一堆僵尸。
    """
    want = read_napcat_pids()
    alive = [p for p in want
             if pid_alive(p) and proc_name(p) in ("node.exe", "")]
    if alive != want:
        write_napcat_pids(alive)
    return bool(alive)


def napcat_pid() -> int:
    """协议层「当前那个」 pid —— 优先给活着的清单项，其次 pid 文件。

    面板上只想显示一个数字时用它；判活请用 `napcat_alive()`。
    """
    for p in read_napcat_pids():
        if pid_alive(p):
            return p
    return read_pid("napcat")


def taskkill_pid(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def clear_pid(name: str) -> None:
    try:
        (RUN_DIR / f"{name}.pid").unlink(missing_ok=True)
    except Exception:
        pass


def heartbeat_age() -> float | None:
    try:
        return time.time() - int((RUN_DIR / "daemon.heartbeat").read_text().strip())
    except Exception:
        return None


def protocol_up() -> bool:
    import socket
    for port in (3000, 3001):
        try:
            with socket.socket() as s:
                s.settimeout(1.0)
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return False
        except Exception:
            return False
    return True


def ollama_up() -> bool:
    """本地推理后端（Ollama）是否在监听。它挂了 = 能收消息但永远不回。"""
    import socket
    try:
        with socket.socket() as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", 11434)) == 0
    except Exception:
        return False


# ---- 推理后端可能是云端的 ------------------------------------------------
#
# config.json 的 llm.provider 写 openai 时，推理跑在云端，Ollama 与本次运行无关。
# 状态面板必须跟着变 —— 否则它会一边显示"Ollama 没在跑"骗人，
# 一边让用户去修一个根本不需要的东西。

CONFIG_FILE = BASE_DIR / "config.json"


def llm_cfg() -> dict:
    """读 config.json 的 llm 段；读不到返回空 dict。"""
    try:
        import json
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return cfg.get("llm") or {}
    except Exception:
        return {}


def llm_provider() -> str:
    return str(llm_cfg().get("provider", "ollama")).strip().lower()


def llm_model() -> str:
    return str(llm_cfg().get("model") or "?")


def tools_cfg() -> dict:
    """读 config.json 的 tools 段（小柚的"手脚"配置）。"""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return cfg.get("tools") or {}
    except Exception:
        return {}


def cfg_section(name: str) -> dict:
    """读 config.json 的任意一段；读不到返回空 dict。"""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return cfg.get(name) or {}
    except Exception:
        return {}


def voice_state() -> tuple[bool, bool]:
    """语音能不能听、能不能说。

    这里**故意不 import voice 模块** —— 面板可能跑在一个没装 vosk /
    imageio-ffmpeg 的解释器下，一 import 就直接报错，把状态面板整个弄崩。
    所以只做无依赖的检查：模型文件在不在、有没有可用的合成嗓子。
    """
    listen = (BASE_DIR / "models" / "vosk-model-small-cn-0.22"
              / "am" / "final.mdl").exists()

    # 说：装了 edge-tts 就算能说 —— 它是联网的，真发的时候可能失败，
    # 那时 voice.py 会自动退回系统语音，所以这里不因为"可能断网"就判否。
    speak = False
    try:
        import importlib.util
        speak = importlib.util.find_spec("edge_tts") is not None
    except Exception:
        speak = False

    # 退一步：Edge 没装，看看系统自带的中文语音包在不在
    if not speak:
        try:
            ps = ("Add-Type -AssemblyName System.Speech;"
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  "($s.GetInstalledVoices()|ForEach-Object{$_.VoiceInfo.Name}) -join ','")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=20)
            out = r.stdout or ""
            speak = any(k in out for k in ("Huihui", "Yaoyao", "Xiaoxiao", "Xiaoyi"))
        except Exception:
            pass
    return listen, speak


def voice_cfg() -> dict:
    """读 config.json 的 voice 段；读不到返回空 dict。"""
    try:
        import json
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return cfg.get("voice") or {}
    except Exception:
        return {}


def tts_engine_name() -> str:
    """当前会用哪个嗓子，给面板显示。

    ⚠️ 这里以前是**自己判断**的：只要 `edge_tts` 能 import 就报「Edge TTS」。
    于是加上本机 GPT-SoVITS 之后，面板**照旧报 Edge** —— 服务明明在跑、
    显示却是错的。**同一个判断在两处各写一遍，就一定有一处会过期。**

    现在改成直接问 `voice`：挑引擎的顺序（本机克隆 → Edge → 系统语音）
    只有它知道，它是唯一真源。实测 `import voice` 只要 0.02 秒 ——
    当初"不 import voice 省时间"的顾虑本身就是多余的（vosk 那 42MB 模型
    是懒加载的，这一句根本不碰它）。
    """
    try:
        import voice
        return voice.describe_engine(voice_cfg())
    except Exception:
        pass
    # voice 都 import 不进来（比如依赖缺了）时的兜底：报个"大概能用哪个"
    try:
        import importlib.util
        if importlib.util.find_spec("edge_tts") is not None:
            return "Edge TTS"
    except Exception:
        pass
    return "系统语音"


def cloud_endpoint() -> tuple[str, int]:
    """从 api_base 解析出 (host, port)，供连通性探测用。"""
    from urllib.parse import urlparse
    base = str(llm_cfg().get("api_base") or "").strip()
    if not base:
        return "", 0
    u = urlparse(base if "//" in base else "https://" + base)
    port = u.port or (443 if (u.scheme or "https") == "https" else 80)
    return u.hostname or "", port


def cloud_up() -> tuple[bool, str]:
    """云端 API 的 host 是否可达。

    只做 TCP 连通性 —— 够用来区分"网断了"和"Key/模型配错了"这两种情况，
    而且不花一分钱、不泄露任何聊天内容。
    """
    import socket
    host, port = cloud_endpoint()
    if not host:
        return False, "api_base 未填写"
    try:
        with socket.socket() as s:
            s.settimeout(4)
            if s.connect_ex((host, port)) == 0:
                return True, "%s:%d 可达" % (host, port)
    except Exception as e:
        return False, "%s:%d 不通（%s）" % (host, port, type(e).__name__)
    return False, "%s:%d 不通" % (host, port)


# --------------------------------------------------------------------------


def cmd_start() -> int:
    head("小柚 · 后台启动")
    dpid = read_pid("daemon")
    if pid_alive(dpid):
        print("  守护进程已经在跑了（pid %d），不用重复启动。" % dpid)
        print()
        print("  它会自己维持协议层和猫娘大脑。")
        return 0

    py = pythonw()
    if not py.exists():
        print("  [错误] 找不到 pythonw.exe：%s" % py)
        return 1

    # DETACHED_PROCESS：守护进程完全脱离本窗口的控制台，
    # 之后关掉这个 cmd（或它自己闪退）都带不走它。
    subprocess.Popen([str(py), str(DAEMON)], cwd=str(BASE_DIR),
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=DETACHED | CREATE_NEW_GROUP)
    time.sleep(2)

    dpid = read_pid("daemon")
    if pid_alive(dpid):
        print("  守护进程已启动（pid %d），没有窗口。" % dpid)
        print()
        print("  接下来它会自动完成：拉起 NapCat 协议层 -> 连上猫娘大脑。")
        print("  大约 15~30 秒后可以跑「查看状态.bat」确认。")
        print()
        print("  日志在 catbot\\logs\\ 下：daemon.log / napcat_console.log / bot.log")
        return 0
    print("  [警告] 守护进程没起来，去看 catbot\\logs\\daemon.log")
    return 1


def _do_stop(keep_ollama: bool = True) -> list[str]:
    """停掉整条链路，返回被停掉的描述。

    顺序重要：先干掉守护进程，否则它下一轮巡检会把组件又拉回来。

    Ollama 默认**不杀** —— 它可能是你自己另外装的、别的程序也在用，
    贸然杀掉会连累别人。想彻底停就用 stop 的完整模式。

    ⚠️ 协议层必须杀掉**全部已知 pid**，不能只杀 pid 文件里那一个：
    独立模式下 NapCat 是「主进程 + Fork Worker」两个 node，只杀主进程会留下
    Worker 孤儿（实测 2026-09-28：stop 之后仍有 3 个 node 在跑）。
    残留的 Worker 会占着 3000/3001 端口或让下次启动行为诡异 —— 这也是
    「二维码莫名过期」的来源之一。
    """
    order = [("daemon", "守护进程"), ("bot", "猫娘大脑")]
    if not keep_ollama:
        order.append(("ollama", "推理后端"))

    stopped = []
    for name, label in order:
        pid = read_pid(name)
        if taskkill_pid(pid):
            stopped.append("%s pid %d" % (label, pid))
        clear_pid(name)

    # 协议层单独处理：清单一网打尽
    for pid in read_napcat_pids():
        if taskkill_pid(pid):
            stopped.append("协议层 pid %d" % pid)
    clear_pid("napcat")
    try:
        NAPCAT_PIDS_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    return stopped


def cmd_stop() -> int:
    head("小柚 · 停止机器人")
    stopped = _do_stop(keep_ollama=True)

    if stopped:
        print("  已停止：" + "、".join(stopped))
    else:
        print("  本来就没在跑。")
    print()
    if ollama_up():
        print("  注：Ollama 保持运行（它可能还被别的程序用着）。")
        print("      想连它一起停，跑 python service_ctl.py stopall")
    print("  想重新起来：双击「一键启动.bat」。")
    print("  注意：这样不会影响你的 QQ —— 独立模式不碰 QQ 客户端。")
    return 0


def cmd_restart() -> int:
    """停掉再拉起 —— 改了代码/配置后用这个让改动生效。"""
    head("小柚 · 重启机器人")
    stopped = _do_stop(keep_ollama=True)
    if stopped:
        print("  已停止：" + "、".join(stopped))
    else:
        print("  之前没在跑，直接启动。")
    print()
    # 协议层端口不会瞬间释放，稍等一下再拉起
    time.sleep(3)
    print("  重新拉起中……")
    return cmd_start()


def cmd_stopall() -> int:
    """连 Ollama 一起停 —— 彻底清场时用。"""
    head("小柚 · 全部停止（含推理后端）")
    stopped = _do_stop(keep_ollama=False)
    if stopped:
        print("  已停止：" + "、".join(stopped))
    else:
        print("  本来就没在跑。")
    print()
    print("  注意：Ollama 被停掉后，其它依赖它的程序也会连不上。")
    return 0


# NapCat 登录失败的几种「长相」。它们在外部表现上**完全一样**：
# 进程活着、3000 端口不起、二维码文件还在刷新 —— 光看进程和端口分不出来，
# 所以必须去读它自己的控制台日志。
#
# 元组顺序 = 优先级：同一个时间窗口里出现多个时，取最靠前的那个。
_LOGIN_EVENTS = (
    ("risk", ("存在安全风险", "serverErrorCode")),   # QQ 风控拒绝，扫码也没用
    ("captcha", ("需要验证码",)),                    # 密码登录被要求短信验证（人工点一下即可）
    # 快速登录失败有**两种措辞**，都要认（只写一种会漏掉另一条路径）：
    #   「快速登录错误： 登录态已失效，请重新登录。」
    #   「快速登录错误： 你的用户身份已失效，为保证账号安全，请你重新登录。」
    ("quickfail", ("登录态已失效", "身份已失效")),
    ("scanned", ("二维码已被扫描",)),                 # 扫了但服务器没放行
)

# 「账号被踢下线」不是登录失败，而是**失败的起因**：
# 被踢 → NapCat 重启 Worker → 票据/会话全没了 → 才要重新登录。
# 单独认它，面板才能回答"为什么老是要我重新登录"。
_KICK_KEYS = ("KickedOffLine", "下线通知")


def _line_time(line: str) -> float | None:
    """把日志行里的 `09-24 07:44:02` 解析成 Unix 秒；没有就返回 None。"""
    # ⚠️ 边界断言用 (?<!\d) / (?!\d)，**不要用 \b**。
    # \b 要求前一字符是"非单词字符"，可 ANSI 颜色码结尾是字母 m
    # （`\x1b[31m09-25 ...`），m 和 0 都是单词字符 → 没有 \b → 整行时间戳解析失败。
    # 这会让带颜色码的日志行全部被当成"没有时间戳"而丢弃。测试抓到过一次。
    m = re.search(r"(?<!\d)(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?!\d)", line)
    if not m:
        return None
    mo, day, hh, mm, ss = (int(x) for x in m.groups())
    now = time.localtime()
    for year in (now.tm_year, now.tm_year - 1):      # 跨年时回退到上一年
        try:
            t = time.mktime((year, mo, day, hh, mm, ss, 0, 0, -1))
        except (ValueError, OverflowError):
            continue
        if t <= time.time() + 86400:
            return t
    return None


def _napcat_login_issue(max_age_hours: float = 6.0):
    """读 NapCat 控制台日志，找出最近一次登录失败的**真实原因**。

    这是典型的「静默失败」：NapCat 处在「等扫码」「扫码被拒」「快速登录失败」
    三种状态时，外部表现一模一样。以前状态面板只看外部表现，于是一律提示
    「双击扫码登录.bat」—— 可当账号被 QQ 风控拒绝时，用户会一遍遍扫、一遍遍
    失败，还以为是自己操作有问题。而日志里其实写得清清楚楚。

    返回 (kind, 详情, 距今小时)；没有有效记录返回 None。
    """
    return _parse_login_log(_tail_text(LOG_DIR / "napcat_console.log"),
                            max_age_hours=max_age_hours)["issue"]


def _tail_text(path, tail_bytes: int = 512 * 1024) -> str:
    """读文件尾部若干字节并解码。日志能长到几 MB，没必要整份读。

    逐行解码：先按 utf-8，失败的行再按 gbk —— NapCat 混着写两种编码。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            blob = f.read()
    except OSError:
        return ""
    out = []
    for raw in blob.split(b"\n"):
        line = re.sub(rb"\x1b\[[0-9;]*m", b"", raw)   # 去掉 ANSI 颜色码
        try:
            out.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(line.decode("gbk", "replace"))
    return "\n".join(out)


def _kick_stats(kick_ts, now: float, gap_floor_min: float = 5.0) -> dict:
    """（纯函数）把一串"被踢时间戳"算成可用于**判断趋势**的统计量。

    为什么需要它：用户反复问「还会不会掉线」。以前面板只能报"最近 24h 被踢 N 次"——
    这是**瞬时值**，看不出好转还是恶化。本机实测过 `o3HookMode` 开关的假改善
    （拿到一个 6.5h 的长间隔就以为治好了，结果当天就塌回 1.3h），所以趋势必须由
    数据自己说话，而不是由人挑一个好看的数字。

    参数：
      kick_ts        被踢时间戳（秒），可乱序、可重复
      now            当前时间
      gap_floor_min  间隔下限（分钟）：同一轮抖动会在几秒内重复记录，按 5 分钟归并

    返回：
      gaps_h       相邻间隔（小时），升序；不足两段则为空列表
      uptime_h     当前存活时长（小时）；从没被踢过则 None
      median_h     间隔中位数（小时）；若含存活段则指的是**历史段**；不足两段则 None
      base_h       历史基准（较早那些段的中位）
      recent_h     最近 3 段（**含当前存活段**）的中位
      trend        "improving" / "worsening" / "flat" / "unknown"
    """
    ts = sorted(set(kick_ts))
    merged: list[float] = []
    for t in ts:
        if not merged or t - merged[-1] > gap_floor_min * 60.0:
            merged.append(t)

    # ⚠️ 时间序和展示序必须分开。
    # 一开始我在排序后的 gaps 上取 `[-3:]` 当"最近三段"，实际拿到的是**最大的三段**，
    # 于是「恶化」被判成「好转」（自测当场抓到）。
    # 所以：chrono 保留时间顺序用于算趋势，gaps 排序后用于展示中位/最值。
    chrono = [(merged[i] - merged[i - 1]) / 3600.0 for i in range(1, len(merged))]
    gaps = sorted(chrono)

    uptime = (now - merged[-1]) / 3600.0 if merged else None
    median = gaps[len(gaps) // 2] if len(gaps) >= 2 else None

    # 趋势判定的基准：**较早**那些段的中位。
    # ⚠️ 不能用「整体中位」——整体里已经含了最近这几段，短命段会把基准一起拖低，
    # 结果 ratio ≈ 1 恒判 flat（自测当场抓到：历史 5h → 最近 1h 被判成 flat）。
    # 用中位而不是均值 —— 被踢间隔是重尾分布（本机样本里有 33h 这种离群值），
    # 均值会被一个长间隔整体拉高，掩盖短命段。本机 10 段样本：中位 2.97h / 均值 6.13h。
    base = None
    if len(chrono) >= 3:
        head = sorted(chrono[:-2] if len(chrono) > 3 else chrono[:-1])
        base = head[len(head) // 2]
    elif chrono:
        base = chrono[0]

    recent = None
    if len(chrono) >= 3:
        recent = sorted(chrono[-2:])[0]          # 最近两段取小的：偏保守

    trend = "unknown"
    if base is not None and base > 0:
        # ⚠️⚠️ 规则一（优先）：当前这轮存活显著长于历史基准 → 直接算好转。
        # 为什么不能让存活段混进"最近三段求中位"：三段的中位数太粗糙，
        # `[2, 2, 36]` 的中位是 2 —— 明明已经活了 36 小时，却被前面两段 2h 淹没，
        # 照样报「恶化」。这正是 2026-09-27 实测时面板显示 18.9h 存活却打
        # `[!!] 恶化` 的原因。存活段是唯一"还在进行中"的观测，它代表现状，
        # 语义上就该压过历史段，而不是去跟中位搏斗。
        if uptime is not None and uptime >= base * 1.5:
            trend = "improving"
        elif recent is not None:
            ratio = recent / base
            if ratio >= 1.5:
                trend = "improving"
            elif ratio <= 0.67:
                trend = "worsening"
            else:
                trend = "flat"
        else:
            trend = "flat"

    return {
        "gaps_h": gaps,
        "uptime_h": uptime,
        "median_h": median,
        "recent_h": recent,
        "base_h": base,
        "trend": trend,
    }


def _parse_login_log(text: str, now: float | None = None,
                     max_age_hours: float = 6.0) -> dict:
    """（纯函数）从 NapCat 控制台日志文本里还原「最近一次登录现场」。

    单独拆成纯函数是为了**能测** —— 喂一段构造好的日志文本就行，
    不用真去起一个 NapCat。这个项目已经吃够了「日志解析逻辑测不了、
    只能靠人手看」的亏。

    返回的字典：
      issue        (kind, detail, 距今小时) 最值得报告的那条；没有则 None
      captcha_url  短信验证链接（QQ 要求人工验证时才有）
      captcha_min  那条验证记录距今几分钟
      last_kick_min 账号最近一次被踢下线距今几分钟
      last_ok_min  账号最近一次**登录成功**距今几分钟（用来判断被踢后是否又登上去过）
      kicks_24h    最近 24 小时被踢了几次
      kick_gaps_h  相邻两次被踢的间隔（小时）列表，按时间升序；不足两次则空
      uptime_h     当前这轮存活了多久（小时）＝ 距最近一次被踢；没被踢过则 None
      gap_median_h 历史被踢间隔的中位数（小时）；样本不足则 None
    """
    now = time.time() if now is None else now
    deadline = now - max_age_hours * 3600.0
    kick_deadline = now - 24 * 3600.0

    best = None                                       # (优先级, 时间, kind, 详情)
    captcha_url, captcha_ts = None, 0.0
    last_kick, kicks_24h = None, 0
    # 被踢时间戳**全量**收集 —— 不能只留最新那条。
    # 用户问的「预计后续稳定性」需要一个趋势，而不是一个瞬时值；
    # 这个项目已经栽过太多次「拿单点当结论」的跟头（o3HookMode 曾被误判为改善）。
    # 收集全部之后交给 _kick_stats() 去算间隔与存活时长。
    kick_ts: list[float] = []
    # 「最近一次登录成功」—— 用来判断**被踢之后有没有又登上去过**。
    # 没有它面板就会说错话：2026-09-26 实测，16:24 被踢、16:27 就扫码回来了、
    # 17:41 还在正常回话，而面板却照旧写「她已经哑了 1.9 小时」——
    # 只因为最近一条被踢记录在 16:24。这种「拿推测当结论」正是这个项目的老毛病。
    last_ok = None

    for raw in text.splitlines():
        # 自己先剥一遍 ANSI 颜色码：纯函数可能被直接喂原始日志
        # （_tail_text 也剥，但这里再来一次是防御性的 —— 少一层依赖少一个坑）。
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        ts = _line_time(line)

        # 登录成功：`[NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略`。
        # 用「已登录成功」而不是「登录成功」，是为了跟「快速登录错误 /
        # 自动快速登录失败」这类只含「登录」的行彻底分开。
        if "已登录成功" in line and ts is not None:
            if last_ok is None or ts > last_ok:
                last_ok = ts
            continue

        # 被踢下线：只统计，不当作 issue —— 它是起因，不是症状。
        if any(k in line for k in _KICK_KEYS):
            if ts is not None:
                kick_ts.append(ts)
                if last_kick is None or ts > last_kick:
                    last_kick = ts
                if ts >= kick_deadline:
                    kicks_24h += 1
            continue

        # ⚠️ 关键词里必须带上「验证码」：真实日志里带验证链接的那行长这样 ——
        #   [info] 需要验证码, proofWaterUrl:  https://ti.qq.com/.../sms-verify-login?...
        # 它**既没有 "Login" 也没有 "登录"**（URL 里那个 login 是小写、匹配不到），
        # 只按"登录"过滤就会把唯一能救命的链接漏掉。这是踩过的坑。
        if not any(k in line for k in ("Login", "登录", "验证码")):
            continue
        if ts is None or ts < deadline:
            continue

        for prio, (kind, keys) in enumerate(_LOGIN_EVENTS):
            if not any(k in line for k in keys):
                continue
            if kind == "risk":
                hit = re.search(r'"message":"([^"]+)"', line)
                detail = hit.group(1) if hit else "账号被 QQ 安全风控限制"
                code = re.search(r'"serverErrorCode":(\d+)', line)
                if code:
                    detail += "（错误码 %s）" % code.group(1)
            elif kind == "captcha":
                hit = re.search(r"proofWaterUrl:\s*(\S+)", line)
                if hit and ts >= captcha_ts:          # 留最新那条链接
                    captcha_url, captcha_ts = hit.group(1), ts
                detail = "密码登录被要求短信验证"
            elif kind == "quickfail":
                detail = "本地登录态已失效"
            else:
                detail = "二维码扫过了，但服务器没放行"
            if best is None or (prio, -ts) < (best[0], -best[1]):
                best = (prio, ts, kind, detail)

    stats = _kick_stats(kick_ts, now)
    return {
        "issue": (best[2], best[3], (now - best[1]) / 3600.0) if best else None,
        "captcha_url": captcha_url,
        "captcha_min": (now - captcha_ts) / 60.0 if captcha_ts else None,
        "last_kick_min": (now - last_kick) / 60.0 if last_kick else None,
        "last_ok_min": (now - last_ok) / 60.0 if last_ok else None,
        "kicks_24h": kicks_24h,
        "kick_gaps_h": stats["gaps_h"],
        "uptime_h": stats["uptime_h"],
        "gap_median_h": stats["median_h"],
        "gap_recent_h": stats["recent_h"],
        "kick_trend": stats["trend"],
    }


def _scan_login_log(max_age_hours: float = 6.0) -> dict:
    return _parse_login_log(_tail_text(LOG_DIR / "napcat_console.log"),
                            max_age_hours=max_age_hours)


def _fmt_age(minutes: float) -> str:
    """把「距今多少分钟」写成人话。"""
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return "%d 分钟前" % int(minutes)
    if minutes < 60 * 24:
        return "%.1f 小时前" % (minutes / 60.0)
    return "%.1f 天前" % (minutes / 1440.0)


def _qr_age() -> float | None:
    """NapCat 登录二维码距今多少秒；取不到返回 None。

    NapCat 在「未登录」状态下会把最新二维码写到这个固定路径，并约每 2 分钟
    刷新一次。于是「进程活着 + 端口没起 + 二维码是新的」= 它在等扫码，
    不是故障 —— 这种情况守护进程帮不上忙，只能人工扫一次。历史教训：
    这个状态以前只显示「可能登录中」，用户看到的是「机器人没反应」，
    完全查不出原因。
    """
    p = BASE_DIR / "napcat" / "napcat" / "cache" / "qrcode.png"
    try:
        return time.time() - p.stat().st_mtime
    except OSError:
        return None


def _webui_url() -> str:
    """NapCat 官方 WebUI 扫码页。

    页面里的二维码和 `扫码登录.bat` 弹的窗口一样是实时刷新的，
    所以这是「窗口看不清 / 屏幕太小」时最可靠的备用入口。
    token 带在 query 上，打开即登录，不用手输。
    """
    port, token = 6099, ""
    cfg = BASE_DIR / "napcat" / "napcat" / "config" / "webui.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        port = data.get("port", 6099)
        token = data.get("token", "") or ""
    except Exception:
        pass
    return "http://127.0.0.1:%d/webui%s" % (port, ("?token=" + token) if token else "")


# 面板诸状态 → 记号
_STATE_MARK = {"ok": "[OK]", "bad": "[!!]", "na": "[--]"}


def group_trigger_status() -> tuple[str, list[str]]:
    """群里 @ 她会不会回话 —— 返回 (状态, 要打印的行)。

    为什么要单独摆到面板上：`trigger.group_at` 一关，**群里 @ 她就不回话了**
    （只有正文里出现关键词才理）。这件事在外部表现上和"机器人挂了"一模一样，
    而且它还会连带把**群暖场**一起掐死（@ 不回 → 她很少在群里说话 →
    暖场要的 engaged 标记永远置不上）。2026-09-25 用户就为这个绕了两圈：
    先问"不会为群暖场"，再问"连群消息都不回了"，两次都是同一个开关。
    """
    t = cfg_section("trigger")
    kws = [str(k) for k in (t.get("group_keywords") or []) if str(k).strip()]
    kw_txt = "、".join(kws) if kws else "（没设关键词）"
    if t.get("group_at", True):
        return "ok", ["群里 @ 她会回话；另外正文提到 %s 也会" % kw_txt]
    lines = ["群里 @ 她**不会**回话（trigger.group_at=false）"]
    if kws:
        lines.append("只有正文里出现 %s 才会理 —— 光 @ 她是不理你的" % kw_txt)
    else:
        lines.append("而且一个关键词都没设 —— 等于她在群里完全不说话")
    lines.append("想恢复：设置页.bat 里把「群里要 @ 才理」打开（改完自动生效）")
    return "bad", lines


def quick_login_status() -> tuple[str, str]:
    """免扫码登录配没配 —— 返回 (状态, 说明文案)。

    状态：`ok` 配好了 / `bad` 没配或格式错 / `na` 读不到助手脚本。

    ⚠️ 判定**一律委托**给 `_setup_quick_login`（那个模块是 .env 的唯一真源）。
    绝对不要在这里自己解析一遍 .env —— 这个项目已经栽过一次「同一个判断写两处」：
    `tts_engine_name()` 曾自己判一遍引擎，加了本机克隆后面板照旧报旧结果，
    服务在跑、显示是错的。那种 bug 用户根本看不出来，只会以为白配置了。
    """
    try:
        import _setup_quick_login as _ql
        md5 = _ql.read_env().get(_ql.KEY_PASSWORD_MD5, "")
        valid = bool(md5) and bool(_ql.MD5_RE.match(md5))
    except Exception:
        return "na", "读不到配置助手（_setup_quick_login.py 缺失？）"
    if valid:
        return "ok", "已配置密码回退（票据失效也不会再要你扫码）"
    if md5:
        return "bad", "密码 MD5 格式不对，会被 NapCat 忽略 —— 重跑「设置免扫码登录.bat」"
    return "bad", ("未配置 —— 票据一失效就要你重新扫码，"
                   "跑「设置免扫码登录.bat」填一次密码即可")


def nudge_armed() -> tuple[int, int] | None:
    """暖场「武装」情况：返回 (她回过话的会话数, 总会话数)。读不到返回 None。

    为什么值得摆到面板上：**群暖场的入场券是「她在这个群里回过话」**
    （bot.py 里的 `engaged`，防止她去从没理过她的群里插嘴）。
    这个标记 2026-09-25 之前只在内存里，大脑一重启就清零 →
    群暖场静默失效、日志里一句错都不报，用户完全查不出原因。
    现在它落盘了（`run/nudge_state.json`），但仍然可能长期是 0（比如
    群里一直没人叫她、或者群里 @ 被关掉了），把数字亮出来就不用猜。
    """
    p = BASE_DIR / "run" / "nudge_state.json"
    try:
        sessions = json.loads(p.read_text(encoding="utf-8")).get("sessions") or {}
    except Exception:
        return None
    if not isinstance(sessions, dict):
        return None
    armed = sum(1 for st in sessions.values()
                if isinstance(st, dict) and st.get("engaged"))
    return armed, len(sessions)


def _webui_login_url() -> str:
    """NapCat WebUI 的 **QQ 登录页** —— 验证码流程必须在这一页里完成。

    依据（从 napcat.mjs + 前端 assets 里挖出来的）：
      - 前端路由是 `/web_login`，挂在 `/webui` 下 → 实际路径 `/webui/web_login`；
      - 它对应两个后端接口：
          `/QQLogin/PasswordLogin` → 返回 `{needCaptcha, proofWaterUrl}`
          `/QQLogin/CaptchaLogin`  → 收 `{uin, passwordMd5, ticket, randstr, sid}`
    也就是说：**光打开腾讯那个 proofWaterUrl，验证结果回不到 NapCat**；
    必须在登录页里走完，ticket 才会被交给 NapCat 完成登录。
    """
    base = _webui_url()                       # http://127.0.0.1:6099/webui?token=xxx
    head, sep, query = base.partition("?")
    return head.rstrip("/") + "/web_login" + (sep + query if sep else "")


def napcat_hook_mode() -> int | None:
    """读 NapCat 的 o3HookMode（防掉线开关）。读不到返回 None。

    ⚠️ 必须读**全局** `config/napcat.json`。napcat.mjs 里那行是关键：
        const s = aae(t.configPath);        // aae() 读 join(configPath, "napcat.json")
        await r.init(..., s.o3HookMode === 1);
    也就是说 hook 初始化只认全局那份，**不认**按账号的 `napcat_<uin>.json`
    （那份只在 WebUI 显示/保存时用）。改错文件 = 表面改了、实际没生效。
    本机两份都设成一样的，就是为了不踩这个。

    schema 默认值是 0；NapCat 安全文档建议「频繁掉线时把 o3Hook 设置为 0」。
    """
    cfg = BASE_DIR / "napcat" / "napcat" / "config" / "napcat.json"
    try:
        return int(json.loads(cfg.read_text(encoding="utf-8")).get("o3HookMode"))
    except Exception:
        return None


# NapCat 反检测的六个开关。顺序照抄 napcat.mjs 里 enableAllBypasses 的入参。
BYPASS_KEYS = ("hook", "window", "module", "process", "container", "js")


def _napcat_configs() -> list[tuple[str, dict | None]]:
    """NapCat config 目录下与 bypass 有关的几份配置（全局 + 按账号）。"""
    d = BASE_DIR / "napcat" / "napcat" / "config"
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("napcat*.json")):
        if p.name.startswith("napcat_protocol_"):
            continue                 # 那份只放 packetBackend，跟 bypass 无关
        try:
            out.append((p.name, json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            out.append((p.name, None))
    return out


def napcat_bypass_state() -> tuple[str, list[str]]:
    """读 NapCat 的反检测开关，返回 (状态, 被关掉的键名)。

    状态：
      "on"         至少一份配置写了 bypass，且六项全是 true
      "off"        有关掉的键（"off"/"partial" 合并 —— 对用户来说都是「漏了」）
      "absent"     所有配置里都没有 bypass 段（交给 native 默认，这里确认不了）
      "unreadable" 一个配置文件都读不到 / 全是坏 JSON

    为什么值得专门盯它（2026-09-26 实测踩到）：
    napcat.mjs 里这几行是 NapCat 的「我不是机器人」总闸 ——
        if (process.env.NAPCAT_DISABLE_BYPASS !== "1") {
            const W = s.bypass ?? {};
            i.nativeExports.enableAllBypasses?.(W);
        }
    配置里写 false，就是把六个反检测钩子一个个关掉；而 4.18.28 的 schema 默认
    全是 true（`hook: p.Boolean({ default: !0 })` …）。

    ⚠️ 关掉它**从任何地方都看不出来**：进程在、端口在、消息照回，
    只有「被踢间隔」在一路缩短（本机实测 6.5h → 6.2h → 1.8h → 1.2h → 0.5h）。
    所以状态页**每回都报**，不挂在「被踢 ≥3 次」的条件里 —— 免得下次又
    悄悄变回 false 而没人发现。
    """
    srcs = _napcat_configs()
    if not srcs:
        return ("unreadable", [])
    off: list[str] = []
    saw_on = False
    read_any = False
    for _name, data in srcs:
        if data is None:
            continue
        read_any = True
        byp = data.get("bypass")
        if not isinstance(byp, dict):
            continue
        for k in BYPASS_KEYS:
            if byp.get(k, True) is False:
                if k not in off:
                    off.append(k)
            else:
                saw_on = True
    if off:
        return ("off", off)
    if saw_on:
        return ("on", [])
    if read_any:
        return ("absent", [])
    return ("unreadable", [])


def cmd_status() -> int:
    head("小柚 · 运行状态")

    dpid = read_pid("daemon")
    if pid_alive(dpid):
        age = heartbeat_age()
        extra = ""
        if age is not None:
            extra = "  最近巡检 %d 秒前" % int(age)
            if age > 150:          # 稳态间隔 45s，超过 3 倍才算卡住
                extra += "（偏旧，可能卡住了）"
        print("  [OK]   守护进程   pid %d%s" % (dpid, extra))
    else:
        print("  [--]   守护进程   没在跑")

    if protocol_up():
        print("  [OK]   协议层     HTTP 3000 / WS 3001 都在监听")
    else:
        npid = napcat_pid()
        qr = _qr_age()
        diag = _scan_login_log()
        issue = diag["issue"]
        kind = issue[0] if issue else ""
        if kind == "risk":
            _, detail, age_h = issue
            print("  [!!]   协议层     登录被 QQ 安全风控拒绝 —— 扫码也登不上")
            print("                    服务器原话：%s" % detail)
            print("                    怎么解：手机上开最新版手机QQ，登录这个号，")
            print("                            按它弹出的提示做完安全验证，把账号恢复正常")
            print("                    注意：风控没解除前，扫码和快速登录都会被拒，先别扫了")
            if age_h >= 1:
                print("                    （这条失败记录是 %.1f 小时前的）" % age_h)
        elif kind == "captcha":
            _, detail, age_h = issue
            print("  [!!]   协议层     密码登录被 QQ 要求短信验证 —— 等你点一下")
            print("                    （密码回退是通的：日志里有「正在密码登录」，")
            print("                      是 QQ 要验证码才卡住的）")
            # ⚠️ 这一条极其反直觉，必须写出来：**配了密码回退之后，这个状态下
            # NapCat 不再生成二维码**（它停在「等你完成验证」，不再走二维码兜底）。
            # 不写的话用户会去双击「扫码登录.bat」，看到一张几十分钟前的旧图，
            # 怎么扫都没反应 —— 然后就以为是自己手机的问题，反复重启。
            # 实测依据：2026-09-25 20:47 起 70 分钟内 NapCat 一次都没写过二维码；
            # 而密码还没配的 17:18 那次是有的。
            qr_age = _qr_age()
            if qr_age is None or qr_age > 300:
                print("                    [!] 现在「扫码登录.bat」是没用的 —— 配了密码回退后，")
                print("                        NapCat 停在「等你完成验证」，不会再生成二维码。")
                if qr_age is not None:
                    print("                        （手上那张二维码是 %s 的旧图，扫了也不会有反应）"
                          % _fmt_age(qr_age / 60.0))
            print("                    ① 浏览器打开 NapCat 登录页，按提示完成短信验证：")
            print("                    %s" % _webui_login_url())
            print("                       （必须在这一页做 —— 验证结果要它交回 NapCat，")
            print("                        单独开腾讯那个链接，结果传不回来）")
            if diag["captcha_url"]:
                print("                       腾讯验证页（备用，可先看要你验证什么）：")
                print("                       %s" % diag["captcha_url"])
            print("                    [!] 动手之前**别再重启机器人** —— 每重启一次就换一个")
            print("                        新的验证会话，你手上的链接立刻作废，永远点不上。")
            print("                    ② 退路（收不到短信时）：双击「设置免扫码登录.bat」选 3")
            print("                        清空密码 → 再重启一次，二维码就回来了。")
            if age_h >= 1:
                print("                    （这条记录是 %.1f 小时前的）" % age_h)
        elif napcat_alive() and qr is not None and qr < 240:
            print("  [!!]   协议层     需要扫码登录 —— QQ 登录态过期了")
            if kind == "quickfail":
                print("                    提速：本地登录态已失效，所以免扫码的快速登录走不通，")
                print("                          这一步只能人工扫一次")
            if kind == "scanned":
                print("                    注意：上一张二维码扫过了但服务器没放行，可能又要扫")
            print("                    双击「扫码登录.bat」：弹出的二维码会自动刷新，扫到成功为止")
            print("                    备用入口（浏览器）：%s" % _webui_url())
        elif napcat_alive():
            print("  [..]   协议层     进程在（pid %d）但端口没起，可能正在登录" % npid)
            print("                    若超过 1 分钟没动静，双击「扫码登录.bat」")
        else:
            print("  [--]   协议层     没在跑")

        # 「为什么老是要我重新登录」的真正答案在这里。
        # 被踢下线才是起因：被踢 → NapCat 重启 Worker → 登录票据全作废 → 才要重登。
        # ⚠️ 这里只能用 ASCII 的 [!] 当警示记号，**不能用 ⚠ 这个符号**：
        # 本函数最后会被 _setup_console() 把 stdout 设成 gbk+errors=replace，
        # 而 ⚠ 不在 GBK 字符集里 → 屏幕上静默变成「?」，既不报错也没人看得出来。
        # 被踢多久了 —— **不限 1 小时**。2026-09-26 实测：07:15 被踢、11:39 才有人扫码，
        # 中间 4 个半小时她是哑的而没人知道。超过一小时就不报了，等于把最需要看见的
        # 那段时间藏起来（原来就是 `km < 60` 才报）。
        km = diag["last_kick_min"]
        if km is not None:
            # 被踢之后又登上去过？那就别再说「她已经哑了这么久」。
            # 见 _parse_login_log 里 last_ok 的注释：2026-09-26 面板就因为漏了这一步
            # 而把「17:41 还在回话」的账号说成「已经哑了 1.9 小时」。
            okm = diag["last_ok_min"]
            back = okm is not None and okm < km
            print("                    [!] 账号 %s被腾讯踢下线（日志原话：「登录已失效」）%s"
                  % (_fmt_age(km), "，之后已经重新登录过" if back else ""))
            if km >= 60 and not back:
                print("                        也就是说她已经哑了这么久 —— 需要人工扫码或验证一次。")
        # 稳定性趋势：用户问的其实是「以后还会不会掉」。
        # 单看「最近 24h 被踢 N 次」答不了这个问题 —— 那是个瞬时值。
        # 所以这里给三样东西：当前存活多久、历史上多久踢一次、最近比历史好还是坏。
        _kp = diag.get("kick_gaps_h") or []
        _up = diag.get("uptime_h")
        _med = diag.get("gap_median_h")
        _rec = diag.get("gap_recent_h")
        _tr = diag.get("kick_trend")
        if _up is not None or len(_kp) >= 2:
            print()
            print("  ── 稳定性趋势 " + "─" * 40)
            if _up is not None:
                print("     当前存活   %.1f 小时（距最近一次被踢）" % _up)
            if len(_kp) >= 2 and _med is not None:
                _sorted = sorted(_kp)
                print("     历史间隔   中位 %.1f h ｜ 最短 %.1f h ｜ 最长 %.1f h ｜ 样本 %d 段"
                      % (_med, _sorted[0], _sorted[-1], len(_kp)))
            if _tr == "improving":
                print("     趋势       好转 —— 最近 3 次的间隔（中位 %.1f h）明显长于历史" % _rec)
            elif _tr == "worsening":
                print("     趋势       恶化 —— 最近 3 次的间隔（中位 %.1f h）在缩短，要当心" % _rec)
            elif _tr == "flat":
                print("     趋势       持平 —— 最近 3 次（中位 %.1f h）与历史差不多" % _rec)
            else:
                print("     趋势       样本太少（需 ≥4 段间隔才判趋势），先攒着")
            # ⚠️ 不给「预计还能撑 N 小时」这种外推。理由写在这里，免得以后有人手痒加：
            # 本机只有 10 段被踢样本，且分布明显重尾（中位 2.97h vs 均值 6.13h）。
            # 对这么小的重尾样本做生存分析，任何模型都会被单个离群值带跑 ——
            # 我自己试过泊松和指数混合两套，都给出过「活过 24h 概率 <5%」这种
            # 与实测（已连续存活 18.9h）自相矛盾的结论。宁可说"样本不够"。
            print("     说明       样本量太小，不做「还能撑多久」的预测 —— 那会变成算命。")
            print("                看趋势有没有好转，比看某个数字靠谱。")
        if diag["kicks_24h"] >= 3:
            print("                    [!] 最近 24 小时被踢了 %d 次。" % diag["kicks_24h"])
            print("                      先说清一件事：手机QQ 和协议端**不会**冲突 ——")
            print("                      腾讯官方支持「一个手机 + 一个电脑」同时在线。")
            print("                      会互踢的是同一账号的**第二个电脑端**：官方PC QQ、")
            print("                      另一个协议端、或另一台电脑。先确认没有这第二个。")
            print("                      如果确实没有：多半是 NapCat 已知的风控掉线，")
            print("                      症状跟这个一模一样（几小时一次、无征兆、有时连通知都没有）。")
            print("                      参考资料：NapCatQQ 仓库 issue #1728 / #1796。")
            _bks, _bko = napcat_bypass_state()
            if _bks == "off":
                print("                      并且下面「反检测」那行显示伪装被关了 %d/6 ——"
                      % len(_bko))
                print("                      这是掉线变勤的头号嫌疑，先把它修了。")
            _hook = napcat_hook_mode()
            if _hook == 0:
                print("                      防掉线开关 o3HookMode 已经是 0（已应用，观察几天看是否改善）。")
            elif _hook is None:
                print("                      读不到 config/napcat.json，没法确认 o3HookMode。")
            else:
                print("                      可试的开关：把 config/napcat.json 里的 o3HookMode 改成 0")
                print("                      （现在是 %d；schema 默认就是 0，NapCat 安全文档" % _hook)
                print("                       也建议频繁掉线时设 0）。注意要改**全局**那份，")
                print("                       不是按账号的 napcat_<QQ号>.json。")

    # 反检测伪装：NapCat 的六个 bypass 开关。
    # 这一条**不挂在「被踢 ≥3 次」的条件里**，每回都报 —— 它坏了外观上毫无变化，
    # 只有掉线会变频繁，正是最需要主动盯的那种「静默失效」。
    _bst, _boff = napcat_bypass_state()
    if _bst == "on":
        print("  [OK]   反检测     6/6 伪装已开（hook/window/module/process/container/js）")
    elif _bst == "off":
        print("  [!!]   反检测     伪装被关了：%s" % "、".join(_boff))
        print("                    这 6 个钩子是 NapCat 用来藏「我不是机器人」的，")
        print("                    官方默认全开。被关掉进程和端口都正常、消息也照回，")
        print("                    唯一症状就是**掉线越来越勤** —— 多半是有谁保存过一份旧配置。")
        print("                    修：把 %s 里 bypass 的这六项改回 true，再重启协议层"
              % "、".join(n for n, _ in _napcat_configs()))
    elif _bst == "absent":
        print("  [..]   反检测     配置里没有 bypass 段 —— 交给 NapCat 内置默认，这里确认不了")

    # 稳定性一行式摘要 —— **无条件报**，和「反检测」同理：
    # 用户最常问的就是「还会不会掉线」，而答案是个趋势、不是瞬时值。
    # 详细统计（最短/最长/样本段数）留在协议层异常分支里；这里只给最好读的三个数。
    _sdiag = _scan_login_log()
    _sup, _smed, _str = (_sdiag.get("uptime_h"), _sdiag.get("gap_median_h"),
                         _sdiag.get("kick_trend"))
    _sgaps = _sdiag.get("kick_gaps_h") or []
    if _sup is None and not _sgaps:
        print("  [--]   稳定性     还没有被踢记录，算不出趋势")
    else:
        _mark = {"improving": "[OK]", "flat": "[OK]",
                 "worsening": "[!!]"}.get(_str, "[..]")
        _txt = {"improving": "好转", "flat": "持平",
                "worsening": "恶化", }.get(_str, "样本不足（需 ≥4 段）")
        _seg = "存活 %.1fh" % _sup if _sup is not None else "存活未知"
        if _smed is not None:
            _seg += " ｜ 历史中位 %.1fh ｜ %d 段样本" % (_smed, len(_sgaps))
        print("  %s   稳定性     %s ｜ 被踢趋势 %s" % (_mark, _seg, _txt))

    if llm_provider() == "ollama":
        if ollama_up():
            print("  [OK]   推理后端   Ollama 11434 已就绪")
        else:
            print("  [!!]   推理后端   Ollama 没在跑 —— 她会收得到消息但回不了话")
            print("                    用「重启机器人.bat」会把它一起拉起来")
    else:
        ok, detail = cloud_up()
        if ok:
            print("  [OK]   推理后端   云端 %s（%s）" % (llm_model(), detail))
        else:
            print("  [!!]   推理后端   云端 %s 连不上：%s" % (llm_model(), detail))
            print("                    本地 Ollama 与本次运行无关，不用管它")

    bpid = read_pid("bot")
    if pid_alive(bpid):
        print("  [OK]   猫娘大脑   pid %d" % bpid)
    else:
        print("  [--]   猫娘大脑   没在跑")

    _gt_state, _gt_lines = group_trigger_status()
    print("  %s   群触发     %s" % (_STATE_MARK[_gt_state], _gt_lines[0]))
    for _l in _gt_lines[1:]:
        print("                    %s" % _l)

    tcfg = tools_cfg()
    if tcfg.get("enabled", True):
        prov = str(tcfg.get("search_provider") or "bing").lower()
        prov_name = {"bocha": "博查AI（需 Key）", "bing": "必应 + 360（免 Key）"}.get(prov, prov)
        print("  [OK]   联网能力   开 —— 搜资料 / 读网页 / 查天气 / 看时间 / 查热梗 / 记事情")
        print("                    搜索通道：%s" % prov_name)
    else:
        print("  [--]   联网能力   关（config.json 里 tools.enabled 改成 true 就能开）")

    vcfg = cfg_section("voice")
    if vcfg.get("enabled", True):
        can_listen, can_speak = voice_state()
        marks = []
        marks.append("听语音 " + ("就绪" if can_listen else "缺模型"))
        marks.append("发语音 " + (tts_engine_name() if can_speak else "缺语音包"))
        flag = "OK" if (can_listen and can_speak) else ("--" if not (can_listen or can_speak) else "OK")
        print("  [%s]   语音能力   %s" % (flag, " | ".join(marks)))
        if vcfg.get("group_enabled"):
            print("                    群里也会发语音（group_enabled=true）")
        else:
            print("                    只在私聊发语音（默认，群里安静）")
    else:
        print("  [--]   语音能力   关（config.json 里 voice.enabled 改成 true 就能开）")

    icfg = cfg_section("vision")
    if icfg.get("enabled", True):
        print("  [OK]   看图       开 —— 图片走 %s" % (icfg.get("model") or "deepseek-chat"))
    else:
        print("  [--]   看图       关（config.json 里 vision.enabled 改成 true 就能开）")

    pcfg = cfg_section("proactive")
    if pcfg.get("enabled", True):
        owner = str(pcfg.get("owner_qq") or "").strip()
        city = str(pcfg.get("city") or "").strip()
        print("  [OK]   主动早安   每天 %s → %s" % (
            pcfg.get("greeting_time") or "08:30", owner or "（未填，自动从记忆推断）"))
        if not city:
            print("                    [!] 没填 proactive.city —— 她拿不到天气，"
                  "会说时间代替（填上城市才能报天气）")
    else:
        print("  [--]   主动早安   关（config.json 里 proactive.enabled 改成 true 就能开）")

    ncfg = (pcfg.get("nudge") or {}) if isinstance(pcfg, dict) else {}
    if pcfg.get("enabled", True) and ncfg.get("enabled", True):
        qh = ncfg.get("quiet_hours") or []
        line = "静默 %s 分钟起" % ncfg.get("silence_min", 45)
        if ncfg.get("group_enabled", True):
            line += "；群里 %s 分钟起、每天 %s 次" % (
                ncfg.get("group_silence_min", 60), ncfg.get("group_max_per_day", 1))
        else:
            line += "；群里不主动"
        print("  [OK]   冷场暖场   %s" % line)
        print("                    私聊每天最多 %s 次，%s 之间不开口" % (
            ncfg.get("private_max_per_day", 3),
            "-".join(str(x) for x in qh) if qh else "无免打扰时段"))
        armed = nudge_armed()
        if armed is None:
            print("                    （还没有热身记录 —— 首次运行会是这样）")
        else:
            a, total = armed
            if total == 0:
                # 一个会话都还没记到 = 她重启之后还没收到过任何消息。
                # 这时候说"一个都没武装"会吓人，其实只是还没开始。
                print("                    还没记录到任何会话（等第一句话进来）")
            else:
                print("                    已武装 %d/%d 个会话（回过话的才会暖）" % (a, total))
                if a == 0:
                    print("                    [!] 一个都没武装 —— 群暖场要等她**在这个群回过话**")
                    print("                        才会开闸。群里 @ 要是被关了（trigger.group_at），")
                    print("                        她很少吭声，群暖场就会一直不动。")
    else:
        print("  [--]   冷场暖场   关（config.json 里 proactive.nudge.enabled 改成 true 就能开）")

    # 自愈：这两项由守护进程执行（它每轮重读 config.json，改完不用重启它）
    wcfg = cfg_section("watch")
    hcfg = cfg_section("health")
    if wcfg.get("enabled", True) or hcfg.get("enabled", True):
        bits = []
        if wcfg.get("enabled", True):
            bits.append("改完自动重启")
        if hcfg.get("enabled", True):
            bits.append("卡死自动重启（心跳 %s 秒不更新即判僵死）"
                        % hcfg.get("hb_stale_sec", 90))
        print("  [OK]   自动重启   " + " | ".join(bits))
    else:
        print("  [--]   自动重启   关"
              "（config.json 里 watch.enabled / health.enabled 改成 true 就能开）")

    # 梗库自动更新：由**大脑**的 corpus 线程执行（读 config.json，改完要重启大脑）
    ccfg = cfg_section("corpus")
    if ccfg.get("enabled", True):
        try:
            days = max(1, min(90, int(str(ccfg.get("refresh_days", 7)).strip())))
        except (TypeError, ValueError):
            days = 7
        # 「上次更新是什么时候」从 run/corpus_state.json 读 —— 只报读得懂的情形，
        # 读不出来就说"还没跑过"，绝不编一个时间出来。
        try:
            st = json.loads((BASE_DIR / "run" / "corpus_state.json")
                            .read_text(encoding="utf-8"))
            last = float(st.get("last_ok") or 0)
        except Exception:
            last = 0.0
        if last > 0:
            age = max(0.0, time.time() - last)
            when = ("%.0f 分钟前" % (age / 60)) if age < 3600 else ("%.1f 天前" % (age / 86400))
        else:
            when = "还没跑过（她启动后会自己抓第一轮）"
        print("  [OK]   梗库更新   每 %s 天自动抓新梗 · 上次 %s" % (days, when))
    else:
        print("  [--]   梗库更新   关（config.json 里 corpus.enabled 改成 true 就能开）")

    acct = BASE_DIR / "napcat_account.txt"
    if acct.exists():
        print("  [OK]   登录账号   %s（已记录）" % acct.read_text().strip())
    else:
        print("  [--]   登录账号   未记录（首次启动要扫码）")

    # 免扫码的判定**只有一处真源** —— `_setup_quick_login.read_env()`。
    # 别再在这里自己解析一遍 .env：这项目已经栽过一次「同一个判断写两处」
    # （见 MEMORY.md：service_ctl.tts_engine_name 曾自己判一遍引擎，
    #  加了本机克隆后面板照旧报旧结果）。判错了用户会以为白配置了。
    _ql_state, _ql_text = quick_login_status()
    print("  %s   免扫码     %s" % (_STATE_MARK[_ql_state], _ql_text))

    if (STARTUP_DIR / AUTOSTART_NAME).exists():
        print("  [OK]   开机自启   已安装（静默，无窗口）")
    elif (STARTUP_DIR / AUTOSTART_LEGACY).exists():
        print("  [!!]   开机自启   装的是旧版 bat（会闪一下），跑「开机自启-安装.bat」升级")
    else:
        print("  [--]   开机自启   未安装（跑「开机自启-安装.bat」）")

    print()
    print("  全部就绪 = 开机后什么都不用管，@它就能回。")
    return 0


def _autostart_content() -> bytes:
    """开机自启脚本。

    用 VBS 而不是 bat：wscript.exe 属于 GUI 子系统，执行时既不弹控制台、
    也不会闪一下黑框；bat 则必然要起一个 cmd 窗口（哪怕只有几十毫秒）。
    Run 的第二个参数 0 = 隐藏窗口，第三个 False = 不等待。
    """
    q = '"'
    run_line = ("CreateObject(" + q + "WScript.Shell" + q + ").Run "
                + q * 3 + str(pythonw()) + q * 2 + " "
                + q * 2 + str(DAEMON) + q * 3 + ", 0, False")
    text = (
        "' 小柚 QQ 猫娘机器人 —— 登录后静默拉起守护进程（无窗口，桌面零痕迹）\r\n"
        + run_line + "\r\n"
    )
    return text.encode("gbk")


def cmd_install() -> int:
    head("小柚 · 安装开机自启")
    if not STARTUP_DIR.exists():
        print("  [错误] 找不到启动文件夹：%s" % STARTUP_DIR)
        return 1

    target = STARTUP_DIR / AUTOSTART_NAME
    try:
        target.write_bytes(_autostart_content())
    except Exception as e:
        print("  [错误] 写入失败：%s" % e)
        return 1

    # 顺手清掉旧版 bat，避免开机时两个入口同时拉起
    old = STARTUP_DIR / AUTOSTART_LEGACY
    if old.exists():
        try:
            old.unlink()
            print("  已移除旧版入口：%s" % AUTOSTART_LEGACY)
        except Exception as e:
            print("  [警告] 旧版 %s 删不掉（%s），建议手动删除" % (AUTOSTART_LEGACY, e))

    print("  已写入：%s" % target)
    print()
    print("  以后每次登录 Windows，机器人会在后台自己起来 —— 全程无窗口。")
    print("  想取消就跑「开机自启-卸载.bat」。")
    return 0


def cmd_uninstall() -> int:
    head("小柚 · 卸载开机自启")
    removed = []
    for name in (AUTOSTART_NAME, AUTOSTART_LEGACY):
        target = STARTUP_DIR / name
        if target.exists():
            try:
                target.unlink()
                removed.append(name)
            except Exception as e:
                print("  [错误] 删除 %s 失败：%s" % (name, e))
                return 1
    if removed:
        print("  已删除：" + "、".join(removed))
        print("  下次开机不会再自动启动（当前进程不受影响）。")
    else:
        print("  本来就没装。")
    return 0


def cmd_logs() -> int:
    if LOG_DIR.exists():
        try:
            os.startfile(str(LOG_DIR))
        except Exception:
            print(str(LOG_DIR))
    else:
        print("还没有日志目录。")
    return 0


COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "stopall": cmd_stopall,
    "status": cmd_status,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "logs": cmd_logs,
}


def main() -> int:
    _setup_console()
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    fn = COMMANDS.get(action)
    if fn is None:
        print("用法: python service_ctl.py "
              "[start|stop|stopall|restart|status|install|uninstall|logs]")
        return 2
    try:
        return fn()
    except Exception:
        import traceback
        print()
        print(BAR)
        print("  出错了：")
        print(BAR)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
