# -*- coding: utf-8 -*-
"""免扫码登录配置助手（_setup_quick_login.py）的单元测试。

跑法: python _test/test_quick_login.py

**不碰真实的 .env** —— 全部写到 tmp 下的临时路径。
真 .env 一旦写错，NapCat 会拿错误的密码去登录，反而多几次失败。
"""

import sys
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _setup_quick_login as q  # noqa: E402

PASS = FAIL = 0
TMP = ROOT / "tmp" / "_test_quick_login"


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


# ---- 隔离：把所有落盘路径改到 tmp ----
TMP.mkdir(parents=True, exist_ok=True)
ENV = TMP / "config" / ".env"
ACC = TMP / "napcat_account.txt"
q.ENV_PATH = ENV
q.ACCOUNT_FILE = ACC
for p in (ENV, ACC):
    if p.exists():
        p.unlink()

print("\n=== 1. MD5 算法必须和 NapCat 一致 ===")
# NapCat 源码: crypto.createHash("md5").update(密码, "utf8").digest("hex")
check("纯 ASCII 密码",
      q.md5_of("abc123"), hashlib.md5(b"abc123").hexdigest())
check("已知向量 123456",
      q.md5_of("123456"), "e10adc3949ba59abbe56e057f20f883e")
check("中文密码按 UTF-8 编码（不是 GBK）",
      q.md5_of("密码"), hashlib.md5("密码".encode("utf-8")).hexdigest())
check_true("输出是小写 32 位",
           bool(q.MD5_RE.match(q.md5_of("XyZ-随便 空格"))))
check("大小写敏感（Pwd 与 pwd 不同）",
      q.md5_of("Pwd") == q.md5_of("pwd"), False)

print("\n=== 2. 没有账号文件时不乱写 ===")
check("do_set → 读不到账号返回 1", q.do_set("whatever"), 1)
check("且没有产生 .env", ENV.exists(), False)

print("\n=== 3. 正常写入 + 回读 ===")
ACC.write_text("1234567890", encoding="utf-8")
check("do_set 返回 0", q.do_set("MyQqPassw0rd"), 0)
check_true(".env 已生成", ENV.exists())
env = q.read_env()
check("ACCOUNT", env.get(q.KEY_ACCOUNT), "1234567890")
check("NAPCAT_QUICK_ACCOUNT", env.get(q.KEY_QUICK_ACCOUNT), "1234567890")
check("NAPCAT_QUICK_PASSWORD_MD5", env.get(q.KEY_PASSWORD_MD5),
      hashlib.md5(b"MyQqPassw0rd").hexdigest())

print("\n=== 4. 文件格式必须被 NapCat 解析器吃下去 ===")
raw = ENV.read_bytes()
check_true("UTF-8 无 BOM", not raw.startswith(b"\xef\xbb\xbf"))
text = raw.decode("utf-8")
check_true("没有裸 LF（统一 \\n，NapCat 用 /\\r?\\n/ 切）", "\r" not in text)
# 复刻 NapCat 的解析循环，确认键值真能读出来
parsed = {}
for line in text.splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and v:
            parsed[k] = v
check("NapCat 解析出的键数", len(parsed), 3)
check("NapCat 解析出的 MD5", parsed.get(q.KEY_PASSWORD_MD5),
      hashlib.md5(b"MyQqPassw0rd").hexdigest())
check_true("注释行不被当成配置",
           all(not k.startswith("#") for k in parsed))

print("\n=== 5. 密码不入明文、不入注释 ===")
check_true("全文不含明文密码", "MyQqPassw0rd" not in text)

print("\n=== 6. 清空 = 退回扫码（但保留账号，别把 -q 一起弄丢）===")
check("do_clear 返回 0", q.do_clear(), 0)
env = q.read_env()
check("MD5 置空", env.get(q.KEY_PASSWORD_MD5, ""), "")
check("账号仍在", env.get(q.KEY_ACCOUNT), "1234567890")
check("空值行不会被 NapCat 当成配置（真源校验）",
      len([1 for ln in ENV.read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.startswith("#")
           and ln.partition("=")[2].strip()]), 2)
