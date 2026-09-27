# -*- coding: utf-8 -*-
"""热梗库自动更新自测 —— 不联网、不调模型、不碰真的 memes.md。

覆盖：
  1. 归一化与「已有梗名」抓取（加粗条目 + 平铺清单两种写法）
  2. 文件拆分 / 合并 / 标记损坏时的行为
  3. 去重：对手写区、对自动区、对自己
  4. 淘汰：只留最近 N 条，新的排在前面
  5. 清洗：缺字段 / 超长 / 敏感词 / markdown 记号
  6. 模型输出解析容错（代码块围栏、前后废话、非法 JSON、不是数组）
  7. update 端到端：**失败绝不动原文件**、dry_run 不落盘、正常写盘、幂等
  8. clamp_int 软着陆

运行:
    python _test/test_corpus.py
"""

import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import corpus  # noqa: E402

# corpus 用的 logger 叫 "xiaoyou"，平时由 bot.py 挂 handler。
# 测试单独 import 时它没有任何 handler，警告会被吞掉 —— 这里接一个出来，
# 一是方便看，二是顺便验证「敏感词被丢弃」这类路径确实喊了话。
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("      ~ %(message)s"))
_log = logging.getLogger("xiaoyou")
_log.addHandler(_h)
_log.setLevel(logging.WARNING)

PASS = FAIL = 0


def ck(name, got, expect):
    global PASS, FAIL
    if got == expect:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         期望: %r\n         实际: %r" % (name, expect, got))


def ck_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         说明: %s" % (name, detail))


TMP = Path(tempfile.mkdtemp(prefix="xiaoyou_corpus_"))

MANUAL = """# 小柚的梗库

> 这是小柚的冲浪笔记。

## 一、撒娇 · 亲昵

- **真嘟假嘟**【当季】——"真的假的"的软萌说法
  > 小柚：真嘟假嘟？

- **主打一个陪伴**【当季】——实力不够但重在在场

## 五、常青

绝了 / 破防了 / emo 了 / 绷不住了 / 抽象 / 上大分

## 七、什么算"过期了"

> 雨女无瓜 / 奥利给 / 给力 / 蓝瘦香菇 / 前方高能
"""


# ==========================================================================
print("\n=== 1. 归一化 ===")
# ==========================================================================
ck("去空白标点", corpus._norm("真嘟 假嘟！"), "真嘟假嘟")
ck("大小写归一", corpus._norm("EMO了"), "emo了")
ck("同一条的不同写法归一后相同",
   corpus._norm("你的胆子真是肥嘟嘟的啊"),
   corpus._norm("你的胆子真是肥嘟嘟的啊？"))

ck("清洗 markdown 记号", corpus._clean("**很新的梗**"), "很新的梗")
ck("清洗反引号与井号", corpus._clean("`#绝了#`"), "绝了")
ck("超长截断带省略号", corpus._clip("x" * 20, 5), "xxxxx…")
ck("不超长就原样", corpus._clip("abc", 5), "abc")


# ==========================================================================
print("\n=== 2. 抓「已出现过的梗名」 ===")
# ==========================================================================
names = corpus.existing_names(MANUAL)
ck_true("抓到了加粗条目", "真嘟假嘟" in names and "主打一个陪伴" in names, sorted(names))
ck_true("抓到了平铺清单里的", "绝了" in names and "破防了" in names, sorted(names))
ck_true("emo 了 也算（归一化后）", "emo了" in names, sorted(names))
ck_true("过期清单里的也抓（反正不许重复）", "奥利给" in names, sorted(names))
ck_true("没把标题当梗名", "小柚的梗库" not in names, sorted(names))


# ==========================================================================
print("\n=== 3. 拆分 / 合并 ===")
# ==========================================================================
head, body, tail, has = corpus._split(MANUAL)
ck("没有标记 → has=False", has, False)
ck("没有标记 → head 就是全文", head == MANUAL, True)

head, body, tail, has = corpus._split(MANUAL + "\n\n" +
                                      corpus.MARK_BEGIN + "\n- **X**【自动·2026-01-01】——y\n" +
                                      corpus.MARK_END + "\n")
ck("有标记 → has=True", has, True)
ck_true("body 切出来了", "X" in body, body)
ck_true("head 保留了手写内容", "真嘟假嘟" in head, head[:40])

