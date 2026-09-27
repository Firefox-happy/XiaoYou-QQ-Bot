# -*- coding: utf-8 -*-
"""
小柚 · 启动前自检
只依赖标准库。退出码：0 = 通过；2 = 发现需要处理的问题。
"""
import os
import sys
import glob
import socket
import ctypes
import ctypes.wintypes as wt

try:
    sys.stdout.reconfigure(encoding="gbk", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# NapCat v4.18.28 硬性要求的最低 QQ build
REQUIRED_BUILD = 40768
# NapCat 官方验证过的 QQ 版本
TESTED_QQ = "9.9.31"

OK = "  [OK]  "
WARN = "  [--]  "
BAD = "  [!!]  "

problems = []


def ok(msg):
    print(OK + msg)


def warn(msg):
    print(WARN + msg)


def bad(msg):
    print(BAD + msg)
    problems.append(msg)


def hr():
    print()


# ---------- 1. Python ----------
def check_python():
    print("[1] Python 运行环境")
    ok("Python " + sys.version.split()[0])
    hr()


# ---------- 2. Ollama ----------
def port_alive(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def check_ollama():
    print("[2] 猫娘大脑（本地模型）")
    if not port_alive("127.0.0.1", 11434):
        warn("Ollama 服务未运行（启动器会自动拉起它）")
        hr()
        return
    ok("Ollama 服务在运行")
    try:
        import json as _json
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as r:
            data = _json.load(r)
        names = [m.get("name", "") for m in data.get("models", [])]
        hit = [n for n in names if "qwen" in n.lower()]
        if hit:
            ok("模型已就绪：" + ", ".join(hit[:3]))
        else:
            bad("没找到 qwen 模型，请先运行：ollama pull qwen2.5:7b-instruct")
    except Exception as e:
        warn("无法读取模型列表（%s）" % e)
    hr()


# ---------- 3. NapCat 文件 ----------
def check_napcat_files():
    print("[3] NapCat 协议层文件")
    inner = os.path.join(HERE, "napcat", "napcat")
    need = [
        "launcher-user.bat",
        "NapCatWinBootMain.exe",
        "NapCatWinBootHook.dll",
        "napcat.mjs",
        "loadNapCat.js",
        "qqnt.json",
    ]
    missing = [n for n in need if not os.path.exists(os.path.join(inner, n))]
    if missing:
        for n in missing:
            bad("缺少文件 " + os.path.relpath(os.path.join(inner, n), HERE))
    else:
        ok("核心文件齐全（%d 项）" % len(need))
    hr()


# ---------- 4. QQ ----------
def read_version(path):
    if not os.path.exists(path):
        return None
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return None
        r = ctypes.c_void_p()
        ln = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(r), ctypes.byref(ln)):
            return None

        class FI(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wt.DWORD), ("dwStrucVersion", wt.DWORD),
                ("dwFileVersionMS", wt.DWORD), ("dwFileVersionLS", wt.DWORD),
                ("dwProductVersionMS", wt.DWORD), ("dwProductVersionLS", wt.DWORD),
                ("dwFileFlagsMask", wt.DWORD), ("dwFileFlags", wt.DWORD),
                ("dwFileOS", wt.DWORD), ("dwFileType", wt.DWORD),
                ("dwFileSubtype", wt.DWORD), ("dwFileDateMS", wt.DWORD),
                ("dwFileDateLS", wt.DWORD),
            ]

        fi = ctypes.cast(r, ctypes.POINTER(FI)).contents
        a = fi.dwFileVersionMS >> 16
        b = fi.dwFileVersionMS & 0xFFFF
        c = fi.dwFileVersionLS >> 16
        d = fi.dwFileVersionLS & 0xFFFF
        return "%d.%d.%d.%d" % (a, b, c, d)
    except Exception:
        return None


def find_qq():
    try:
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        ]
        for root, key in keys:
            try:
                with winreg.OpenKey(root, key) as k:
                    us, _ = winreg.QueryValueEx(k, "UninstallString")
                    p = os.path.join(os.path.dirname(us.strip().strip('"')), "QQ.exe")
                    if os.path.exists(p):
                        return p
            except OSError:
                continue
    except Exception:
        pass
    for cand in [
        r"C:\Program Files\Tencent\QQNT\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
    ]:
        if os.path.exists(cand):
            return cand
    return None


