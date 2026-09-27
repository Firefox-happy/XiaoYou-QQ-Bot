# -*- coding: utf-8 -*-
"""
小柚 · 前台调试启动（两个窗口，日志实时可见）
============================================

⚠️ 日常不需要用它。平时的入口是顶层的「一键启动.bat」——那是**无窗口常驻**，
   关掉窗口也不影响机器人。

这个脚本是给"想盯着日志排查问题"时用的：开两个窗口，滚动日志看得见。

注意：走的是 **NapCat 独立模式**，不需要 QQ 客户端、也不会碰你的 QQ。
bat 只做三件事：切代码页、调本脚本、pause 兜底。
所有真正的逻辑在这里 —— 想改启动行为，改这个文件，别改 bat。
"""
import os
import sys
import time
import subprocess

try:
    sys.stdout.reconfigure(encoding="gbk", errors="replace", line_buffering=True)
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


def _pick_python() -> str:
    """挑一个能跑 bot.py 的解释器：项目 venv 优先，否则用自己这个。"""
    for root in (HERE, os.path.dirname(HERE)):
        for name in (".venv", "venv", "env"):
            for rel in ("Scripts\\python.exe", "bin/python"):
                cand = os.path.join(root, name, rel)
                if os.path.exists(cand):
                    return cand
    return sys.executable


PY = _pick_python()
DRY = "--dry" in sys.argv

BAR = "=" * 58


def head(text):
    print(BAR)
    print("  " + text)
    print(BAR)


def sub(text):
    print("       " + text)


def run_script(name, *args):
    """跑同目录下的 Python 脚本，透传输出，返回退出码。"""
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        print("       [错误] 缺少脚本 " + name)
        return 127
    try:
        return subprocess.run([PY, path] + list(args)).returncode
    except Exception as e:
        print("       [错误] 无法运行 %s：%s" % (name, e))
        return 126


def open_window(title, bat_name):
    bat = os.path.join(HERE, bat_name)
    cmd = 'start "%s" "%s"' % (title, bat)
    if DRY:
        print("       [演练] 将执行: " + cmd)
        return
    subprocess.Popen(cmd, shell=True, cwd=HERE)


def main():
    head("小柚 - QQ 猫娘机器人   前台调试启动")
    print()
    print("  提示：如果你只是想让她跑起来，关掉本窗口，")
    print("        改用顶层的「一键启动.bat」—— 那是无窗口常驻的。")
    print()

    # ---------- 1/3 自检 ----------
    print("[1/3] 运行环境自检")
    print()
    rc = run_script("preflight.py")
    print()
    if rc != 0:
        head("  自检没通过，先处理上面的问题再启动")
        return 2

    # ---------- 2/3 网络配置 ----------
    print("[2/3] 写入 NapCat 网络配置")
    rc = run_script("setup_napcat.py", "--auto")
    if rc == 3:
        print()
        sub("第一次启动：NapCat 还没登录过，暂时写不了配置。")
        sub("稍后弹出的二维码用【机器人小号】扫一下；")
        sub("登录成功后关掉窗口，再双击一次本脚本即可。")
    elif rc == 0:
        sub("配置已就绪")
    else:
        sub("[警告] 配置写入失败（退出码 %d），不影响首次登录，先继续" % rc)
    print()

    # ---------- 3/3 拉起 ----------
    print("[3/3] 启动组件（独立模式，不碰 QQ 客户端）")
    print()
    open_window("小柚 NapCat QQ协议层", "_start_napcat.bat")
    time.sleep(5)
    open_window("小柚 猫娘大脑", "_start_bot.bat")
    print()
    print(BAR)
    print("  已打开两个新窗口：")
    print("    [小柚 NapCat QQ协议层] - 协议层，自动免扫码登录")
    print("    [小柚 猫娘大脑]        - 看到 \"已连上 NapCat\" 就成功了")
    print()
    print("  这两个窗口一关，机器人就下线了 —— 所以只用于调试。")
    print("  想常驻请用顶层「一键启动.bat」。")
    print()
    print("  回到本窗口按回车键即可关闭。")
    print(BAR)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        print()
        print(BAR)
        print("  启动器遇到意外错误：")
        print(BAR)
        traceback.print_exc()
        print()
        print("  把上面这段报错发我。")
        sys.exit(1)