# 只有 BEGIN 没有 END → 当作没有自动区（绝不猜，宁可重写一节）
broken = MANUAL + "\n" + corpus.MARK_BEGIN + "\n- **Y**【自动·2026-01-01】——z\n"
ck("标记损坏 → 当没有自动区", corpus._split(broken)[3], False)

NEW1 = [{"name": "新梗甲", "meaning": "说明甲", "example": "例句甲", "date": "2026-09-24"}]
NEW2 = [{"name": "新梗乙", "meaning": "说明乙", "example": "", "date": "2026-09-24"}]

text, added = corpus.merge(MANUAL, NEW1 + NEW2, 24)
ck("首次合并：新增 2 条", added, 2)
ck_true("加了标记", corpus.MARK_BEGIN in text and corpus.MARK_END in text, text[-200:])
ck_true("手写内容一字未动", text.startswith(MANUAL.rstrip()), text[:60])
ck_true("新梗写进去了", "新梗甲" in text and "新梗乙" in text, text[-200:])
ck_true("有第八节标题", corpus.SECTION_TITLE in text, text[-300:])

# 再合一次：老条目要留着，新的排前面
text2, added2 = corpus.merge(text, [{"name": "新梗丙", "meaning": "丙",
                                     "example": "", "date": "2026-09-25"}], 24)
ck("二次合并：新增 1 条", added2, 1)
ck_true("老条目还在", "新梗甲" in text2 and "新梗乙" in text2, text2[-300:])
ck_true("新条目排在老的前面",
        text2.index("新梗丙") < text2.index("新梗甲"), text2[-300:])
ck_true("手写区依旧没动", text2.startswith(MANUAL.rstrip()), text2[:60])


# ==========================================================================
print("\n=== 4. 去重 ===")
# ==========================================================================
_, added = corpus.merge(MANUAL, [{"name": "真嘟假嘟", "meaning": "重复", "example": "",
                                  "date": "2026-09-24"}], 24)
ck("和手写区重名 → 不收", added, 0)

_, added = corpus.merge(text, [{"name": "新梗甲", "meaning": "重复", "example": "",
                                "date": "2026-09-24"}], 24)
ck("和自动区重名 → 不收", added, 0)

_, added = corpus.merge(MANUAL, [
    {"name": "同一个梗", "meaning": "a", "example": "", "date": "2026-09-24"},
    {"name": "同一个梗！", "meaning": "b", "example": "", "date": "2026-09-24"},
], 24)
ck("自己内部重名（写法不同也算）→ 只留一个", added, 1)

_, added = corpus.merge(MANUAL, [{"name": "真嘟假嘟", "meaning": "x", "example": "",
                                  "date": "2026-09-24"}, NEW1[0]], 24)
ck("混合：一个重复一个新 → 只加 1", added, 1)


# ==========================================================================
print("\n=== 5. 淘汰 ===")
# ==========================================================================
many = [{"name": "梗%02d" % i, "meaning": "m", "example": "", "date": "2026-09-24"}
        for i in range(30)]
text3, added3 = corpus.merge(MANUAL, many, 10)
parsed = corpus._parse_items(corpus._split(text3)[1])
ck("自动区最多 10 条", len(parsed), 10)
ck("保留的是最新的 10 条", parsed[0]["name"], "梗00")
ck_true("被砍掉的是尾巴", all(("梗%02d" % i) not in text3 for i in range(10, 30)), "")

# 再加一条，最老的应该被挤出去
text4, _ = corpus.merge(text3, [{"name": "更新的", "meaning": "m", "example": "",
                                 "date": "2026-09-25"}], 10)
parsed4 = corpus._parse_items(corpus._split(text4)[1])
ck("还是 10 条", len(parsed4), 10)
ck("最新的在最前", parsed4[0]["name"], "更新的")
ck_true("原来的第 10 条被挤掉", "梗09" not in text4, "")

# 往返一致性：渲染出来还能被解析回去
rt = corpus._parse_items(corpus._render_items(parsed4))
ck("渲染→解析往返一致", [x["name"] for x in rt], [x["name"] for x in parsed4])
ck("例句也往返得住", rt[1].get("example"), "")


