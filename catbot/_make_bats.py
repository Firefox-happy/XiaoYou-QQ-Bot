# -*- coding: utf-8 -*-
"""
把 .bat 启动脚本重写为 Windows cmd 原生格式：
  - 编码 GBK (cp936) 无 BOM —— cmd 默认代码页，中文不乱码
  - 换行 CRLF
  - 逻辑极简：bat 只当壳，真正的逻辑在 launch.py
  - 每条退出路径都有 pause，杜绝"窗口一闪就没"
运行一次即可。要改启动行为，改 launch.py，不用动这里。
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="gbk", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


def _pick_python(exe="python.exe"):
    """找一个能跑本项目的解释器，写进生成的 bat 里。

    不硬编码绝对路径 —— 那样别人的机器上必然跑不起来。顺序：
      ① 项目自带 venv（.venv / venv / env，见 README「安装」）
      ② 环境变量 XIAOYOU_PYTHON
      ③ 当前解释器旁边的同名程序（生成 bat 时用的那个）
    实在找不到就退回裸命令名，让 cmd 去 PATH 里找。
    """
    for root in (HERE, os.path.dirname(HERE)):
        for name in (".venv", "venv", "env"):
            for rel in (os.path.join("Scripts", exe), os.path.join("bin", exe)):
                cand = os.path.join(root, name, rel)
                if os.path.exists(cand):
                    return cand
    env = os.environ.get("XIAOYOU_PYTHON")
    if env and os.path.exists(env):
        return env
    side = os.path.join(os.path.dirname(sys.executable), exe)
    if os.path.exists(side):
        return side
    return exe


PY = _pick_python("python.exe")
PYW_FALLBACK = _pick_python("pythonw.exe")

LAUNCHER = r"""@echo off
chcp 936 >nul
title 小柚 启动器
cd /d "%~dp0"

echo.
"__PY__" "%~dp0launch.py"
echo.
echo 按任意键关闭本窗口。
pause >nul
"""

NAPCAT = r"""@echo off
chcp 936 >nul
title 小柚 NapCat QQ协议层
cd /d "%~dp0napcat\napcat"

echo ============================================================
echo   小柚 - NapCat（QQ 协议层）
echo ============================================================
echo.

REM 上次登录成功后会记下账号，用它做免扫码快速登录
set "NAPCAT_QUICK_ACCOUNT="
if exist "%~dp0napcat_account.txt" set /p NAPCAT_QUICK_ACCOUNT=<"%~dp0napcat_account.txt"

if not exist "launcher-user.bat" goto :no_napcat

if defined NAPCAT_QUICK_ACCOUNT (
    echo  正在用账号 %NAPCAT_QUICK_ACCOUNT% 尝试免扫码登录...
) else (
    echo  还没记录过账号 —— 请用【机器人号】扫描下方二维码登录。
    echo  登录成功后，下次启动就会自动免扫码。
)
echo.
echo  * 如果每次都还要你扫码：双击根目录的「设置免扫码登录.bat」，
echo    填一次 QQ 密码（只存 MD5），之后就再也不用扫了。
echo.
echo  * 保持本窗口开着；关掉它，机器人就下线了。
echo  * 如果 QQ 窗口起来了、但本窗口迟迟不滚出 NapCat 日志，
echo    说明注入没成功，请把本窗口的内容截图发我。
echo.
echo ------------------------------------------------------------
echo.

call launcher-user.bat

echo.
echo ------------------------------------------------------------
echo  NapCat 已退出。
pause
exit /b 0

:no_napcat
echo   [错误] 找不到 launcher-user.bat
echo   当前目录: %CD%
echo.
echo   请确认 napcat 目录已完整解压到 catbot 下。
pause
exit /b 1
"""

BOT = r"""@echo off
chcp 936 >nul
title 小柚 猫娘大脑
cd /d "%~dp0"

echo ============================================================
echo   小柚 - 猫娘大脑
echo ============================================================
echo.
echo  正在连接 NapCat ...
echo.

"__PY__" "%~dp0bot.py"

echo.
echo ------------------------------------------------------------
echo  机器人已退出。
pause
"""

SCANLOGIN = r"""@echo off
chcp 936 >nul
title 小柚 - 扫码登录
cd /d "%~dp0"

REM 弹二维码窗口需要带 tkinter 的解释器；有些精简 venv 不带 tkinter，
REM 所以先试本机的 pythonw，找不到就退回项目解释器
REM （那种情况脚本会自动降级成用浏览器打开 NapCat 官方扫码页）。
set "PYW=__PYW__"
if not exist "%PYW%" set "PYW=__PY__"

