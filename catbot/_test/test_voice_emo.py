# -*- coding: utf-8 -*-
"""
情绪语音引擎（voice_emo）单元测试 —— 纯函数，不联网。

来历：2026-09-25 主人问"怎么让她的语音有情绪"。实测发现 Edge TTS 免费
端点一次请求只认一组 rate/pitch/volume（<mstts:express-as>、<break>、
<emphasis>、多段 <prosody> 全被拒），所以起伏必须在客户端分段做。
这个文件盯住那条链里最容易写错的三件事：

  1. **不许超速** —— shape 系数只能衰减，不能放大（曾把"兴奋"的
     pitch 顶到 +56Hz，念出来像电子玩具）。
  2. **切句不能丢标点** —— 标点就是停顿，丢了就变成机器念稿。
  3. **判情绪不能崩** —— 启发式再粗糙也不能抛异常，否则语音整条哑掉。

运行:
    python _test/test_voice_emo.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import voice_emo as E  # noqa: E402

PASS = FAIL = 0


def ck(name, got, expect):
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望: {expect!r}\n         实际: {got!r}")


def ck_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("\n=== 1. clamp / 取情绪 ===")
ck("clamp 夹上限", E.clamp(150), 100)
ck("clamp 夹下限", E.clamp(-150), -100)
ck("clamp 不动中间值", E.clamp(37), 37)
ck_true("有 10 档情绪", len(E.emotion_keys()) == 10, E.emotion_keys())
for k in E.emotion_keys():
    ck_true(f"get('{k}') 能取到且名字非空", bool(E.get(k).get("name")), E.get(k))
for bad in ("", None, "不存在", "NORMAL ", 123, "   "):
    ck_true(f"get({bad!r}) 兜住不崩", E.get(bad).get("name") is not None, E.get(bad))
ck("大小写/空格容错", E.get("  HAPPY ").get("name"), "开心")

print("\n=== 2. 判情绪：关键词 ===")
cases = [
    ("呜呜，对不起嘛…别不理我", "sad"),
    ("抱抱我好不好嘛～", "cute"),
    ("好耶！太棒了！！", "excited"),
    ("哼！讨厌你，坏蛋", "angry"),
    ("才、才不是特意等你的呢", "shy"),
    ("诶？！真的吗，不会吧", "surprised"),
    ("嘿嘿，今天超开心的", "happy"),
    ("早点睡吧，晚安乖乖", "soft"),
    ("看吧，我早就说过了", "smug"),
]
for text, want in cases:
    ck(f"{text[:14]!r} → {want}", E.detect(text), want)

print("\n=== 3. 判情绪：没有关键词时靠标点猜气势 ===")
ck("两个感叹号 → 兴奋", E.detect("你回来啦！！"), "excited")
ck("一串问号 → 惊讶", E.detect("真的吗？？？"), "surprised")
ck("问号+感叹号 → 惊讶", E.detect("什么？！"), "surprised")
ck("一串省略号 → 委屈", E.detect("你走了……"), "sad")
ck("省略号+问号 → 委屈", E.detect("你不理我了…？"), "sad")
ck("单感叹短句 → 开心", E.detect("太好了！"), "happy")
ck("波浪号 → 撒娇", E.detect("今天天气真好～"), "cute")
ck("平淡陈述 → 平常", E.detect("今天星期三，下午有会。"), "normal")
for bad in ("", "   ", None, "。。。", "！！！", "\n", "…"):
    try:
        r = E.detect(bad)
        ck_true(f"detect({bad!r}) 返回合法情绪", r in E.EMOTIONS, r)
    except Exception as e:
        ck_true(f"detect({bad!r}) 不抛异常", False, type(e).__name__)

print("\n=== 4. 切句：标点必须留着（它就是停顿）===")
line = "主人！你终于回来啦，小柚等你等得，都快睡着了喵……"
parts = E.split_clauses(line, max_segments=4)
ck("切成 4 段", len(parts), 4)
ck("拼回去与原文一致", "".join(parts), line)
ck_true("每段都带尾部标点", all(p[-1] in "！，。…～" for p in parts), parts)
ck("空串 → 空列表", E.split_clauses(""), [])
ck("纯空白 → 空列表", E.split_clauses("   "), [])
ck("没有标点的长句 → 1 段", len(E.split_clauses("今天天气不错适合出去走走")), 1)
ck_true("太碎的片段被并进前一段",
        all(len(p) >= 3 for p in E.split_clauses("好，啊，嗯，走吧")),
        E.split_clauses("好，啊，嗯，走吧"))
ck_true("段数不超过上限（长句）",
        len(E.split_clauses("一，二，三，四，五，六，七，八，九，十，", max_segments=3)) <= 3,
        E.split_clauses("一，二，三，四，五，六，七，八，九，十，", max_segments=3))
ck_true("拼接后仍然是原文（合并过短片段时）",
        "".join(E.split_clauses("好，啊，嗯，走吧")) == "好，啊，嗯，走吧",
        E.split_clauses("好，啊，嗯，走吧"))

print("\n=== 5. 停顿长度 ===")
ck("句号后停 320ms", E.gap_after("回来啦。"), 320)
ck("感叹号后停 260ms", E.gap_after("主人！"), 260)
ck("逗号后停 150ms", E.gap_after("回来啦，"), 150)
ck("省略号后停 340ms", E.gap_after("喵……"), 340)
ck("波浪号后停 180ms", E.gap_after("好呀～"), 180)
ck("没标点 → 默认 160ms", E.gap_after("回来啦"), 160)
ck("空串不崩", E.gap_after(""), 160)

print("\n=== 6. 分段计划（contour）===")
p1 = E.contour(line, "excited", max_segments=1)
ck("单段只有 1 条", len(p1), 1)
ck("单段末尾不留白", p1[0]["gap_ms"], 0)
e = E.get("excited")
ck("单段=情绪的准确值 rate", p1[0]["rate"], e["rate"])
ck("单段=情绪的准确值 pitch", p1[0]["pitch"], e["pitch"])
ck("单段=情绪的准确值 volume", p1[0]["volume"], e["volume"])

p4 = E.contour(line, "excited", max_segments=4)
ck("多段：最后一段不留白", p4[-1]["gap_ms"], 0)
ck_true("多段：中间段都留白", all(s["gap_ms"] > 0 for s in p4[:-1]), p4)
ck_true("多段：文本拼回去还是原文", "".join(s["text"] for s in p4) == line, p4)
ck("空文本 → 空计划", E.contour("", "happy"), [])
ck_true("非法情绪名不会崩", len(E.contour("你好呀。", "不存在的情绪")) >= 1)

print("\n=== 7. 铁律：句内起伏只能衰减，绝不许超速 ===")
# 这是 2026-09-25 踩过的坑：shape 系数 >1 会把情绪放大到超出它的定义
for k in E.emotion_keys():
    sh = E.get(k)["shape"]
    ck_true(f"'{k}' 的三个系数都不超过 1.0", all(x <= 1.0 for x in sh), sh)
    ck_true(f"'{k}' 的系数都为正", all(x > 0 for x in sh), sh)

SAMPLE = "主人！你终于回来啦，小柚等你等得，都快睡着了喵……"
# 判据是"离基准的距离"，不是数值大小：
#   cute 的 rate 是 -8（比基准 8 更慢），衰减后 -3 —— 数值变大了，但
#   "比起基准的偏差"从 16 缩到 11，这才是"不超速"的真正含义。
#   当初把兴奋的 pitch 顶到 +56 时，偏差 46 > 满格 32，这条就会红。
for k in E.emotion_keys():
    e = E.get(k)
    for n in (1, 2, 3, 4):
        for s in E.contour(SAMPLE, k, max_segments=n):
            for dim in ("rate", "pitch", "volume"):
                full = abs(e[dim] - E.BASE[dim])
                used = abs(s[dim] - E.BASE[dim])
                ck_true(f"'{k}'/{n}段 {dim} 未超速（{used} ≤ {full}）",
                        used <= full, s)
            ck_true(f"'{k}'/{n}段 参数在合法区间",
                    -100 <= s["rate"] <= 100 and -100 <= s["pitch"] <= 100
                    and -100 <= s["volume"] <= 100, s)

print("\n=== 8. 弧线方向要对（撒娇尾巴最黏 / 兴奋开头最冲）===")
cute = E.contour(SAMPLE, "cute", max_segments=3)
ck("撒娇：尾巴音调最高", cute[-1]["pitch"], E.get("cute")["pitch"])
ck_true("撒娇：开头比尾巴低", cute[0]["pitch"] < cute[-1]["pitch"], cute)
ex = E.contour(SAMPLE, "excited", max_segments=3)
ck("兴奋：开头最快", ex[0]["rate"], E.get("excited")["rate"])
ck_true("兴奋：往后一路收", ex[0]["rate"] > ex[-1]["rate"], ex)
ang = E.contour(SAMPLE, "angry", max_segments=3)
ck_true("生气：尾巴音量收住", ang[-1]["volume"] < ang[0]["volume"], ang)
sad = E.contour(SAMPLE, "sad", max_segments=3)
ck_true("委屈：尾巴音量最小", sad[-1]["volume"] < sad[0]["volume"], sad)

print("\n=== 9. 一步到位 plan_for ===")
key, plan = E.plan_for("哼！讨厌你！")
ck("自动判成生气", key, "angry")
ck_true("计划非空", len(plan) >= 1, plan)
key2, plan2 = E.plan_for("随便说点什么", emotion_key="soft")
ck("指定情绪时以指定为准", key2, "soft")
ck("指定情绪的参数取自该档", plan2[0]["rate"], E.get("soft")["rate"])

print("\n=== 10. 给设置页的清单 ===")
rows = E.table_for_page()
ck("10 行", len(rows), 10)
ck_true("每行字段齐全",
        all({"key", "name", "rate", "pitch", "volume", "desc"} <= set(r) for r in rows), rows)
ck_true("键与情绪表一致", [r["key"] for r in rows] == E.emotion_keys())

print(f"\n{'=' * 50}")
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 50)
sys.exit(1 if FAIL else 0)