# ==========================================================================
print("\n=== 6. 清洗 ===")
# ==========================================================================
raw = [
    {"name": "好梗", "meaning": "ok", "example": "例"},
    {"name": "", "meaning": "缺名字"},
    {"name": "缺解释", "meaning": ""},
    "我不是字典",
    None,
    {"name": "x" * 40, "meaning": "名字太长"},
    {"name": "涉政梗", "meaning": "提到了六四这件事"},
    {"name": "正常名", "meaning": "解释里夹带 赌博 内容"},
    {"name": "**带记号**", "meaning": "# 也有"},
]
clean = corpus.sanitize(raw, "2026-09-24")
got = [c["name"] for c in clean]
ck("留下的条数", len(clean), 2)
ck("缺字段/非字典/超长/敏感 都被丢掉", got, ["好梗", "带记号"])
ck("markdown 记号被清掉", clean[1]["name"], "带记号")
ck("日期戳补上了", clean[0]["date"], "2026-09-24")

long_one = corpus.sanitize([{"name": "梗", "meaning": "解" * 200, "example": "例" * 200}],
                           "2026-09-24")[0]
ck_true("解释被截断", len(long_one["meaning"]) <= corpus.MAX_MEANING_CHARS + 1,
        len(long_one["meaning"]))
ck_true("例句被截断", len(long_one["example"]) <= corpus.MAX_EXAMPLE_CHARS + 1,
        len(long_one["example"]))

ck("英文别名 meaning 也认", corpus.sanitize([{"name": "a", "desc": "d"}], "x")[0]["meaning"], "d")
ck("中文键名也认", corpus.sanitize([{"name": "a", "解释": "d"}], "x")[0]["meaning"], "d")


# ==========================================================================
print("\n=== 7. 模型输出解析 ===")
# ==========================================================================
one = '[{"name":"甲","meaning":"m"}]'
ck("裸 JSON", len(corpus._load_json_array(one)), 1)
ck("带代码块围栏", len(corpus._load_json_array("```json\n%s\n```" % one)), 1)
ck("前后有废话", len(corpus._load_json_array("好的，这是结果：\n%s\n希望有帮助！" % one)), 1)
ck("非法 JSON → 空", corpus._load_json_array("[{坏了"), [])
ck("不是数组 → 空", corpus._load_json_array('{"name":"甲"}'), [])
ck("空字符串 → 空", corpus._load_json_array(""), [])
ck("纯文字 → 空", corpus._load_json_array("我不会"), [])
ck("空数组", corpus._load_json_array("[]"), [])


# ==========================================================================
print("\n=== 8. clamp_int 软着陆 ===")
# ==========================================================================
ck("正常值", corpus.clamp_int(7, 5, 1, 90), 7)
ck("脏值 → 默认", corpus.clamp_int("abc", 7, 1, 90), 7)
ck("None → 默认", corpus.clamp_int(None, 7, 1, 90), 7)
ck("超上限被钳", corpus.clamp_int(999, 7, 1, 90), 90)
ck("超下限被钳", corpus.clamp_int(0, 7, 1, 90), 1)
ck("字符串数字也认", corpus.clamp_int("30", 7, 1, 90), 30)
ck("空字符串 → 默认", corpus.clamp_int("", 7, 1, 90), 7)


# ==========================================================================
print("\n=== 9. update 端到端 ===")
# ==========================================================================

def make_search(text):
    return lambda q: text


def make_chat(reply):
    return lambda s, h, u: reply


OK_SEARCH = "【2026年9月 网络热梗】的搜索结果：\n1. 本月新梗盘点\n   甲、乙都火了"
OK_CHAT = json.dumps([{"name": "端到端甲", "meaning": "端到端的梗", "example": "例句"}],
                     ensure_ascii=False)

mp = TMP / "memes.md"
mp.write_text(MANUAL, encoding="utf-8")

# 9.1 正常一轮
res = corpus.update(mp, make_search(OK_SEARCH), make_chat(OK_CHAT), max_items=10)
ck("正常一轮 ok", res.get("ok"), True)
ck("新增 1 条", res.get("added"), 1)
after = mp.read_text(encoding="utf-8")
ck_true("文件里有新梗", "端到端甲" in after, after[-200:])
ck_true("手写区还在", after.startswith(MANUAL.rstrip()), after[:50])

# 9.2 幂等：同一批再跑不会重复
res = corpus.update(mp, make_search(OK_SEARCH), make_chat(OK_CHAT), max_items=10)
ck("重复跑 → 不再新增", res.get("added"), 0)
ck("重复跑 → 文件没变", mp.read_text(encoding="utf-8"), after)

# 9.3 搜索没结果 → 不动文件
before = mp.read_text(encoding="utf-8")
res = corpus.update(mp, make_search("（搜索没拿到结果，换个说法再试试）"),
                    make_chat(OK_CHAT), max_items=10)
