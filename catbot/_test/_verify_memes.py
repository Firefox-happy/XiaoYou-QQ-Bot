# -*- coding: utf-8 -*-
"""验收：① 热梗工具（搜索版） ② memes.md 有没有真的进人设。"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

OUT = HERE / "_memes_result.txt"
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    lines.append(s)
    print(s)


log("=" * 62)
log("① 热梗工具（get_trending_memes）")
from tools import ToolRegistry  # noqa: E402

reg = ToolRegistry({"tools": {"enabled": True, "timeout": 12}})
names = [t["function"]["name"] for t in reg.schemas()]
log("  已注册工具:", names)
assert "get_trending_memes" in names, "工具没注册上！"
log()

log("  --- 不带 keyword：应返回「最近流行的梗」而不是新闻 ---")
out = reg.call("get_trending_memes", "{}", {})
for ln in out.split("\n")[:14]:
    log("  " + ln)
log("  （共 %d 字，含『梗』%d 次）" % (len(out), out.count("梗")))
NEWS = ["主席", "总统", "外交部", "特朗普", "拜登", "地震", "货车", "遇难",
        "讣告", "去世", "拘留", "宣判"]
hit = [w for w in NEWS if w in out]
log("  新闻类词命中:", hit if hit else "无 ✓")
log()

log("  --- 带 keyword：查具体梗 ---")
for kw in ("真嘟假嘟", "主打一个陪伴"):
    o = reg.call("get_trending_memes", '{"keyword": "%s"}' % kw, {})
    ok = ("真的假的" in o or "谐音" in o) if kw == "真嘟假嘟" else ("陪伴" in o)
    log("  %s %s → %s（%d 字）" % ("✓" if ok else "✗", kw, "查到释义" if ok else "没查到", len(o)))
log()

log("  --- 参数写坏不应崩 ---")
for bad in ("{", '{"keyword": 123}', "null", "", None):
    try:
        r = reg.call("get_trending_memes", bad, {})
        log("  args=%-20r -> %s" % (bad, (r[:36] + "...") if len(r) > 36 else r))
    except Exception as e:
        log("  args=%-20r -> 抛异常 %s ← 不该发生" % (bad, type(e).__name__))
log()

log("=" * 62)
log("② 人设是否带上了 memes.md")
try:
    import bot  # noqa: E402
    p = bot.Persona(ROOT / "persona.md", extras=[ROOT / "memes.md"])
    t = p.text
    log("  人设总字数:", len(t))
    for probe in ("真嘟假嘟", "主打一个陪伴", "我要验牌", "已读不回", "背手负鼠",
                  "全部放开", "这些情况下别用梗", "小柚的梗库",
                  "滚啦", "神经病啊", "卧槽"):
        log("    含 %-12s : %s" % (probe, "✓" if probe in t else "✗ 缺失"))
    log("  persona 主体还在:", "✓" if "绝对遵守" in t else "✗")
    log("  拼接顺序（人设在前）:",
        "✓" if t.index("绝对遵守") < t.index("小柚的梗库") else "✗")
    log("  热重载字段存在:", "✓" if hasattr(p, "_stamps") else "✗")
except Exception as e:
    import traceback
    log("  FAIL:", e)
    log(traceback.format_exc())
log()

log("=" * 62)
log("③ 「放开说话」是否两处都写了（persona + 系统提示）")
try:
    import bot as b2
    import inspect
    src = inspect.getsource(b2.CatBot._system_prompt)
    log("  系统提示含「放开说话」:", "✓" if "放开说话" in src else "✗")
    log("  系统提示含四样红线  :", "✓" if "只有四样绝对不碰" in src else "✗")
    tg = b2._TOOL_GUIDE
    log("  工具导引同步了红线  :", "✓" if "露骨" in tg and "真实歧视仇恨" in tg else "✗")
    ptext = (ROOT / "persona.md").read_text(encoding="utf-8")
    log("  persona 含「全部放开」:", "✓" if "全部放开" in ptext else "✗")
    log("  persona 含四样红线   :", "✓" if "只有这四样绝对不碰" in ptext else "✗")
except Exception as e:
    log("  FAIL:", e)

OUT.write_text("\n".join(lines), encoding="utf-8")
print("\nwritten", OUT)
