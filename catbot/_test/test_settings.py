# -*- coding: utf-8 -*-
"""设置页自测：schema 一致性、校验、写回保真、脱敏、HTTP 端到端。

原则：**所有写操作都在临时副本上做**，绝不碰真实的 config.json。
"""
import json
import os
import re
import ast
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)

CATBOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CATBOT))

import settings_page as sp  # noqa: E402

OUT = Path(__file__).resolve().parent / "settings_out.txt"
REAL_CONFIG = CATBOT / "config.json"

lines = []
passed = [0]
failed = [0]


def log(x=""):
    lines.append(str(x))


def ck(name, cond, extra=""):
    if cond:
        passed[0] += 1
        log("  [PASS] " + name)
    else:
        failed[0] += 1
        log("  [FAIL] " + name + ("  <- " + str(extra) if extra else ""))


MISSING = object()


def gp(d, path):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return MISSING
        cur = cur[p]
    return cur


# ==========================================================================
log("=" * 64)
log("  设置页自测")
log("=" * 64)

# --- 1. schema 自检 ---
log()
log("[1] schema 结构")
paths = [it["path"] for it in sp.SCHEMA]
ck("路径无重复", len(paths) == len(set(paths)))
ck("项数 > 50", len(sp.SCHEMA) > 50, len(sp.SCHEMA))
gids = {g["id"] for g in sp.GROUPS}
bad_group = [it["path"] for it in sp.SCHEMA if it["group"] not in gids]
ck("每项都归属一个存在的分组", not bad_group, bad_group)
TYPES = {"bool", "int", "float", "text", "secret", "select", "time", "list", "pair", "timerange"}
bad_type = [it["path"] for it in sp.SCHEMA if it["type"] not in TYPES]
ck("类型都合法", not bad_type, bad_type)
ck("每个分组至少有一项",
   all(any(it["group"] == g["id"] for it in sp.SCHEMA) for g in sp.GROUPS))
ck("bool 项没写 min/max",
   not [it["path"] for it in sp.SCHEMA if it["type"] == "bool" and ("min" in it or "max" in it)])

# 2026-09-24 新增：自愈（改完自动生效 / 卡死自动重启）
# 这一组是给 daemon.py 读的，但它同样得在表上有名有姓、默认值和 bot.py 对得上。
ck("自动重启分组已注册", "watch" in gids, sorted(gids))
ck("自愈配置项齐全",
   sorted(p for p in paths if p.startswith(("watch.", "health."))) == [
       "health.busy_sec", "health.enabled", "health.grace_sec",
       "health.hb_stale_sec", "health.interval_sec", "health.max_per_hour",
       "health.ws_down_sec", "watch.cooldown_sec", "watch.enabled",
       "watch.interval_sec", "watch.settle_sec"],
   sorted(p for p in paths if p.startswith(("watch.", "health."))))
ck("两个总开关都是 bool",
   [sp.SCHEMA_BY_PATH["watch.enabled"]["type"],
    sp.SCHEMA_BY_PATH["health.enabled"]["type"]], ["bool", "bool"])

# --- 2. 默认值与 bot.py 的 DEFAULT_CONFIG 一致 ---
log()
log("[2] 默认值一致性（防止 schema 与代码漂移）")
src = (CATBOT / "bot.py").read_text(encoding="utf-8")
m = re.search(r"^DEFAULT_CONFIG\s*=\s*(\{.*?^\})\s*$", src, re.S | re.M)
ck("能从 bot.py 抽到 DEFAULT_CONFIG", bool(m))
DC = ast.literal_eval(m.group(1)) if m else {}
ck("sp.defaults() 真的抽到了（不是走兜底）", sp.defaults() == DC)
drift = []
for it in sp.SCHEMA:
    v = gp(DC, it["path"])
    if v is MISSING:
        continue                      # 代码里没有的项（如 task_max_chars）跳过
    if v != it["default"]:
        drift.append("%s: schema=%r bot=%r" % (it["path"], it["default"], v))
ck("schema 默认值 == bot.DEFAULT_CONFIG", not drift, drift)