check("重复 clear 幂等（返回 0）", q.do_clear(), 0)

print("\n=== 7. 账号非法时不写 ===")
ACC.write_text("不是数字", encoding="utf-8")
check("do_set 返回 1", q.do_set("x"), 1)
ACC.write_text("1234567890", encoding="utf-8")

print("\n=== 8. 空密码 = 取消，不改动 ===")
q.do_clear()
check("do_set('') 返回 1", q.do_set(""), 1)
check("MD5 仍未设置", q.read_env().get(q.KEY_PASSWORD_MD5, ""), "")

print("\n=== 9. 账号变更后重写会跟随 napcat_account.txt ===")
q.do_set("pw")
ACC.write_text("9999999999", encoding="utf-8")
q.do_set("pw")
env = q.read_env()
check("ACCOUNT 跟着新账号", env.get(q.KEY_ACCOUNT), "9999999999")
ACC.write_text("1234567890", encoding="utf-8")

print("\n=== 10. 状态面板那一行（service_ctl.quick_login_status）===")
# 关键：面板必须**委托**给这里判定，不能自己再解析一遍 .env。
# 这项目栽过一次「同一个判断写两处」（tts_engine_name 报了错的引擎名），
# 所以这里断言「改 .env 后面板结论跟着变」。
import service_ctl as sc  # noqa: E402

check("_STATE_MARK 覆盖三种状态",
      sorted(sc._STATE_MARK), ["bad", "na", "ok"])

q.do_clear()
state, text = sc.quick_login_status()
check("没配 → bad", state, "bad")
check_true("没配的文案指向新 bat", "设置免扫码登录.bat" in text)

q.do_set("pw")
state, _ = sc.quick_login_status()
check("配好 → ok", state, "ok")

# 手写一个坏 MD5（模拟用户手改坏了）
bad = ENV.read_text(encoding="utf-8").replace(
    q.read_env()[q.KEY_PASSWORD_MD5], "not-a-valid-md5")
ENV.write_text(bad, encoding="utf-8")
state, text = sc.quick_login_status()
check("坏 MD5 → bad（不能误报 ok）", state, "bad")
check_true("坏 MD5 的文案说得清问题", "格式" in text)

# 助手脚本反过来被面板读到 → 两条路径读的是同一份真源
check_true("面板与助手读同一份配置",
           sc.quick_login_status()[0] == ("ok" if q.MD5_RE.match(
               q.read_env().get(q.KEY_PASSWORD_MD5, "") or "") else "bad"))

# 模块缺失时不能崩，只能降级
import sys as _sys  # noqa: E402
_saved = _sys.modules.get("_setup_quick_login")
_sys.modules["_setup_quick_login"] = None      # import 会抛 ImportError
state, text = sc.quick_login_status()
check("助手不在 → na（不崩）", state, "na")
if _saved is not None:
    _sys.modules["_setup_quick_login"] = _saved

print("\n=== 11. 助手顶层不能有副作用（这个项目踩过）===")
# service_ctl 曾经在模块顶层 reconfigure(stdout)，任何 import 它的进程
# 输出当场变乱码。助手脚本同理会污染调用方。
import subprocess  # noqa: E402
r = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, r'%s'); "
     "import _setup_quick_login; "
     "sys.stdout.write('OK')" % ROOT],
    capture_output=True)
check("import 时不打印东西、不改标准输出", r.stdout, b"OK")
check("import 时返回码 0", r.returncode, 0)

# 收拾干净
for p in (ENV, ACC):
    if p.exists():
        p.unlink()

print("\n" + "=" * 50)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 50)
sys.exit(0 if FAIL == 0 else 1)