ck("搜索没结果 → ok=False", res.get("ok"), False)
ck("搜索没结果 → 文件没动", mp.read_text(encoding="utf-8"), before)

# 9.4 模型没挑出梗 → 不动文件
res = corpus.update(mp, make_search(OK_SEARCH), make_chat("[]"), max_items=10)
ck("模型返回空数组 → ok=False", res.get("ok"), False)
ck("模型返回空数组 → 文件没动", mp.read_text(encoding="utf-8"), before)

res = corpus.update(mp, make_search(OK_SEARCH), make_chat("抱歉，我做不到"), max_items=10)
ck("模型胡言乱语 → ok=False", res.get("ok"), False)
ck("模型胡言乱语 → 文件没动", mp.read_text(encoding="utf-8"), before)

# 9.5 模型全给敏感/垃圾 → 不动文件
BAD_CHAT = json.dumps([{"name": "赌博梗", "meaning": "赌"}, {"name": "", "meaning": ""}],
                      ensure_ascii=False)
res = corpus.update(mp, make_search(OK_SEARCH), make_chat(BAD_CHAT), max_items=10)
ck("全是垃圾 → ok=False", res.get("ok"), False)
ck("全是垃圾 → 文件没动", mp.read_text(encoding="utf-8"), before)

# 9.6 搜索函数抛异常 → 不动文件（关键：不能把异常带出去）
def boom(q):
    raise RuntimeError("网络炸了")


res = corpus.update(mp, boom, make_chat(OK_CHAT), max_items=10)
ck("搜索抛异常 → ok=False", res.get("ok"), False)
ck_true("异常被吞下并回报", "搜索出错" in str(res.get("reason")), res)
ck("搜索抛异常 → 文件没动", mp.read_text(encoding="utf-8"), before)


# 9.7 模型函数抛异常 → 不动文件
def chat_boom(s, h, u):
    raise RuntimeError("模型炸了")


res = corpus.update(mp, make_search(OK_SEARCH), chat_boom, max_items=10)
ck("模型抛异常 → ok=False", res.get("ok"), False)
ck("模型抛异常 → 文件没动", mp.read_text(encoding="utf-8"), before)

# 9.8 dry_run 不落盘
res = corpus.update(mp, make_search(OK_SEARCH),
                    make_chat(json.dumps([{"name": "干跑出来的", "meaning": "m"}],
                                         ensure_ascii=False)),
                    max_items=10, dry_run=True)
ck("dry_run → ok", res.get("ok"), True)
ck("dry_run → written=False", res.get("written"), False)
ck_true("dry_run → 文件里没有它", "干跑出来的" not in mp.read_text(encoding="utf-8"), "")

# 9.9 文件不存在 → 创建一个只有自动区的
fresh = TMP / "brand_new.md"
res = corpus.update(fresh, make_search(OK_SEARCH), make_chat(OK_CHAT), max_items=10)
ck("文件不存在也能建", res.get("ok"), True)
ck_true("自动区建起来了", corpus.MARK_BEGIN in fresh.read_text(encoding="utf-8"), "")
ck_true("新梗在里面", "端到端甲" in fresh.read_text(encoding="utf-8"), "")

# 9.10 每一轮都留下合法 JSON 般的结构（能被再次解析）
rt = corpus._parse_items(corpus._split(mp.read_text(encoding="utf-8"))[1])
ck_true("落盘内容可被解析回条目", len(rt) >= 1, rt)


# ==========================================================================
print("\n=== 10. 和 Persona 的配合（自动区能被读到） ===")
# ==========================================================================
import bot  # noqa: E402

(TMP / "persona_test.md").write_text("# 你是谁\n你叫小柚。", encoding="utf-8")
p = bot.Persona(TMP / "persona_test.md", extras=[mp])
ck_true("人设里带上了梗库", "真嘟假嘟" in p.text, p.text[:120])
ck_true("自动抓的梗也在人设里", "端到端甲" in p.text, "")
ck_true("两个文件都算数", "小柚" in p.text and "端到端甲" in p.text, "")

# 改文件后能热重载（不用重启）
time.sleep(0.05)
mp.write_text(mp.read_text(encoding="utf-8").replace("端到端甲", "改过的梗名"),
              encoding="utf-8")
ck_true("存盘即生效（Persona 热重载）", "改过的梗名" in p.text, p.text[-200:])


# ==========================================================================
print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 56)

shutil.rmtree(TMP, ignore_errors=True)

sys.exit(1 if FAIL else 0)