# --- 3. coerce 校验 ---
log()
log("[3] 取值校验")
I = sp.SCHEMA_BY_PATH
cases = [
    ("llm.temperature", 0.9, 0.9, None),
    ("llm.temperature", 0.863, 0.85, None),          # 归到 step 网格
    ("llm.temperature", 5, None, "不能大于"),
    ("llm.temperature", -1, None, "不能小于"),
    ("llm.temperature", "abc", None, "数字"),
    ("llm.max_tokens", 220, 220, None),
    ("llm.max_tokens", 220.5, None, "整数"),
    ("llm.max_tokens", 20, None, "不能小于"),
    ("trigger.private", True, True, None),
    ("trigger.private", "false", False, None),
    ("trigger.private", "随便", None, "开或关"),
    ("llm.provider", "openai", "openai", None),
    ("llm.provider", "gemini", None, "只能选"),
    ("proactive.greeting_time", "08:30", "08:30", None),
    ("proactive.greeting_time", "25:00", None, "HH:MM"),
    ("proactive.greeting_time", "8:30", None, "HH:MM"),
    ("trigger.group_keywords", ["a", "b"], ["a", "b"], None),
    ("trigger.group_keywords", "a\nb\n a \n", ["a", "b"], None),   # 去空去重
    ("whitelist.groups", [], [], None),
    ("reply.typing_delay", [1.0, 2.0], [1.0, 2.0], None),
    ("reply.typing_delay", [3, 1], None, "不能大于"),
    ("reply.typing_delay", [1], None, "两个值"),
    ("reply.typing_delay", [0.1, 2], None, "不能小于"),
    ("proactive.nudge.quiet_hours", ["23:30", "09:00"], ["23:30", "09:00"], None),
    ("proactive.nudge.quiet_hours", ["23:30", "9:00"], None, "HH:MM"),
    ("llm.api_key", "", sp._SKIP, None),             # 空 = 不改
    ("llm.api_key", None, sp._SKIP, None),
    # --- 语音：Edge TTS 那一组 ---
    ("voice.tts_provider", "edge", "edge", None),
    ("voice.tts_provider", "auto", "auto", None),
    ("voice.tts_provider", "sapi", "sapi", None),
    ("voice.tts_provider", "azure", None, "只能选"),
    ("voice.edge_voice", "zh-CN-XiaoyiNeural", "zh-CN-XiaoyiNeural", None),
    ("voice.edge_voice", "zh-CN-YunjianNeural", "zh-CN-YunjianNeural", None),
    ("voice.edge_voice", "en-US-AriaNeural", None, "只能选"),
    ("voice.edge_rate", 20, 20, None),
    ("voice.edge_rate", 999, None, "不能大于"),
    ("voice.edge_rate", -999, None, "不能小于"),
    ("voice.edge_pitch", 10, 10, None),
    ("voice.edge_pitch", 100, None, "不能大于"),
    ("voice.edge_volume", 0, 0, None),
    # --- 梗库文件 ---
    ("memes_file", "memes.md", "memes.md", None),
]
for path, raw, want, errkw in cases:
    try:
        got = sp.coerce(I[path], raw)
    except ValueError as e:
        if errkw and errkw in str(e):
            ck("%s <- %r 被拒(%s)" % (path, raw, errkw), True)
        else:
            ck("%s <- %r 应通过或被拒错因" % (path, raw), False, "抛了 %s" % e)
        continue
    if errkw:
        ck("%s <- %r 应当被拒" % (path, raw), False, "却通过了 -> %r" % (got,))
    else:
        ck("%s <- %r -> %r" % (path, raw, got), got == want, "want %r" % (want,))

# --- 4. 写回保真（在临时副本上） ---
log()
log("[4] 写回保真（临时副本，不碰真实配置）")
tmpdir = Path(tempfile.mkdtemp(prefix="xy_settings_"))
work = tmpdir / "config.json"
shutil.copy2(REAL_CONFIG, work)
sp.CONFIG_FILE = work                                   # 重定向到副本
orig = json.loads(work.read_text(encoding="utf-8"))
orig_keys = list(orig.keys())

# 掺一个"用户自己写的、schema 里没有的"字段
orig2 = dict(orig)
orig2["_my_note"] = {"author": "me", "why": "手写的"}
work.write_text(json.dumps(orig2, ensure_ascii=False, indent=2), encoding="utf-8")