def check_qq():
    print("[4] NapCat 独立模式依赖")
    # 独立模式不需要 QQ 客户端在运行，但 wrapper.node 依赖 BoringSSL 两个 dll
    nap = os.path.join(HERE, "napcat")
    missing = [n for n in ("ssl.dll", "crypto.dll")
               if not os.path.exists(os.path.join(nap, n))]
    if missing:
        bad("缺少 " + "、".join(missing))
        print("        它们是 wrapper.node 的依赖，QQ 安装目录里有现成的：")
        print("        <QQNT>\\versions\\<版本>\\resources\\app\\")
        print("        启动时守护进程会自动尝试帮你补齐")
    else:
        ok("BoringSSL 就绪（ssl.dll / crypto.dll）")

    qq = find_qq()
    if qq:
        ok("本机 QQ：" + qq)
        print("        （独立模式不需要它运行，你自己的 QQ 照常用，互不干扰）")
    else:
        ok("本机没装 QQ —— 没关系，独立模式本来就不需要")
    hr()


# ---------- 5. 配置 ----------
def check_config():
    print("[5] NapCat 网络配置")
    import json

    found = glob.glob(os.path.join(HERE, "napcat", "napcat", "config", "onebot11*.json"))
    if not found:
        warn("尚未生成（首次登录 NapCat 后自动写入，属正常）")
        hr()
        return

    # 记录下来的机器人账号
    acc_file = os.path.join(HERE, "napcat_account.txt")
    if os.path.exists(acc_file):
        try:
            uin = open(acc_file, "r", encoding="ascii", errors="ignore").read().strip()
        except OSError:
            uin = ""
        if uin.isdigit():
            ok("机器人账号 %s（启动时自动免扫码登录）" % uin)
        else:
            warn("napcat_account.txt 内容不是纯数字，删掉它会在下次启动时重新记录")
    else:
        warn("还没有记录机器人账号，首次启动会要求扫一次码")

    for path in sorted(found):
        name = os.path.basename(path)
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            bad("%s 解析失败：%s" % (name, e))
            continue

        net = data.get("network") if isinstance(data.get("network"), dict) else {}
        top_ws = data.get("websocketServers") or []
        net_ws = net.get("websocketServers") or []
        net_http = net.get("httpServers") or []

        def enabled_port(items, port):
            for it in items:
                if isinstance(it, dict) and it.get("enable") and it.get("port") == port:
                    return True
            return False

        has_http = enabled_port(net_http, 3000)
        has_ws = enabled_port(net_ws, 3001)

        if has_http and has_ws:
            ok("%s 配置正确（HTTP 3000 / WS 3001 均启用）" % name)
        elif not net_ws and top_ws:
            bad("%s 把服务写在了顶层，但 NapCat 读的是 network 层 —— 端口不会监听" % name)
            print("        修复：在本目录执行  python setup_napcat.py")
        else:
            bad("%s 缺少启用的服务（HTTP 3000 / WS 3001）" % name)
    hr()


# ---------- 6. 运行状态 ----------
def check_running():
    print("[6] 运行状态（启动完成后看这里）")
    http_up = port_alive("127.0.0.1", 3000)
    ws_up = port_alive("127.0.0.1", 3001)
    if http_up and ws_up:
        ok("协议层端口已监听：3000 / 3001")
    else:
        warn("端口未监听：3000 %s / 3001 %s"
             % ("开" if http_up else "关", "开" if ws_up else "关"))
        print("        如果这是启动前跑的自检，属正常；")
        print("        如果启动后仍是关的，说明协议层没登录成功或配置没生效。")
    hr()


def main():
    print("=" * 60)
    print("   小柚 · 启动前自检")
    print("=" * 60)
    hr()
    check_python()
    check_ollama()
    check_napcat_files()
    check_qq()
    check_config()
    check_running()
    print("=" * 60)
    if problems:
        print("   发现 %d 个需要处理的问题：" % len(problems))
        for i, p in enumerate(problems, 1):
            print("     %d. %s" % (i, p))
        print("=" * 60)
        return 2
    print("   自检通过，可以启动。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
