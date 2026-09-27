#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
自动配置 NapCat 的 OneBot 11 网络服务。

需要两项：
  - HTTP Server  → 127.0.0.1:3000  (机器人用来发消息)
  - WS Server    → 127.0.0.1:3001  (机器人用来收消息)

用法:
    python setup_napcat.py                 # 自动发现 QQ 号并配置
    python setup_napcat.py --uin 123456    # 指定 QQ 号（还没登录时先建好）
    python setup_napcat.py --check         # 只看当前配置
"""

import argparse
import glob
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "napcat" / "napcat" / "config"
ACCOUNT_FILE = ROOT / "napcat_account.txt"

HTTP_PORT = 3000
WS_PORT = 3001
HOST = "127.0.0.1"

HTTP_TPL = {
    "name": "catbot-http",
    "enable": True,
    "port": HTTP_PORT,
    "host": HOST,
    "enableCors": True,
    "enableWebsocket": False,
    "messagePostFormat": "array",
    "token": "",
    "debug": False,
}

WS_TPL = {
    "name": "catbot-ws",
    "enable": True,
    "host": HOST,
    "port": WS_PORT,
    "messagePostFormat": "array",
    "reportSelfMessage": False,
    "token": "",
    "enableForcePushEvent": True,
    "debug": False,
    "heartInterval": 30000,
}

EMPTY = {
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [],
    "plugins": [],
}


def merge_entry(items: list, template: dict, match_keys=("port", "host")) -> tuple[list, str]:
    """已有同端口的就更新，否则追加。返回 (新列表, 动作说明)。"""
    for it in items:
        if not isinstance(it, dict):
            continue
        if all(it.get(k) == template.get(k) for k in match_keys):
            changed = {k: v for k, v in template.items() if it.get(k) != v}
            it.update(template)
            return items, ("更新" if changed else "已是最新")
    items.append(dict(template))
    return items, "新增"


def configure(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as e:
        print(f"  ! 解析失败({e})，将备份后重建")
        shutil.copy2(path, path.with_suffix(".json.bak"))
        data = {}

    # 【重要】NapCat v4 真正读取的是 data["network"][...]；
    # 顶层那份同名数组只是旧格式残留（NapCat 自己的模板里也留着）。
    # 必须写进 network 层才会生效，两边都写以兼容不同版本。
    net = data.get("network")
    if not isinstance(net, dict):
        net = {}
        data["network"] = net

    for k, v in EMPTY.items():
        if not isinstance(net.get(k), list):
            net[k] = list(v)
        if not isinstance(data.get(k), list):
            data[k] = list(v)

    net["httpServers"], a1 = merge_entry(net["httpServers"], HTTP_TPL)
    net["websocketServers"], a2 = merge_entry(net["websocketServers"], WS_TPL)

    data["httpServers"] = [dict(x) if isinstance(x, dict) else x for x in net["httpServers"]]
    data["websocketServers"] = [dict(x) if isinstance(x, dict) else x for x in net["websocketServers"]]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"http": a1, "ws": a2, "path": path}


def describe(path: Path) -> None:
    print(f"\n配置文件: {path}")
    if not path.exists():
        print("  不存在")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  解析失败: {e}")
        return
    net = data.get("network") if isinstance(data.get("network"), dict) else {}
    for key, label in (("httpServers", "HTTP Server"), ("websocketServers", "WS Server"),
                       ("httpClients", "HTTP Client"), ("websocketClients", "WS Client")):
        items = net.get(key) or data.get(key) or []
        if not items:
            print(f"  {label}: (空)")
        for it in items:
            flag = "启用" if it.get("enable") else "停用"
            print(f"  {label}: {flag}  {it.get('host')}:{it.get('port')}  name={it.get('name')}")


def read_existing_account() -> str:
    """已经记录过谁就用谁 —— 绝不能因为写配置刷新了文件时间就换号。"""
    try:
        uin = ACCOUNT_FILE.read_text(encoding="ascii", errors="ignore").strip()
        return uin if uin.isdigit() else ""
    except OSError:
        return ""


def pick_account(targets) -> str:
    """首次运行时挑机器人 QQ 号：取改动时间最新的配置（≈ 最后一次登录的号）。"""
    best, best_m = "", -1.0
    for t in targets:
        stem = t.stem  # 形如 onebot11_<QQ号>
        uin = stem.split("_", 1)[1] if "_" in stem else ""
        if not uin.isdigit():
            continue
        try:
            m = t.stat().st_mtime
        except OSError:
            m = 0.0
        if m > best_m:
            best, best_m = uin, m
    return best


def write_account(uin: str) -> None:
    """写一个纯数字的小文件，_start_napcat.bat 读它做免扫码快速登录。
    故意不写结尾换行 —— cmd 的 set /p 读起来最干净。"""
    if not uin:
        return
    try:
        ACCOUNT_FILE.write_bytes(uin.encode("ascii"))
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uin", help="机器人 QQ 号，用于指定配置文件名")
    ap.add_argument("--check", action="store_true", help="只查看配置")
    ap.add_argument("--auto", action="store_true",
                    help="自动配置；若还没有配置文件则以退出码 3 结束（供启动器判断首次运行）")
    args = ap.parse_args()

    if not CONFIG_DIR.parent.exists():
        print(f"找不到 NapCat 目录: {CONFIG_DIR.parent}")
        print("请确认 napcat/ 已解压到本项目目录下。")
        return 1

    if args.check:
        found = sorted(glob.glob(str(CONFIG_DIR / "onebot11*.json")))
        if not found:
            print(f"{CONFIG_DIR} 下还没有 onebot11 配置（NapCat 登录后会生成）")
            return 0
        for f in found:
            describe(Path(f))
        return 0

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if args.uin:
        targets = [CONFIG_DIR / f"onebot11_{args.uin}.json"]
    else:
        targets = [Path(p) for p in sorted(glob.glob(str(CONFIG_DIR / "onebot11*.json")))]
        if not targets:
            if args.auto:
                print("  NapCat 尚未登录过，暂无配置文件。")
                return 3
            print("还没有找到 onebot11_<QQ号>.json —— NapCat 可能还没登录过。")
            print("\n两种做法：")
            print("  1) 先启动 NapCat 扫码登录，然后重新运行本脚本；")
            print("  2) 直接指定你的机器人 QQ 号：")
            print("     python setup_napcat.py --uin 123456")
            return 0

    print(f"  目标配置: {', '.join(p.name for p in targets)}")
    for t in targets:
        r = configure(t)
        print(f"  [OK] {r['path'].name}")
        print(f"       HTTP Server : {r['http']}")
        print(f"       WS   Server : {r['ws']}")

    # 已有记录就沿用，别用文件时间重新挑（写配置会刷新时间戳，会挑错号）
    uin = args.uin or read_existing_account() or pick_account(targets)
    write_account(uin)
    if uin:
        print(f"  机器人账号: {uin}")
        print(f"  （已记到 napcat_account.txt，下次启动免扫码自动登录）")

    print("  配置完成，需重启 NapCat 生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