saved, errs = sp.apply_changes({"llm.temperature": 0.5, "reply.max_chars": 200})
ck("改动被接受", saved == ["llm.temperature", "reply.max_chars"], (saved, errs))
after = json.loads(work.read_text(encoding="utf-8"))
ck("temperature 写进去了", gp(after, "llm.temperature") == 0.5, gp(after, "llm.temperature"))
ck("max_chars 写进去了", gp(after, "reply.max_chars") == 200)
ck("顶层的键顺序没被打乱", list(after.keys()) == orig_keys + ["_my_note"], list(after.keys()))
ck("未知的自定义字段还在", gp(after, "_my_note.author") == "me")
ck("嵌套结构没被拍平", isinstance(gp(after, "llm"), dict) and
   len(gp(after, "llm")) == len(orig2["llm"]),
   len(gp(after, "llm")) if isinstance(gp(after, "llm"), dict) else gp(after, "llm"))

# 只改一项时，其它项必须逐字节等价
before = json.loads(work.read_text(encoding="utf-8"))
sp.apply_changes({"llm.model": "deepseek-chat"})
after2 = json.loads(work.read_text(encoding="utf-8"))
diff = [k for k in set(list(before.keys()) + list(after2.keys())) if before.get(k) != after2.get(k)]
ck("只改 llm 段，其它顶层段完全没动", diff == ["llm"], diff)
ck("llm 段内只动了 model",
   before["llm"]["model"] != after2["llm"]["model"] and
   {k: v for k, v in before["llm"].items() if k != "model"} ==
   {k: v for k, v in after2["llm"].items() if k != "model"})

# 空改动 / 全非法改动 不应破坏文件
snap = work.read_text(encoding="utf-8")
s2, e2 = sp.apply_changes({})
ck("空改动不写盘", s2 == [] and not e2 and work.read_text(encoding="utf-8") == snap)
s3, e3 = sp.apply_changes({"llm.temperature": 999, "不存在.的.字段": 1})
ck("非法改动整体拒绝（不写半截）", s3 == [] and len(e3) == 2, e3)
ck("被拒后文件原样", work.read_text(encoding="utf-8") == snap)
ck("非法路径的报错可读", "不是可配置项" in e3.get("不存在.的.字段", ""), e3)

# 路径注入：想借 schema 之外的路由改别的文件
s4, e4 = sp.apply_changes({"../../evil": 1, "napcat.ws_url; rm": 1})
ck("可疑路径被拒", s4 == [] and len(e4) == 2, e4)

# 密钥：空值不清空原值
sp.apply_changes({"llm.api_key": "sk-realkey-1234567890"})
ck("密钥写入成功", gp(json.loads(work.read_text(encoding="utf-8")), "llm.api_key") ==
   "sk-realkey-1234567890")
sp.apply_changes({"llm.api_key": ""})
ck("提交空密钥 = 保持原样（不清空）",
   gp(json.loads(work.read_text(encoding="utf-8")), "llm.api_key") == "sk-realkey-1234567890")

# 原子性：不该留下 .tmp
ck("没残留临时文件", not list(tmpdir.glob("*.tmp")), list(tmpdir.glob("*.tmp")))

# --- 5. 脱敏 ---
log()
log("[5] 密钥脱敏")
key = "sk-test0000000000000000000000000000"   # 假的，只为试脱敏逻辑
masked = sp.mask_secret(key)
ck("脱敏后不含完整密钥", key not in masked, masked)
ck("保留了头尾可辨认", masked.startswith("sk-") and masked.endswith(key[-4:]), masked)
ck("空值返回空", sp.mask_secret("") == "" and sp.mask_secret(None) == "")

# --- 6. 页面渲染 ---
log()
log("[6] 页面渲染")
real_status = sp.build_status
sp.build_status = lambda: {"items": []}                 # 不真去探端口，快
try:
    html = sp.render_page(snapshot=True)
finally:
    sp.build_status = real_status
ck("占位符全部替换", "__SCHEMA_JSON__" not in html and "__TOKEN__" not in html and
   "__VALUES_JSON__" not in html)
ck("静态快照里没有明文密钥", key not in html and "sk-realkey-1234567890" not in html)
ck("快照标记为 preview", "const SNAPSHOT = true" in html)
ck("快照里 token 为空", 'const TOKEN    = ""' in html or 'const TOKEN = ""' in html or 'const TOKEN = ""' in html)
ck("页面里有设置项", "llm.temperature" in html and "冷场暖场" in html)
ck("script 没被配置内容截断", html.count("</script>") == 1, html.count("</script>"))
ck("html 结尾完整", html.rstrip().endswith("</html>"))