echo.
echo   ============================================================
echo     小柚 - QQ 扫码登录
echo   ============================================================
echo.
echo   QQ 登录态过期了，需要你重新扫一次码。
echo.
echo   马上会弹出一个二维码窗口 —— 用【机器人那个 QQ 号】的
echo   手机 QQ 扫一扫，并在手机上点确认。
echo.
echo   二维码会自动刷新，不用担心它过期。
echo   扫描成功后窗口会自己关掉，机器人约 10 秒后上线。
echo.

start "" "%PYW%" "%~dp0扫码登录.py"
timeout /t 5 >nul
exit /b 0
"""

QUICKLOGIN = r"""@echo off
chcp 936 >nul
title 小柚 - 设置免扫码登录
cd /d "%~dp0"

echo.
echo  ============================================================
echo    小柚 - 设置免扫码登录
echo  ============================================================
echo.
echo   为什么要用这个：
echo     机器人每次启动都要你扫一次码。根因是 NapCat 的「本地登录票据」
echo     在这台机器上失效了，而它又没配「密码回退」，于是每次都掉到二维码。
echo     这里填一次 QQ 密码，只在本机换算成 MD5 保存（不存明文），
echo     以后启动就直接密码登录，不用再扫。
echo.
echo   [1] 设置 / 更新密码     推荐
echo   [2] 看当前状态
echo   [3] 清空密码，退回扫码登录
echo   [0] 退出
echo.

set "CH="
set /p "CH= 请选择 [1/2/3/0]: "
if "%CH%"=="1" goto do_set
if "%CH%"=="2" goto do_status
if "%CH%"=="3" goto do_clear
goto done

:do_set
echo.
"__PY__" "%~dp0catbot\_setup_quick_login.py"
goto after

:do_status
echo.
"__PY__" "%~dp0catbot\_setup_quick_login.py" --status
goto done

:do_clear
echo.
"__PY__" "%~dp0catbot\_setup_quick_login.py" --clear
goto after

:after
echo.
echo  ------------------------------------------------------------
echo   改完要重启一次才生效：双击「重启机器人.bat」。
echo   重启后日志里应该出现「密码回退登录成功」，而不是二维码。
echo.

:done
pause
exit /b 0
"""

SETTINGS = r"""@echo off
chcp 936 >nul
title 小柚 设置台
cd /d "%~dp0"

echo.
echo  ============================================================
echo    小柚 - 设置台
echo  ============================================================
echo.
echo   正在打开设置页 ... 浏览器会自动弹出。
echo   没弹的话，把下方那行 http://127.0.0.1:... 复制到浏览器打开。
echo.
echo   用法：改完点右下角【保存】，再点【保存并重启】。
echo   关掉本窗口 = 关闭设置页（配置已写进 config.json，不会丢）。
echo.

"__PY__" "%~dp0catbot\settings_page.py"

echo.
echo  设置页已关闭。
pause >nul
"""

VOICE_START = r"""@echo off
chcp 936 >nul
title 小柚 语音服务
cd /d "%~dp0"

echo ============================================================
echo   小柚 - 本机语音服务（GPT-SoVITS）
echo ============================================================
echo.

REM ==== GPT-SoVITS 位置（可选组件，不用语音克隆就不必管）====
REM 三选一，按顺序生效：
REM   1) 环境变量 XIAOYOU_GSV 指到 GPT-SoVITS 目录
REM   2) 与项目同级的 GPT-SoVITS 文件夹
REM   3) __GSV__（生成本 bat 时探测到的位置）
set "GSV=%XIAOYOU_GSV%"
if not exist "%GSV%\venv\Scripts\pythonw.exe" set "GSV=%~dp0..\GPT-SoVITS"
if not exist "%GSV%\venv\Scripts\pythonw.exe" set "GSV=__GSV__"
set "PYW=%GSV%\venv\Scripts\pythonw.exe"

REM 说完话闲多少秒，就把模型从显存卸回内存（省显存）。
REM   0    = 关掉这个特性，模型一直占着显卡（约 1.5G）
REM   180  = 默认，闲 3 分钟后只留几百兆
REM   10   = 最省显存，但每次说话前要多等 1 秒左右把模型搬回显卡
set "GSV_IDLE_UNLOAD_SEC=180"

if not exist "%PYW%" goto :missing

REM 幂等：端口已在监听就说明服务活着，别重复拉起一份去抢显存
netstat -ano | findstr ":9880" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  语音服务已经在跑了（端口 9880），不用重复启动。
    echo.
    timeout /t 4 >nul
    exit /b 0
)

