# -*- coding: utf-8 -*-
"""把小柚的 QQ 号 + 密码 MD5 写进 NapCat 的 config/.env，实现「不用扫码」。

背景（2026-09-25 实测结论）
---------------------------------------------------------------
NapCat 独立模式的登录链路是三级：

    ① 本地登录票据（`-q <QQ号>` 走这条）
    ② 密码回退（读 config/.env 里的 NAPCAT_QUICK_PASSWORD / _MD5）
    ③ 二维码（兜底）

这台机器上 ① 常常成功不了 —— 日志会变成
    「正在快速登录 <QQ号>」→「快速登录错误：你的用户身份已失效」
（账号被腾讯踢下线之后，票据在腾讯那边就作废了。）
而 ② 又没配置，所以每次都掉到 ③ 扫码。

于是把 ② 补上，往后启动就能走 密码登录 → 少扫很多码。
只有 QQ 触发「验证码 / 新设备验证」时才会退回人工，那种情况到
WebUI（http://127.0.0.1:6099）里点一下就好了。

于是只要把 ② 补上，启动就会 密码登录 → 不再需要扫码。
只有 QQ 触发「验证码 / 新设备验证」时才会退回扫码，那种情况到
WebUI（http://127.0.0.1:6099）里点一下就好了。

NapCat 认的键（读的是 <napcat.mjs 所在目录>/config/.env）
---------------------------------------------------------------
    ACCOUNT=<QQ号>
    NAPCAT_QUICK_ACCOUNT=<QQ号>
    NAPCAT_QUICK_PASSWORD_MD5=<密码的 32 位小写 MD5>

MD5 算法与 NapCat 内部完全一致：md5(密码的 UTF-8 字节) 的小写 hex
（源码：crypto.createHash("md5").update(密码, "utf8").digest("hex")）。

安全说明
---------------------------------------------------------------
落盘的是 **密码的 MD5**，不是明文密码。但它等价于一把能登录这个 QQ 的
钥匙（能反推弱密码），所以：
  - 只在这台机器上写，别把这个 .env 发出去、别提交到网上；
  - 想撤销：跑 `python _setup_quick_login.py --clear`，立刻退回扫码登录。

跑法
---------------------------------------------------------------
    python _setup_quick_login.py            # 交互输入密码（不回显），写入
    python _setup_quick_login.py --status   # 只看现在是什么状态
    python _setup_quick_login.py --clear    # 删掉密码，退回扫码
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent              # catbot/
ENV_PATH = HERE / "napcat" / "napcat" / "config" / ".env"
ACCOUNT_FILE = HERE / "napcat_account.txt"

HEADER = """\
# 小柚 NapCat 自动登录配置
# ------------------------------------------------------------------
# 这个文件由「设置免扫码登录.bat」自动维护，手改也认。
# 作用是补上 NapCat 的第二级登录：本地票据失效时用密码登录，别再要你扫码。
# 改完要重启协议层才生效（跑「重启机器人.bat」，或双击「一键启动.bat」）。
#
# 不想用了：双击「设置免扫码登录.bat」选清空，或把这行 MD5 删掉即可退回扫码。
# ------------------------------------------------------------------
"""

KEY_ACCOUNT = "ACCOUNT"
KEY_QUICK_ACCOUNT = "NAPCAT_QUICK_ACCOUNT"
KEY_PASSWORD_MD5 = "NAPCAT_QUICK_PASSWORD_MD5"

# NapCat 源码里用的是 /^[a-fA-F0-9]{32}$/ 校验，且只认小写返回
MD5_RE = re.compile(r"^[a-f0-9]{32}$")


# ---------------------------------------------------------------- 读写

def read_env() -> dict[str, str]:
    """读现有 .env（文件不存在就返回空）。"""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_env(account: str, md5_hex: str) -> None:
    """整份重写 .env（这个小文件只有我们几个键，重写比打补丁稳）。"""
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        HEADER.rstrip("\n"),
        "",
        f"{KEY_ACCOUNT}={account}",
        f"{KEY_QUICK_ACCOUNT}={account}",
        f"{KEY_PASSWORD_MD5}={md5_hex}",
        "",
    ]
    # NapCat 用 readFileSync(..., "utf8") 读它，所以必须是 UTF-8；
    # 不要写 BOM —— 带了 BOM 第一个键就变成 "\ufeff#..."，虽然因为是注释
    # 会被跳过，但没必要留这个雷。
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def read_account() -> str:
    """优先用 napcat_account.txt（daemon 也用它做 -q）。"""
    if ACCOUNT_FILE.exists():
        s = ACCOUNT_FILE.read_text(encoding="utf-8", errors="replace").strip()
        if s:
            return s
    return ""


def md5_of(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def mask(h: str) -> str:
    return f"{h[:6]}…{h[-4:]}" if len(h) == 32 else "(无)"


# ---------------------------------------------------------------- 动作

def do_status() -> int:
    acc = read_account()
    env = read_env()
    cur = env.get(KEY_PASSWORD_MD5, "")

    print("=" * 60)
    print("  小柚 · 免扫码登录 状态")
    print("=" * 60)
    print(f"  配置文件      {ENV_PATH}")
    print(f"  文件存在      {'是' if ENV_PATH.exists() else '否'}")
    print(f"  机器人 QQ 号  {acc or '（没读到，检查 napcat_account.txt）'}")
    print(f"  .env 里的号   {env.get(KEY_ACCOUNT) or '（未设置）'}")
    print(f"  密码 MD5      {mask(cur)}")

    if cur and not MD5_RE.match(cur):
        print("  [!!] MD5 格式不对（必须 32 位十六进制）—— 会被 NapCat 忽略")
    if cur:
        print("\n  => 已配置。启动时 NapCat 会先试登录票据，失败就用密码登录，")
        print("     正常情况不再需要扫码。")
        if acc and env.get(KEY_ACCOUNT) and acc != env.get(KEY_ACCOUNT):
            print(f"\n  [!!] 账号不一致：napcat_account.txt={acc} / .env={env.get(KEY_ACCOUNT)}")
            print("       重跑一次本脚本，会以 napcat_account.txt 为准重写。")
    else:
        print("\n  => 没配置。启动时票据一失效就会退到扫码 ——")
        print("     双击「设置免扫码登录.bat」填一次密码即可。")
    print("=" * 60)
    return 0


def do_clear() -> int:
    env = read_env()
    if not env.get(KEY_PASSWORD_MD5):
        print("本来就没配密码，不用清。")
        return 0
    acc = read_account() or env.get(KEY_ACCOUNT, "")
    if not acc:
        print("[错误] 读不到机器人 QQ 号，先检查 napcat_account.txt")
        return 1
    write_env(acc, "")
    print("已清空密码 MD5 —— 退回扫码登录。")
    print(f"（{ENV_PATH}）")
    return 0


def do_set(password: str) -> int:
    acc = read_account()
    if not acc:
        print("[错误] 读不到机器人 QQ 号（catbot/napcat_account.txt）。")
        print("       先让机器人成功登录一次，或手动把 QQ 号写进那个文件。")
        return 1
    if not acc.isdigit():
        print(f"[错误] 机器人 QQ 号不像数字：{acc!r}")
        return 1
    if not password:
        print("没输入密码，已取消（什么都没改）。")
        return 1

    h = md5_of(password)
    write_env(acc, h)

    # 回读确认，避免"写进去了但其实没生效"
    back = read_env()
    ok = (back.get(KEY_PASSWORD_MD5) == h
          and back.get(KEY_ACCOUNT) == acc
          and back.get(KEY_QUICK_ACCOUNT) == acc)

    print("=" * 60)
    print("  已写入免扫码登录配置")
    print("=" * 60)
    print(f"  机器人 QQ 号  {acc}")
    print(f"  密码 MD5      {mask(h)}")
    print(f"  配置文件      {ENV_PATH}")
    print(f"  回读校验      {'通过' if ok else '失败（请重跑）'}")
    print("\n  下一步：重启协议层让它生效 ——")
    print("    双击「重启机器人.bat」；或先「停止机器人.bat」再「一键启动.bat」。")
    print("\n  重启后应该看到日志里出现「密码回退登录成功」，而不是二维码。")
    print("  如果 QQ 要求验证码/新设备验证，会弹出提示，")
    print("  到 http://127.0.0.1:6099 的 WebUI 里点一下就行（只需一次）。")
    print("=" * 60)
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(
        description="把小柚的 QQ 密码 MD5 写进 NapCat 的 config/.env，实现免扫码登录")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="只看当前配置状态")
    g.add_argument("--clear", action="store_true", help="清空密码，退回扫码登录")
    ap.add_argument("--password", default=None,
                    help="直接给密码（会留在命令历史里，不推荐；默认交互输入不回显）")
    args = ap.parse_args()

    if args.status:
        return do_status()
    if args.clear:
        return do_clear()

    print("=" * 60)
    print("  小柚 · 设置免扫码登录")
    print("=" * 60)
    print("  这一步会把 QQ 密码换算成 MD5 存到本机 NapCat 配置里，")
    print("  以后启动就直接密码登录，不用再扫码。")
    print("  （只存 MD5，不存明文；想撤销就跑 --clear）")
    print()

    pw = args.password
    if pw is None:
        try:
            pw = getpass.getpass("  请输入机器人 QQ 号的密码（输入时不显示）: ")
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            return 1
        if pw:
            again = getpass.getpass("  再输一遍确认: ")
            if again != pw:
                print("\n两次输入不一致，已取消（什么都没改）。")
                return 1
    print()
    return do_set(pw)


if __name__ == "__main__":
    sys.exit(main())