# --- 7. HTTP 端到端 ---
log()
log("[7] HTTP 端到端（真起一个服务，本机随机端口）")
sp.Handler.token = "TESTTOKEN"
from http.server import ThreadingHTTPServer  # noqa: E402
srv = ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
srv.daemon_threads = True
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port


def req(path, method="GET", body=None, token="TESTTOKEN", host=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    if token:
        r.add_header("X-Token", token)
    if data:
        r.add_header("Content-Type", "application/json")
    if host:
        r.add_header("Host", host)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


st, data = req("/api/config")
ck("GET /api/config 200", st == 200 and data.get("ok"), (st, data))
ck("返回了 values", isinstance(data.get("values"), dict) and gp(data["values"], "llm.model"))
ck("密钥字段不回传明文", gp(data["values"], "llm.api_key") is None,
   gp(data["values"], "llm.api_key"))
ck("给了脱敏提示", data["hints"].get("llm.api_key", "").startswith("sk-") and
   key not in json.dumps(data["hints"], ensure_ascii=False), data["hints"])

st, data = req("/api/status")
ck("GET /api/status 可用", st == 200 and "items" in data, st)

st, data = req("/api/save", "POST", {"changes": {"reply.max_chars": 210}})
ck("POST /api/save 成功", st == 200 and data.get("ok") and data["saved"] == ["reply.max_chars"],
   (st, data))
ck("写回落到磁盘",
   gp(json.loads(work.read_text(encoding="utf-8")), "reply.max_chars") == 210)

st, data = req("/api/save", "POST", {"changes": {"reply.max_chars": 99999}})
ck("越界值被服务端拒绝", st == 200 and not data.get("ok") and data["errors"], (st, data))

st, data = req("/api/reveal", "POST", {"path": "llm.api_key"})
ck("POST /api/reveal 能拿回真值（本机+令牌）",
   st == 200 and data.get("value") == "sk-realkey-1234567890", (st, data))
st, data = req("/api/reveal", "POST", {"path": "llm.temperature"})
ck("reveal 只认密钥项", st == 400, st)

st, data = req("/api/config", token="WRONG")
ck("错误令牌被拒（403）", st == 403, st)
st, data = req("/api/config", token=None)
ck("没令牌被拒（403）", st == 403, st)

st, data = req("/api/service", "POST", {"action": "rm -rf"})
ck("未知服务动作被拒", st == 400, st)

# Host 校验：模拟 DNS rebinding（Host 不是本机）
conn = HTTPConnection("127.0.0.1", port, timeout=10)
conn.request("GET", "/api/config", headers={"Host": "evil.example.com", "X-Token": "TESTTOKEN"})
resp = conn.getresponse()
resp.read()
ck("伪造 Host 被拒（403）", resp.status == 403, resp.status)
conn.close()

# 无令牌直接开首页 -> 友好的提示页，而不是白屏
try:
    urllib.request.urlopen(base + "/", timeout=10)
    ck("无令牌首页返回 403 页", False, "竟然 200")
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8")
    ck("无令牌首页返回 403 页", e.code == 403 and "设置页.bat" in body, e.code)

with urllib.request.urlopen(base + "/?t=TESTTOKEN", timeout=20) as resp:
    page = resp.read().decode("utf-8")
ck("带令牌首页正常返回", resp.status == 200 and "设置台" in page)
ck("首页注入了真 token", "TESTTOKEN" in page)
ck("首页不含明文密钥", key not in page)

srv.shutdown()
sp.Handler.token = ""

# --- 收尾：确认真实配置没被碰过 ---
log()
log("[8] 真实配置未被污染")
ck("真实 config.json 仍是安全的（未被测试写入）",
   "sk-realkey-1234567890" not in REAL_CONFIG.read_text(encoding="utf-8"))
shutil.rmtree(tmpdir, ignore_errors=True)

log()
log("=" * 64)
log("  通过 %d 项，失败 %d 项" % (passed[0], failed[0]))
log("=" * 64)

OUT.write_text("\n".join(lines), encoding="utf-8")
sys.exit(1 if failed[0] else 0)
