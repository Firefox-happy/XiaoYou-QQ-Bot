# -*- coding: utf-8 -*-
"""热梗库自动更新 —— 让 memes.md 自己从网上长出新梗，不用人动手。

### 为什么不让模型直接写 markdown

模型写格式一定会飘：少个横杠、多一层缩进、混进解释性段落、自作主张加标题。
而这个文件会被**原样拼进她的系统提示**，飘一次就长期影响她，而且很难发现
（提示词里的格式问题不会报错）。所以流程拆成两步：

    模型只负责「判断什么算梗、它是什么意思」→ 吐结构化 JSON
    渲染 markdown（缩进、加粗、分行）→ 由代码做

这样无论模型多不靠谱，落盘的文件格式永远是合法的。

### 为什么只动「自动区」

memes.md 里手写的梗是精挑细选的 —— 一个梗配什么例句、什么场合别用，
都是斟酌过的，价值远高于自动抓的。所以用一对注释标记划出自动区，
**手写的内容永远不被碰**。万一标记被手滑删了，也不会乱写：只是把自动区
重新补到文件末尾。

### 为什么要淘汰

这个文件每次对话都会进 prompt。只增不减的话，半年后它会变成几万字，
既烧 token 又稀释注意力（梗库越长，模型越可能挑不准该用哪个）。
自动区保留最近 N 条，超出的自动滚掉。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

# 复用 bot.py 的 logger 名字 —— 这样这里的日志会跟着进 logs/bot.log，
# 而且自动继承「跑测试时不写生产日志」那条规矩（见 bot.py 顶部）。
logger = logging.getLogger("xiaoyou")

MARK_BEGIN = "<!-- AUTO-MEMES:BEGIN -->"
MARK_END = "<!-- AUTO-MEMES:END -->"

DEFAULT_MAX_ITEMS = 24          # 自动区保留多少条
MAX_NAME_CHARS = 18
MAX_MEANING_CHARS = 64
MAX_EXAMPLE_CHARS = 64
MAX_RAW_ITEMS = 40              # 模型一次最多能交付多少条，超出直接截断

SECTION_TITLE = "## 八、自动抓来的新梗（机器人自己更新的）"
SECTION_NOTE = (
    "> 下面这些是小柚自己定时从网上抓的，**你不用手改**（改了会被覆盖）。\n"
    "> 想手写梗，请写在上面几节里。"
)

# 命中这些词的热梗一律不收 —— 收进来她就会照着说。
#
# 注意和「撒娇式怼人」不冲突：这里挡的不是"骂人"，而是**会被她学走的
# 敏感源**。前者是她对主人的亲昵，后者是政治/色情/违法/仇恨内容，
# 属于人设里那三条红线，一个字都不能沾。
BLOCKED = (
    # 政治 / 时政
    "习近平", "习近", "共产党", "政治局", "总书记", "国家主席", "国务院",
    "两会", "信访", "游行", "示威", "镇压", "六四", "天安门", "法轮",
    "台独", "港独", "藏独", "疆独", "反华", "辱华", "亡国", "煽动",
    # 色情
    "约炮", "援交", "裸聊", "做爱", "性爱", "开房", "黄片", "淫", "嫖",
    "娼", "炮友", "肉便",
    # 赌博 / 毒品 / 违法
    "赌博", "博彩", "六合彩", "毒品", "冰毒", "大麻", "枪支", "军火",
    "诈骗", "洗钱", "刷单", "代开", "办证", "杀人", "自杀",
    # 真实歧视 / 仇恨
    "地域黑", "仇女", "仇男", "支那", "黑鬼", "贱民", "洋垃圾",
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def clamp_int(v, default: int, lo: int, hi: int) -> int:
    """配置里读来的数一律软着陆 —— 脏值不许抛异常，只许退回默认值。

    和 tools.py 的 `_safe_int` 同一个套路。这里单独写一份是因为
    corpus 不想反向依赖 tools（tools 也不依赖它，保持各管一摊）。
    """
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _norm(s) -> str:
    """归一化梗名，用来判断"这两条是不是同一个梗"。

    去掉空白和标点 —— 「真嘟假嘟」「真嘟假嘟？」「真 嘟 假 嘟」算同一个。
    """
    return re.sub(r"[\s\-_|·,，。、!！?？~～:：;；]+", "", str(s)).lower()


def _clean(s) -> str:
    """抹掉模型可能带进来的 markdown 记号（它一激动就会加粗）。"""
    return re.sub(r"\s+", " ", re.sub(r"[*`#>]+", "", str(s))).strip()


def _clip(s, n: int) -> str:
    s = str(s).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _blocked(text) -> bool:
    t = str(text).lower()
    return any(w in t for w in BLOCKED)


def _atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再原子替换。

    和 bot.py 里那个同名函数是同一套做法。这里没直接 import 它，
    是因为 bot.py 要 import 本模块（接线），反向 import 会成环。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 文件结构：拆分 / 合并
# --------------------------------------------------------------------------

def _split(text: str):
    """把文件切成 (标记前的部分, 自动区内容, 标记后的部分, 有没有自动区)。"""
    i = text.find(MARK_BEGIN)
    j = text.find(MARK_END)
    if i == -1 or j == -1 or j < i:
        return text, "", "", False
    head = text[:i]
    body = text[i + len(MARK_BEGIN): j]
    tail = text[j + len(MARK_END):]
    return head, body, tail, True


def existing_names(text: str) -> set[str]:
    """把文件里已经出现过的梗名全抓出来 —— 自动抓的条目要拿它去重。

    两种写法都要认：
      - 加粗条目：`- **真嘟假嘟**【当季】——…`（第一~四节，以及自动区自己）
      - 平铺清单：`绝了 / 破防了 / emo 了 / …`（第五节的"常青"）
    """
    names: set[str] = set()
    for m in re.finditer(r"\*\*(.+?)\*\*", text):
        n = _norm(m.group(1))
        if n:
            names.add(n)
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#") or s[:1] in "-*|":     # 标题 / 列表 / 表格行
            continue
        # 「平铺清单」的特征是一行里斜杠很多。**引用行也要看一眼** ——
        # 过期清单就写在引用里（`> 雨女无瓜 / 奥利给 / …`），漏掉它，
        # 自动区就会把"已经宣布不用了"的梗又抓回来，自相矛盾。
        # 而引用行里的例句（`> 小柚：真嘟假嘟？`）斜杠很少，天然被挡在外面。
        body = s.lstrip(">").strip()
        if body.count("/") < 3:
            continue
        for part in body.split("/"):
            p = part.strip()
            if 1 <= len(p) <= 12:
                names.add(_norm(p))
    names.discard("")
    return names


_ITEM_RE = re.compile(r"^-\s*\*\*(.+?)\*\*【自动·([\d\-]+)】——(.*)$")
_EX_RE = re.compile(r"^>\s*小柚[:：]\s*(.*)$")


def _parse_items(body: str) -> list[dict]:
    """把自动区的正文还原成条目列表（老条目要留着，不能每次都清空重来）。"""
    items: list[dict] = []
    cur: dict | None = None
    for line in body.splitlines():
        s = line.strip()
        m = _ITEM_RE.match(s)
        if m:
            if cur:
                items.append(cur)
            cur = {"name": m.group(1).strip(), "date": m.group(2),
                   "meaning": m.group(3).strip(), "example": ""}
            continue
        if cur is not None:
            m2 = _EX_RE.match(s)
            if m2:
                cur["example"] = m2.group(1).strip()
    if cur:
        items.append(cur)
    return items


def _render_items(items: list[dict]) -> str:
    lines = []
    for it in items:
        line = "- **%s**【自动·%s】——%s" % (it["name"], it["date"], it["meaning"])
        if it.get("example"):
            line += "\n  > 小柚：" + it["example"]
        lines.append(line)
    return "\n".join(lines)


def merge(memes_text: str, new_items: list[dict], max_items: int) -> tuple[str, int]:
    """把新条目并进文件，返回 (新全文, 实际新增条数)。

    顺序上**新条目排在前面** —— 自动区就是"最近抓到的越靠前"，
    淘汰时从尾部砍，等于先扔最旧的。
    """
    head, body, tail, has_block = _split(memes_text)
    old_items = _parse_items(body) if has_block else []

    # 手写区（标记外）里已有的梗名，新条目一律不许重复
    manual = existing_names(head) | existing_names(tail)

    seen: set[str] = set()
    merged: list[dict] = []
    for it in list(new_items) + old_items:      # 新的优先，老的补齐
        k = _norm(it.get("name"))
        if not k or k in seen or k in manual:
            continue
        seen.add(k)
        merged.append(it)
    merged = merged[:max_items]

    # 「新增」= 这次真的往库里添了新梗。
    #
    # 模型再次提到自动区里已有的梗时，那条会被顶到最前面（说明它还在流行），
    # 日期也刷新 —— 但那**不算新增**。否则日志会天天报"新增 N 条"，
    # 看着像库在膨胀，其实一条没多。
    old_keys = {_norm(it.get("name")) for it in old_items}
    fresh_keys = {_norm(it.get("name")) for it in new_items} - old_keys - manual
    added = len([x for x in merged if _norm(x.get("name")) in fresh_keys])

    block = _render_items(merged)
    if has_block:
        new_text = head + MARK_BEGIN + "\n" + block + "\n" + MARK_END + tail
    else:
        section = "\n\n---\n\n%s\n\n%s\n\n%s\n%s\n%s\n" % (
            SECTION_TITLE, SECTION_NOTE, MARK_BEGIN, block, MARK_END)
        new_text = memes_text.rstrip() + section
    return new_text, added


# --------------------------------------------------------------------------
# 模型输出 → 干净的条目
# --------------------------------------------------------------------------

def sanitize(raw_items, today: str) -> list[dict]:
    """把模型吐的东西洗成可信条目。任何一条不合格就丢掉，不迁就。"""
    out: list[dict] = []
    for d in (raw_items or [])[:MAX_RAW_ITEMS]:
        if not isinstance(d, dict):
            continue
        name = _clean(d.get("name") or "")
        meaning = _clean(d.get("meaning") or d.get("desc") or d.get("解释") or "")
        example = _clean(d.get("example") or d.get("例句") or "")
        if not name or not meaning:
            continue
        if len(name) > MAX_NAME_CHARS:
            continue
        if _blocked(name) or _blocked(meaning) or _blocked(example):
            logger.info("梗库：丢弃敏感条目 %r", name)
            continue
        out.append({
            "name": name,
            "meaning": _clip(meaning, MAX_MEANING_CHARS),
            "example": _clip(example, MAX_EXAMPLE_CHARS),
            "date": today,
        })
    return out


def _load_json_array(text: str) -> list:
    """从模型输出里挖出 JSON 数组 —— 容忍它套代码块、加前言、说废话。"""
    t = str(text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", t).strip()
    i, j = t.find("["), t.rfind("]")
    if i == -1 or j <= i:
        return []
    try:
        v = json.loads(t[i:j + 1])
    except Exception as e:
        logger.warning("梗库：模型输出不是合法 JSON（%s）", e)
        return []
    return v if isinstance(v, list) else []


SYSTEM = (
    "你是一个「网络热梗」筛选器。用户会给你一批网页搜索结果的原文，"
    "请你从中认出真正在流行的中文网络热梗 / 流行语，输出一个 JSON 数组。\n"
    "\n"
    "只输出 JSON 本身，不要解释、不要 markdown 代码块、不要任何前后缀。\n"
    "\n"
    "每个元素的形状：\n"
    '{"name": "梗名", "meaning": "一句话说明它是什么意思、什么场合用",'
    ' "example": "一句用得上它的话（可留空）"}\n'
    "\n"
    "严格遵守：\n"
    "1. 只挑**梗和流行语**。新闻事件、人物、公司、产品、广告、投票活动一律不要。\n"
    "2. 拿不准是什么意思的，不要；明显已过时的老梗（如\"蓝瘦香菇\"\"给力\"），不要。\n"
    "3. 涉政、涉黄、涉赌毒、地域歧视、辱骂特定人群的，一律不要。\n"
    "4. name 不超过 12 字，meaning 不超过 40 字，example 不超过 40 字。\n"
    "5. 宁缺勿滥：一条可用的都没有，就输出 []。"
)


def pick_memes(search_text: str, chat_fn, max_items: int) -> list[dict]:
    """让模型从搜索结果里挑梗。chat_fn(system, history, user) -> str。"""
    user = ("以下是搜索结果原文：\n\n%s\n\n"
            "请从中挑出最多 %d 条热梗，只输出 JSON 数组。" % (search_text, max_items))
    try:
        raw = chat_fn(SYSTEM, [], user)
    except Exception as e:
        logger.warning("梗库：模型调用失败 %s", e)
        return []
    return sanitize(_load_json_array(raw), time.strftime("%Y-%m-%d"))


# --------------------------------------------------------------------------
# 一次性更新
# --------------------------------------------------------------------------

def update(memes_path: Path, search_fn, chat_fn, max_items: int = DEFAULT_MAX_ITEMS,
           query: str | None = None, dry_run: bool = False) -> dict:
    """跑一轮：搜 → 挑 → 渲染 → 原子写。

    返回一个结果字典（给日志用）。**任何一步失败都不动原文件** ——
    自动更新最忌讳的就是"抓挂了还把好文件写坏"。
    """
    max_items = clamp_int(max_items, DEFAULT_MAX_ITEMS, 4, 80)
    now = time.localtime()
    q = query or "%d年%d月 网络热梗 流行语 盘点" % (now.tm_year, now.tm_mon)

    try:
        found = search_fn(q)
    except Exception as e:
        return {"ok": False, "reason": "搜索出错: %s" % e}
    if not found or "没拿到结果" in found:
        return {"ok": False, "reason": "搜索没拿到结果"}

    items = pick_memes(found, chat_fn, max_items)
    if not items:
        return {"ok": False, "reason": "模型没挑出可用的梗"}

    try:
        old_text = memes_path.read_text(encoding="utf-8")
    except OSError:
        old_text = ""

    new_text, added = merge(old_text, items, max_items)
    if new_text == old_text:
        return {"ok": True, "added": 0, "total": len(items),
                "names": [], "note": "没有新梗，未改动"}

    if not dry_run:
        _atomic_write_text(memes_path, new_text)

    names = [it["name"] for it in items][:8]
    logger.info("梗库已更新：新增 %d 条（自动区上限 %d）%s",
                added, max_items, "、".join(names))
    return {"ok": True, "added": added, "total": len(items),
            "names": names, "written": not dry_run}