echo  正在拉起语音服务 ...
echo.
echo  * 启动时要读一次模型，大约 20~40 秒；读完她就能用新嗓子说话。
echo  * 模型**只在说话时上显卡**。闲 %GSV_IDLE_UNLOAD_SEC% 秒后自动卸回内存，
echo    显存只剩几百兆的底子（原来一直占着约 1.5G）。
echo    想让她随时秒接话、不在意显存，把上面 set "GSV_IDLE_UNLOAD_SEC=..." 改成 0。
echo  * 它故意没有窗口。出问题看这份日志：
echo      %GSV%\logs\api_v2.log
echo  * 不想用它也没关系 —— 服务没起来时她会自动用 Edge 音色，
echo    只是少了情绪和专属音色，不会变成哑巴。
echo.

cd /d "%GSV%"
start "" "%PYW%" "_run_api.py"

timeout /t 3 >nul
echo  已发出启动指令。约半分钟后她就能用新嗓子说话了。
echo.
timeout /t 5 >nul
exit /b 0

:missing
echo  [错误] 找不到语音服务程序：
echo         %PYW%
echo.
echo  说明 GPT-SoVITS 不在上面那个路径，改 set "GSV=..." 这一行即可。
pause
exit /b 1
"""

VOICE_STOP = r"""@echo off
chcp 936 >nul
title 小柚 语音服务 - 停止
cd /d "%~dp0"

echo.
echo  正在请语音服务退出 ...
echo.

"__PY__" -c "import urllib.request as u; u.urlopen('http://127.0.0.1:9880/control?command=exit', timeout=6)" 2>nul
if errorlevel 1 (
    echo  没连上服务 —— 它本来就没在跑。
) else (
    echo  已请求退出，端口几秒后就释放了。
)

echo.
timeout /t 5 >nul
exit /b 0
"""

BATS = {
    "一键启动.bat": LAUNCHER,
    "_start_napcat.bat": NAPCAT,
    "_start_bot.bat": BOT,
}

# 顶层入口（用户真正双击的那些）。历史上那 7 个 bat 没留源文件，
# 所以不在这里生成；**新增入口一律加到这儿**，别再手写 .bat ——
# 手写必然踩 LF/UTF-8 的坑，表现是双击窗口一闪就没。
ROOT_BATS = {
    "扫码登录.bat": SCANLOGIN,
    "设置免扫码登录.bat": QUICKLOGIN,
    "设置页.bat": SETTINGS,
    "语音服务-启动.bat": VOICE_START,
    "语音服务-停止.bat": VOICE_STOP,
}


def _find_gsv():
    """探测 GPT-SoVITS 目录（可选组件）。找不到就留个占位，bat 里会提示。"""
    env = os.environ.get("XIAOYOU_GSV")
    if env and os.path.exists(os.path.join(env, "venv", "Scripts", "pythonw.exe")):
        return env
    # 与项目同级，以及常见的 Downloads 位置
    for cand in (os.path.join(os.path.dirname(HERE), "GPT-SoVITS"),
                 r"C:\GPT-SoVITS", r"D:\GPT-SoVITS"):
        if os.path.exists(os.path.join(cand, "venv", "Scripts", "pythonw.exe")):
            return cand
    return r"C:\GPT-SoVITS"      # 占位：bat 会提示"没找到"，不会静默出错


GSV_DIR = _find_gsv()


def _emit(path, text):
    text = text.replace("__PY__", PY)
    text = text.replace("__PYW__", PYW_FALLBACK)
    text = text.replace("__GSV__", GSV_DIR)
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode("gbk")
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def _check(path):
    d = open(path, "rb").read()
    crlf = d.count(b"\r\n")
    lf = d.count(b"\n") - crlf
    bom = d[:3] == b"\xef\xbb\xbf"
    return crlf > 0 and lf == 0 and not bom, crlf, lf, bom


def main():
    root = os.path.dirname(HERE)
    targets = [(os.path.join(HERE, n), t) for n, t in BATS.items()]
    targets += [(os.path.join(root, n), t) for n, t in ROOT_BATS.items()]

    for path, text in targets:
        n = _emit(path, text)
        print("[写入] %-20s %5d 字节" % (os.path.basename(path), n))

    print()
    allok = True
    for path, _ in targets:
        ok, crlf, lf, bom = _check(path)
        allok = allok and ok
        print("[%s] %-20s CRLF=%d 裸LF=%d BOM=%s" % (
            "OK " if ok else "BAD", os.path.basename(path), crlf, lf, "有" if bom else "无"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
