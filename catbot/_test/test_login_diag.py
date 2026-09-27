# -*- coding: utf-8 -*-
"""NapCat 登录现场解析（service_ctl._parse_login_log）的单元测试。

跑法: python _test/test_login_diag.py

**测试里的日志行全部是从真实 napcat_console.log 抄下来的原话**，
不是我凭印象编的。这个功能的价值就在于"照着日志说真话"，
用编的字符串去测，等于自己给自己盖章。

纯函数测试：喂文本，不碰网络、不碰进程、不碰真日志。
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import service_ctl as sc  # noqa: E402

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


def stamp(minutes_ago: float) -> str:
    """生成一条距今 N 分钟的日志时间戳（MM-DD HH:MM:SS）。"""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(time.time() - minutes_ago * 60))


def line(minutes_ago, level, text):
    return "%s [%s] %s" % (stamp(minutes_ago), level, text)


# ---- 真实日志原话（照抄，改时间戳） ----
KICK = "请输入文本 | [KickedOffLine] [下线通知] 你的账号当前登录已失效，请重新登录。"
QUICKFAIL_A = "快速登录错误： 登录态已失效，请重新登录。"
QUICKFAIL_B = "快速登录错误： 你的用户身份已失效，为保证账号安全，请你重新登录。"
PWD_LOGIN = "正在密码登录  1234567890"
CAPTCHA_URL = ("需要验证码, proofWaterUrl:  https://ti.qq.com/safe/tools/captcha/"
               "sms-verify-login?aid=2086438785&login_appid=1600001604&"
               "sid=8850320636832537219&uin=1234567890")
CAPTCHA_WEBUI = "[NapCat] [WebUi] 自动密码回退登录需要验证码，请在登录页面继续完成: 1234567890"
RISK = ('[Core] [Login] Login Error , ErrInfo:  [1,{"account":"1234567890",'
        '"serverErrorCode":168,"title":"","message":"你的账号近期存在安全风险，'
        '部分功能使用受限，请登录最新手机QQ并根据提示恢复账号使用。",'
        '"additionalType":0,"additionalMessage":""}]')
SCANNED = "[Core] [Login] 二维码已被扫描，等待确认..."

print("\n=== 1. 空日志 / 无关日志 ===")
r = sc._parse_login_log("")
check("空文本 → issue 为 None", r["issue"], None)
check("空文本 → kicks_24h 为 0", r["kicks_24h"], 0)
check("空文本 → captcha_url 为 None", r["captcha_url"], None)
r = sc._parse_login_log(line(1, "info", "接收 <- 私聊 (10001) 中秋节快乐"))
check("普通聊天日志不产生 issue", r["issue"], None)

print("\n=== 2. 被踢下线（起因，不是 issue）===")
r = sc._parse_login_log(line(5, "error", KICK))
check("被踢不产生 issue", r["issue"], None)
check_true("能报出被踢时间", r["last_kick_min"] is not None
           and 4 <= r["last_kick_min"] <= 6, r["last_kick_min"])
check("24 小时内被踢 1 次", r["kicks_24h"], 1)

print("\n=== 3. 需要短信验证码（本次真实故障）===")
txt = "\n".join([line(10, "error", KICK),
                 line(9, "info", QUICKFAIL_B),
                 line(9, "info", PWD_LOGIN),
                 line(9, "warn", "请扫描下面的二维码，然后在手Q上授权登录："),
                 line(9, "info", CAPTCHA_URL),
                 line(9, "info", CAPTCHA_WEBUI)])
r = sc._parse_login_log(txt)
check("识别为 captcha", r["issue"][0], "captcha")
check_true("抓到验证链接", (r["captcha_url"] or "").startswith(
    "https://ti.qq.com/safe/tools/captcha/sms-verify-login"), r["captcha_url"])
check_true("链接带 uin", "uin=1234567890" in (r["captcha_url"] or ""))
check_true("captcha_min 合理", 8 <= (r["captcha_min"] or 0) <= 11, r["captcha_min"])
check_true("同时报出被踢", r["last_kick_min"] is not None)

print("\n=== 4. ⚠️ 回归：带链接那行不含「登录」二字 ===")
# 真实那行是 `[info] 需要验证码, proofWaterUrl:  https://.../sms-verify-login?...`
# 只在"登录/Login"上过滤，会把唯一能救命的链接整条漏掉（踩过）。
r = sc._parse_login_log(line(3, "info", CAPTCHA_URL))
check_true("单独一行验证链接也能抓到", r["captcha_url"] is not None)
check_true("且被识别成 captcha", r["issue"] is not None and r["issue"][0] == "captcha")

print("\n=== 5. 两种「快速登录失败」措辞都要认 ===")
for tag, text in (("登录态已失效", QUICKFAIL_A), ("用户身份已失效", QUICKFAIL_B)):
    r = sc._parse_login_log(line(2, "error", text))
    check_true("认得 %s" % tag,
               r["issue"] is not None and r["issue"][0] == "quickfail",
               r["issue"])

print("\n=== 6. 风控（168）===")
r = sc._parse_login_log(line(30, "error", RISK))
check("识别为 risk", r["issue"][0], "risk")
check_true("详情含安全风险原话", "安全风险" in r["issue"][1], r["issue"][1])
check_true("详情带错误码", "168" in r["issue"][1], r["issue"][1])
check_true("距今约 0.5 小时", 0.4 <= r["issue"][2] <= 0.6, r["issue"][2])

print("\n=== 7. 扫码后被拒 ===")
r = sc._parse_login_log(line(1, "info", SCANNED))
check("识别为 scanned", r["issue"][0], "scanned")

print("\n=== 8. 优先级：越严重越优先 ===")
allof = "\n".join([line(9, "error", QUICKFAIL_B),
                   line(9, "info", CAPTCHA_URL),
                   line(9, "error", RISK),
                   line(9, "info", SCANNED)])
check("risk 压过其它", sc._parse_login_log(allof)["issue"][0], "risk")
no_risk = "\n".join([line(9, "error", QUICKFAIL_B),
                     line(9, "info", CAPTCHA_URL),
                     line(9, "info", SCANNED)])
check("captcha 压过 quickfail / scanned",
      sc._parse_login_log(no_risk)["issue"][0], "captcha")
no_cap = "\n".join([line(9, "error", QUICKFAIL_B), line(9, "info", SCANNED)])
check("quickfail 压过 scanned",
      sc._parse_login_log(no_cap)["issue"][0], "quickfail")

print("\n=== 9. 新旧取舍：同一种事取最新的那条 ===")
r = sc._parse_login_log("\n".join([line(200, "error", QUICKFAIL_A),
                                   line(3, "error", QUICKFAIL_A)]))
check_true("取 3 分钟前那条", 2.5 <= r["issue"][2] * 60 <= 3.5, r["issue"][2])

print("\n=== 10. 太老的 issue 不算，但被踢仍按 24h 窗口统计 ===")
r = sc._parse_login_log("\n".join([line(600, "error", QUICKFAIL_A)]),
                        max_age_hours=6.0)
check("10 小时前的失败不算 issue", r["issue"], None)
r = sc._parse_login_log("\n".join([line(600, "error", KICK)]))
check("10 小时前的被踢仍计入 24h", r["kicks_24h"], 1)
r = sc._parse_login_log("\n".join([line(600, "error", QUICKFAIL_A)]),
                        max_age_hours=24.0)
check_true("窗口放宽后又能看到", r["issue"] is not None)

print("\n=== 11. 被踢次数统计（今天真实发生的 4 次）===")
four = "\n".join([line(430, "error", KICK), line(370, "error", KICK),
                  line(190, "error", KICK), line(12, "error", KICK)])
check("统计到 4 次", sc._parse_login_log(four)["kicks_24h"], 4)
old = "\n".join([line(430, "error", KICK), line(30 * 60, "error", KICK)])
check("30 小时前那次不计入", sc._parse_login_log(old)["kicks_24h"], 1)

print("\n=== 11.5 被踢之后有没有又登上去过（面板别再说错话）===")
# 2026-09-26 实测：16:24 被踢 → 16:27 扫码回来 → 17:41 还在回话，
# 而面板照旧写「她已经哑了 1.9 小时」，只因为最近一条被踢记录在 16:24。
LOGIN_OK = "[NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略"

r = sc._parse_login_log("\n".join([line(200, "error", KICK)], ))
check("只被踢、没有成功记录 → last_ok_min 为 None", r["last_ok_min"], None)
check_true("此时才敢说「哑了很久」的前提成立",
           r["last_ok_min"] is None, r["last_ok_min"])

r = sc._parse_login_log("\n".join([line(200, "error", KICK),
                                   line(100, "info", LOGIN_OK)]))
check_true("被踢后又登上去过 → last_ok_min 有值", r["last_ok_min"] is not None
           and 95 <= r["last_ok_min"] <= 105, r["last_ok_min"])
check_true("而且比被踢更近（okm < km）", r["last_ok_min"] < r["last_kick_min"],
           (r["last_ok_min"], r["last_kick_min"]))

# 顺序反过来（先成功、后被踢）= 还没恢复，这才是真的哑着
r = sc._parse_login_log("\n".join([line(200, "info", LOGIN_OK),
                                   line(30, "error", KICK)]))
check_true("先成功后又被踢 → 仍是「哑着」", r["last_ok_min"] > r["last_kick_min"],
           (r["last_ok_min"], r["last_kick_min"]))

# 「快速登录错误 / 自动快速登录失败」是**失败**，绝不能算成登录成功
r = sc._parse_login_log("\n".join([line(20, "error", QUICKFAIL_B),
                                   line(19, "warn", "自动快速登录失败：身份已失效")]))
check("失败行不会被当成「已登录成功」", r["last_ok_min"], None)

print("\n=== 12. ANSI 颜色码 / 异常行不能把解析搞崩 ===")
weird = "\n".join(["\x1b[31m" + line(1, "error", QUICKFAIL_A) + "\x1b[0m",
                   "乱七八糟没有时间戳的一行",
                   "",
                   "[error] 快速登录错误： 用户身份已失效"])
r = sc._parse_login_log(weird)
check_true("带颜色码照样认得", r["issue"] is not None
           and r["issue"][0] == "quickfail", r["issue"])

print("\n=== 13. _fmt_age ===")
check("小于 1 分钟", sc._fmt_age(0.3), "刚刚")
check("分钟", sc._fmt_age(12.7), "12 分钟前")
check("小时", sc._fmt_age(150), "2.5 小时前")
check("天", sc._fmt_age(60 * 30), "1.2 天前")

print("\n=== 14. 旧接口 _napcat_login_issue 仍然可用（别破坏调用方）===")
check_true("_napcat_login_issue 是函数", callable(sc._napcat_login_issue))
r = sc._napcat_login_issue()
check_true("对真实日志不抛异常，返回 None 或三元组",
           r is None or (isinstance(r, tuple) and len(r) == 3))

print("\n=== 15. _tail_text 混编码不崩 ===")
tmp = ROOT / "tmp" / "_test_login_diag"
tmp.mkdir(parents=True, exist_ok=True)
p = tmp / "mixed.log"
p.write_bytes(b"2026-09-25 10:00:00 [info] " + "中文正常行".encode("utf-8") + b"\n"
              + b"2026-09-25 10:00:01 [info] " + "GBK编码行".encode("gbk") + b"\n")
t = sc._tail_text(p)
check_true("utf-8 行读对", "中文正常行" in t)
check_true("gbk 行至少没崩（不抛异常）", isinstance(t, str) and len(t) > 0)
check("文件不存在 → 空字符串", sc._tail_text(tmp / "nope.log"), "")
p.unlink()

print("\n=== 16. 面板输出必须 GBK 安全（否则静默变成问号）===")
# service_ctl 的 _setup_console() 把 stdout 设成 gbk + errors="replace"，
# 于是**任何 GBK 装不下的字符都会静默变成「?」**：不报错、不崩、
# 只是屏幕上少个字。⚠ / emoji / 各种花体符号全都中招。
# 已真踩过一次（新加的 [!] 警示原本写成 ⚠，用户看到的是「? 账号被踢下线」）。
# 这里守住 print 语句 —— 注释和 docstring 不进输出，不查它们。
_src = (ROOT / "service_ctl.py").read_text(encoding="utf-8").splitlines()
_bad = []
for _i, _ln in enumerate(_src, 1):
    if "print(" not in _ln:
        continue
    for _ch in _ln:
        try:
            _ch.encode("gbk")
        except UnicodeEncodeError:
            _bad.append((_i, _ch))
check("print 行里没有 GBK 装不下的字符", _bad, [])

# 顺带确认 _setup_console 真的还在 main() 里（挪到模块顶层会污染所有 import 方）。
# 判据：整行就是 `_setup_console()` 的只有一处，且**必须有缩进**（在函数体内）。
# 不能只数出现次数 —— 注释里提到它也算，那样测试会随注释变化而误报。
_calls = [l for l in _src if l.strip() == "_setup_console()"]
check("只在函数体里调用一次（不在模块顶层）",
      [len(l) - len(l.lstrip()) > 0 for l in _calls], [True])

print("\n=== 17. o3HookMode 读取（防掉线开关）===")
# ⚠️ 必须读**全局** napcat.json：napcat.mjs 里 hook 初始化用的是
# `aae(configPath)` → `join(configPath, "napcat.json")`，不认按账号的那份。
_fake = ROOT / "tmp" / "_test_hook_mode"
(_fake / "napcat" / "napcat" / "config").mkdir(parents=True, exist_ok=True)
_cfg = _fake / "napcat" / "napcat" / "config" / "napcat.json"
_real_base = sc.BASE_DIR
try:
    sc.BASE_DIR = _fake
    check("文件不存在 → None", sc.napcat_hook_mode(), None)

    _cfg.write_text('{"o3HookMode": 0}', encoding="utf-8")
    check("读到 0", sc.napcat_hook_mode(), 0)

    _cfg.write_text('{"o3HookMode": 1}', encoding="utf-8")
    check("读到 1", sc.napcat_hook_mode(), 1)

    _cfg.write_text('{"fileLog": false}', encoding="utf-8")
    check("字段缺失 → None", sc.napcat_hook_mode(), None)

    _cfg.write_text('{坏掉的 json', encoding="utf-8")
    check("JSON 损坏 → None（不崩）", sc.napcat_hook_mode(), None)
finally:
    sc.BASE_DIR = _real_base

# 本机真实状态哨兵：为治「反复被踢」建议把 o3HookMode 设为 0（schema 默认值）。
# ⚠️ 只在**装了 NapCat 且配置已生成**时才断言 —— 否则别人刚克隆下来
# （napcat/ 还没放进去）跑测试会平白报红，那种红是噪音不是问题。
if sc.napcat_hook_mode() is not None:
    check("本机全局 o3HookMode 已设为 0（防掉线建议值）", sc.napcat_hook_mode(), 0)
else:
    print("  [--]   跳过 o3HookMode 检查（还没有 napcat 配置，属正常）")
import json as _json  # noqa: E402
_per_acct = (ROOT / "napcat" / "napcat" / "config" / "napcat_1234567890.json")
check_true("按账号那份也一致（免得两份打架）",
           not _per_acct.exists()
           or _json.loads(_per_acct.read_text(encoding="utf-8")).get("o3HookMode") == 0)

print("\n=== 18. WebUI 登录页地址（验证码必须在这一页完成）===")
_u = sc._webui_login_url()
check_true("指向 /webui/web_login", "/webui/web_login" in _u, _u)
# token 是从 NapCat 日志里抓的 —— 没跑过 NapCat 就没有，那种情况不算失败
if "token=" in _u:
    check_true("带上了 webui token（打开即免输）", True, _u)
else:
    print("  [--]   跳过 token 检查（还没有 NapCat 日志，属正常）")
check_true("没有出现双斜杠或重复 webui", "//web" not in _u and _u.count("/webui") == 1, _u)
# 前端资源里确认过这个路由确实存在
check_true("路由名与 NapCat 前端一致（web_login）", "web_login" in _u, _u)

print("\n=== 19. 暖场「武装」情况（群暖场为什么不动）===")
_ns = ROOT / "tmp" / "_test_nudge_armed"
(_ns / "run").mkdir(parents=True, exist_ok=True)
_nsp = _ns / "run" / "nudge_state.json"
_rb2 = sc.BASE_DIR
try:
    sc.BASE_DIR = _ns
    _nsp.unlink(missing_ok=True)
    check("没有状态文件 → None", sc.nudge_armed(), None)

    _nsp.write_text("{坏掉的", encoding="utf-8")
    check("文件损坏 → None（不崩）", sc.nudge_armed(), None)

    _nsp.write_text(json.dumps({"version": 1, "sessions": {
        "g1": {"at": 1, "engaged": True},
        "g2": {"at": 1, "engaged": False},
        "p9": {"at": 1, "engaged": True},
    }}), encoding="utf-8")
    check("(回过话的, 总数)", sc.nudge_armed(), (2, 3))

    _nsp.write_text(json.dumps({"version": 1, "sessions": {}}), encoding="utf-8")
    check("一个都没有 → (0, 0)", sc.nudge_armed(), (0, 0))

    _nsp.write_text(json.dumps({"sessions": {"g1": "不是字典"}}), encoding="utf-8")
    check("脏数据不崩，且不算武装", sc.nudge_armed(), (0, 1))
finally:
    sc.BASE_DIR = _rb2

print("\n=== 20. 群触发状态（「群里 @ 她不回话」的真凶）===")
# 这条守的是 2026-09-25 用户绕了两圈的那个开关：trigger.group_at 一关，
# 群里 @ 她不回（只有正文含关键词才理），外部表现和"机器人挂了"一模一样，
# 而且会连带把群暖场一起掐死（@ 不回 → engaged 置不上）。
_orig_cfgsec = sc.cfg_section
_fake_trig = {}
sc.cfg_section = lambda name: (_fake_trig if name == "trigger" else _orig_cfgsec(name))
try:
    _fake_trig = {"group_at": True, "group_keywords": ["小柚", "猫娘"]}
    st, lines = sc.group_trigger_status()
    check("开着 → ok", st, "ok")
    check_true("开着时提到关键词", "小柚" in lines[0] and "猫娘" in lines[0], lines[0])

    _fake_trig = {"group_at": False, "group_keywords": ["小柚", "猫娘"]}
    st, lines = sc.group_trigger_status()
    check("关着 → bad", st, "bad")
    check_true("关着时明说「@ 不会回话」", "不会" in lines[0], lines[0])
    check_true("关着时给出恢复办法", any("设置页" in l for l in lines), lines)
    check_true("关着时说明只有关键词才理",
               any("小柚" in l for l in lines), lines)

    # 关键词也没设 = 群里完全不说话，这个更严重，要说出来
    _fake_trig = {"group_at": False, "group_keywords": []}
    st, lines = sc.group_trigger_status()
    check("关着且没关键词 → 仍报 bad", st, "bad")
    check_true("关着且没关键词时点明「完全不说话」",
               any("完全不说话" in l for l in lines), lines)

    # 字段缺失时按"开"处理（和 bot.py 的默认值一致，别把用户吓一跳）
    _fake_trig = {}
    check("字段缺失 → 按默认（开）处理", sc.group_trigger_status()[0], "ok")
finally:
    sc.cfg_section = _orig_cfgsec


# ---------- 反检测开关 napcat_bypass_state() ----------
# 2026-09-26 的坑：config/napcat.json 里六个 bypass 全被写成 false，
# 等于把 NapCat 的反检测伪装整个关掉 —— 进程在、端口在、消息照回，
# 唯一的症状是「被踢间隔一路缩短」。所以要有个东西天天盯着它。
print("\n[反检测开关]")

import tempfile  # noqa: E402

_BASE_KEEP = sc.BASE_DIR


def _mk_cfg(files):
    """造一个假的 NapCat 配置目录，返回它的 BASE_DIR。"""
    d = Path(tempfile.mkdtemp(prefix="_byp_"))
    c = d / "napcat" / "napcat" / "config"
    c.mkdir(parents=True)
    for name, txt in files.items():
        (c / name).write_text(txt, encoding="utf-8")
    return d


def _bypass_json(on: bool, only=None):
    keys = only if only is not None else sc.BYPASS_KEYS
    body = ", ".join('"%s": %s' % (k, "true" if on else "false") for k in keys)
    return '{"bypass": {%s}}' % body


check("六个开关的名单与 napcat.mjs 一致",
      tuple(sc.BYPASS_KEYS),
      ("hook", "window", "module", "process", "container", "js"))

try:
    sc.BASE_DIR = _mk_cfg({"napcat.json": _bypass_json(True)})
    check("六项全 true → on", sc.napcat_bypass_state(), ("on", []))

    sc.BASE_DIR = _mk_cfg({"napcat.json": _bypass_json(False)})
    st, off = sc.napcat_bypass_state()
    check("六项全 false → off", st, "off")
    check("全 false 时六个键都点名", off, list(sc.BYPASS_KEYS))

    sc.BASE_DIR = _mk_cfg({"napcat.json": '{"bypass": {"hook": false, "js": false}}'})
    check("只关两个 → 只报这两个", sc.napcat_bypass_state(), ("off", ["hook", "js"]))

    # 一行真实踩到的情形：全局那份修好了，但另一个账号的那份还全是 false
    sc.BASE_DIR = _mk_cfg({
        "napcat.json": _bypass_json(True),
        "napcat_10001.json": _bypass_json(False),
    })
    st, off = sc.napcat_bypass_state()
    check("一份开一份关 → 仍报 off（漏一份就等于漏）", st, "off")
    check("并且点名是哪些键", off, list(sc.BYPASS_KEYS))

    # 缺 bypass 段：交给 native 默认，这里不做判断（别瞎猜）
    sc.BASE_DIR = _mk_cfg({"napcat.json": '{"o3HookMode": 0}'})
    check("没有 bypass 段 → absent（不猜）", sc.napcat_bypass_state(), ("absent", []))

    # 坏 JSON 不能抛异常 —— 状态页是要在故障时用的，自己先炸就没意义了
    sc.BASE_DIR = _mk_cfg({"napcat.json": "{ 这不是 json"})
    check("坏 JSON → unreadable，不抛异常", sc.napcat_bypass_state(), ("unreadable", []))

    sc.BASE_DIR = Path(tempfile.mkdtemp(prefix="_byp_none_"))
    check("目录都没有 → unreadable", sc.napcat_bypass_state(), ("unreadable", []))

    # packetBackend 那份跟 bypass 无关，放个 false 进去也不该被算进来
    sc.BASE_DIR = _mk_cfg({
        "napcat.json": _bypass_json(True),
        "napcat_protocol_1234567890.json": '{"bypass": {"hook": false}}',
    })
    check("napcat_protocol_*.json 被跳过", sc.napcat_bypass_state(), ("on", []))
finally:
    sc.BASE_DIR = _BASE_KEEP

# 最要紧的一条：盯住**线上那份真配置**，别再被谁改成 false。
# 这条红了不是测试的问题 —— 是机器人要开始变卡了。
#
# ⚠️ 只在配置已生成时才断言：刚克隆下来还没放 NapCat 的话，
# `napcat_bypass_state()` 返回 unreadable，那是"还没装"而不是"被改坏了"，
# 报红只会误导人。用 unreadable 当跳过信号。
_real_state, _real_off = sc.napcat_bypass_state()
if _real_state == "unreadable":
    print("  [--]   跳过线上配置哨兵（NapCat 配置还没生成，属正常）")
else:
    check("★ 线上 NapCat 的反检测伪装是开着的", _real_state, "on")
    check("★ 线上没有被关掉的开关", _real_off, [])
    if sc.napcat_hook_mode() is not None:
        check("★ 线上 o3HookMode 是 0（防掉线建议值）", sc.napcat_hook_mode(), 0)

# ── 被踢间隔统计（_kick_stats）：支撑状态页的「稳定性趋势」那一段 ──
# 这段逻辑看着简单，实际连踩两坑：① gaps 排序后取 [-3:] 拿到的不是"最近三段"
# 而是"最大的三段"；② 用整体中位当基准会被最近的短命段一起拖低，恒判 flat。
# 两个坑都是靠下面这些用例抓出来的 —— 所以它们必须留在测试里。
_H = 3600.0
_NOW = 1_000_000.0


def _kicks(offsets_h):
    return sc._kick_stats([_NOW - o * _H for o in offsets_h], _NOW)


check("空输入：无存活、无间隔、趋势未知", _kicks([]),
      {"gaps_h": [], "uptime_h": None, "median_h": None,
       "recent_h": None, "base_h": None, "trend": "unknown"})

_r = _kicks([3.0])
check("单次被踢：算得出存活时长", round(_r["uptime_h"], 2), 3.0)
check("单次被踢：没有间隔也没趋势", (_r["gaps_h"], _r["median_h"], _r["trend"]),
      ([], None, "unknown"))

# 同一轮抖动会在几秒内重复记多条被踢（Worker 重启会再报一次），必须归并成一次
_r = _kicks([5.0, 5.0 - 10 / 3600.0])
check("同轮抖动被归并：只剩一段存活、零个间隔", (_r["gaps_h"], round(_r["uptime_h"], 2)),
      ([], 5.0))

_r = _kicks([20, 15, 10, 8.9, 7.9, 6.9])
check("间隔缩短 → worsening", _r["trend"], "worsening")
check("近期基准取的是最近三段（1.00h）", round(_r["recent_h"], 2), 1.0)
check("历史基准取的是更早的段（5.00h）", round(_r["base_h"], 2), 5.0)

_r = _kicks([30, 29, 28, 20, 12, 4])
check("间隔拉长 → improving", _r["trend"], "improving")

_r = _kicks([24, 19, 14, 9, 4])       # 间隔恒为 5h：这才是真的"持平"
check("变化不大 → flat", _r["trend"], "flat")

# 间隔必须按时间序对外给出最值和样本数（面板要显示"最短/最长/样本 N 段"）
_r = _kicks([24, 19, 14, 9, 4])
check("间隔样本数正确", len(_r["gaps_h"]), 4)
check("间隔已升序排列", _r["gaps_h"], sorted(_r["gaps_h"]))

# 真实线上样本（2026-09-24 ~ 09-26 诊断期）：就在最后一次被踢的**当时**看，
# 一路恶化。这里 now 取 5.51h 前最后那条被踢刚过 1 分钟 —— 还原故障发生时的现场。
# （若拿"现在的时刻"去喂这段历史，uptime 会变成 5.5h，那已经是"后来变好了"的视角，
#   判 improving 也合理 —— 但那不是这条用例要测的东西。）
_r = sc._kick_stats(
    [_NOW - o * _H for o in [33.42, 32.42, 29.39, 26.47, 21.25, 14.66, 8.47, 7.17, 6.46, 5.51]],
    _NOW - 5.51 * _H + 60)
check("真实故障样本（故障当时）被判为 worsening", _r["trend"], "worsening")

# ★ 最要紧的一条：当前存活显著长于历史时，**必须**判好转。
# 少了它，面板会在"已经 18.9 小时没被踢"的时候报「[!!] 恶化」——
# 只因为历史那几段在缩短。2026-09-27 实测真被自己坑了一次。
#
# 构造：历史间隔一路在**缩短**（10h→8h→2h→2h），若只看历史必然判 worsening；
# 但当前已经 36h 没被踢（最后一条被踢在 36h 前）—— 这个事实必须翻成 improving。
_shrink = [60, 50, 42, 40, 38, 36]              # 间隔 10,8,2,2,2（越近越短）
_r = sc._kick_stats([_NOW - o * _H for o in _shrink], _NOW)
check("画面：历史在缩短但当前已存活 36h", round(_r["uptime_h"], 1), 36.0)
check("存活远长于历史 → 判好转（不看存活会报恶化）", _r["trend"], "improving")

# 反面：把「现在」挪到刚被踢完（uptime≈1 分钟），同一段历史就该如实报恶化
_r = sc._kick_stats([_NOW - o * _H for o in _shrink], _NOW - 36 * _H + 60)
check("刚被踢完时如实报恶化", _r["trend"], "worsening")

# 反面：当前存活**也很短**时不许误判成好转
_r = sc._kick_stats([_NOW - o * _H for o in [40, 20, 15, 10, 5]], _NOW - 0.5 * _H)
check("当前只活了 0.5h → 不算好转", _r["trend"], "worsening")

# _parse_login_log 必须把统计量一起带出来，否则面板拿不到
_dg = sc._parse_login_log(
    "09-26 13:27:00 [error] [KickedOffLine] 你的账号当前登录已失效，请重新登录。\n"
    "09-26 14:44:48 [error] [KickedOffLine] 你的账号当前登录已失效，请重新登录。\n",
    now=_NOW)
check("登录诊断带出被踢间隔字段", "kick_gaps_h" in _dg and "kick_trend" in _dg, True)
check("登录诊断带出当前存活字段", "uptime_h" in _dg, True)

print("\n" + "=" * 50)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 50)
sys.exit(0 if FAIL == 0 else 1)
