# -*- coding: utf-8 -*-
"""小柚 · 情绪语音引擎
======================

解决的问题
----------
她的语音一直"平"，因为 `voice.speak()` 每条都拿同一组参数
（rate +8% / pitch +10Hz）—— 撒娇和生气念出来一模一样。

为什么不能靠改 SSML 解决（**实测结论，别推翻重做**）
--------------------------------------------------
Edge TTS 免费端点对 SSML 的限制极严，逐一试过：

    <prosody>（单个）              ✅ 认
    xmlns:mstts 命名空间声明       ✅ 认（但没用）
    <mstts:express-as style=...>   ❌ NoAudioReceived（换音色也一样）
    <break time=...>               ❌ 同上
    <emphasis level=...>           ❌ 同上
    多个 <prosody> 并列            ❌ 同上

也就是说一次请求**只能**带一组 rate/pitch/volume。Azure 那边能写
`express-as` 的"情绪样式"，免费端点不给用。

于是情绪的出路只有两条，本模块做的是第一条：

1. **客户端合成起伏**（本模块）：按标点把一句切成几个意群，每个意群
   单独给一组 prosody，再拼起来 —— 段与段的落差就是"起伏"，
   中间插静音就是"停顿"（顶替用不了的 <break>）。
2. 换引擎（Azure Speech / 讯飞情感发音人 / 本地 CosyVoice、GPT-SoVITS），
   那是 `voice.py` 的 `tts_provider` 该管的事，不在本模块。

两层设计
--------
- **条级**：整句先定一个情绪（`EMOTIONS` 里的一档），决定基准参数。
- **句内**：按情绪自带的 `shape`（头/中/尾三段的放大系数）做出弧线，
  避免从头到尾一条直线。

`detect()` 是启发式的、纯代码的 —— 不动提示词、不改她的回复格式，
所以可以随时开关。等哪天要让模型自己标情绪（更准），也只需在
`EMOTIONS` 上多接一个来源，不必改这里的结构。
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# 情绪表：一条情绪 = 一组 prosody + 一个"句内弧线"
#
# rate  语速%，pitch 音调 Hz，volume 音量%（都是相对值的整数，与
# `voice._edge_synth` 收的口径一致 —— 翻译成 "+8%" 字符串只在那一边做）。
#
# shape 是 (头, 中, 尾) 三段的系数，乘到该情绪的参数增量上。
#
# ⚠️ 这三个系数**一律不得超过 1.0**：表里的 rate/pitch 已经是该情绪的
# 满格值，系数再往上乘就是超速——实测把"兴奋"的 pitch 顶到 +56Hz，
# 念出来是电子玩具在尖叫，不是人在兴奋。起伏靠**衰减**做，不靠加码：
#   兴奋 → 头满格，后面一路收（抢着说 → 说完了）
#   撒娇 → 头压一点，尾巴才满格（越说越黏，尾音翘上去）
#   委屈 → 越说越小声（尾巴满格，但那是"低"的满格）
# pitch 一律不超过 ±50Hz。
# --------------------------------------------------------------------------

BASE = {"rate": 8, "pitch": 10, "volume": 0}

EMOTIONS: dict[str, dict] = {
    "normal": {
        "name": "平常", "rate": 8, "pitch": 10, "volume": 0,
        "shape": (1.0, 1.0, 1.0),
        "desc": "现在就是这个 —— 平，但稳",
    },
    "happy": {
        "name": "开心", "rate": 25, "pitch": 30, "volume": 8,
        "shape": (1.0, 0.82, 0.92),
        "desc": "轻快上扬，一句话带笑",
    },
    "excited": {
        "name": "兴奋", "rate": 35, "pitch": 42, "volume": 12,
        "shape": (1.0, 0.82, 0.7),
        "desc": "开头冲出去，后面收",
    },
    "cute": {
        "name": "撒娇", "rate": -8, "pitch": 46, "volume": 5,
        "shape": (0.7, 0.85, 1.0),
        "desc": "越说越黏，尾音翘上去",
    },
    "shy": {
        "name": "害羞", "rate": -15, "pitch": 32, "volume": -6,
        "shape": (1.0, 0.8, 0.9),
        "desc": "开口就小声，说着说着更轻",
    },
    "angry": {
        "name": "生气", "rate": 22, "pitch": 18, "volume": 15,
        "shape": (1.0, 0.85, 0.6),
        "desc": "头最冲，尾巴硬收住",
    },
    "sad": {
        "name": "委屈", "rate": -22, "pitch": -22, "volume": -12,
        "shape": (0.85, 0.9, 1.0),
        "desc": "低、慢，越说越小声",
    },
    "surprised": {
        "name": "惊讶", "rate": 12, "pitch": 44, "volume": 10,
        "shape": (1.0, 0.8, 0.65),
        "desc": "开头拔高，后面落回",
    },
    "soft": {
        "name": "温柔", "rate": -28, "pitch": 0, "volume": -10,
        "shape": (1.0, 0.9, 0.8),
        "desc": "哄睡、说晚安用",
    },
    "smug": {
        "name": "得意", "rate": 6, "pitch": 26, "volume": 4,
        "shape": (1.0, 0.85, 0.95),
        "desc": "抖着说，尾巴翘",
    },
}

# 语气词/标点给的信号。命中越多越倾向该情绪 —— 顺序即优先级。
_HINTS: list[tuple[str, tuple[str, ...], int]] = [
    ("sad",       ("呜呜", "呜……", "对不起", "不要走", "别不理", "委屈", "难过", "想哭"), 3),
    ("cute",      ("抱抱", "喵呜", "蹭蹭", "求你", "好不好嘛", "嘛～", "嘛~", "陪我"), 3),
    ("excited",   ("啊啊", "太棒", "好耶", "哇塞", "！！", "冲鸭", "救命"), 3),
    ("angry",     ("哼！", "讨厌", "走开", "不要你", "臭", "坏蛋", "气死", "烦人"), 3),
    ("shy",       ("才不是", "才没有", "讨厌啦", "别、别", "脸红", "不准看"), 3),
    ("surprised", ("诶？！", "咦", "什么？！", "真的吗", "不会吧", "天呐"), 2),
    ("happy",     ("嘿嘿", "开心", "太好了", "喜欢", "嘻嘻", "好呀", "嘿嘿嘿"), 2),
    ("soft",      ("晚安", "睡吧", "休息", "乖乖", "别怕", "早点睡"), 2),
    ("smug",      ("早就说", "看吧", "厉害吧", "当然啦", "佩服我吧"), 2),
]

# 切句：标点留在前一段的尾巴上，念起来才自然
_SPLIT_RE = re.compile(r"[^。！？!?…~～，,；;、]+[。！？!?…~～，,；;、]*")
_LONG_GAP = {"。": 320, "！": 260, "？": 260, "!": 260, "?": 260, "…": 340}
_SHORT_GAP = {"，": 150, ",": 150, "；": 180, ";": 180, "、": 120,
              "~": 180, "～": 180, "。": 320}


def clamp(v: int, lo: int = -100, hi: int = 100) -> int:
    return max(lo, min(hi, v))


def emotion_keys() -> list[str]:
    return list(EMOTIONS)


def get(key: str) -> dict:
    """按名字取情绪；认不出就给平常那档（绝不抛异常）。"""
    e = EMOTIONS.get(str(key or "").strip().lower())
    return e if e else EMOTIONS["normal"]


def detect(text: str) -> str:
    """纯启发式判情绪 —— 不看模型、不改回复格式。

    拿到分就先按分排；一个都没命中时退化成"按标点猜气势"：
    一堆感叹号＝兴奋，一串省略号＋问号＝委屈。宁可判错也不要判成
    永远一个调 —— 一句话听起来"有点起伏"就赢了。
    """
    t = (text or "").strip()
    if not t:
        return "normal"

    score: dict[str, int] = {}
    for key, words, weight in _HINTS:
        for w in words:
            if w in t:
                score[key] = score.get(key, 0) + weight
    if score:
        best = max(score.items(), key=lambda kv: (kv[1], -list(EMOTIONS).index(kv[0])))
        return best[0]

    marks = t.count("！") + t.count("!") + t.count("？！") + t.count("！？")
    qmarks = t.count("？") + t.count("?")
    dots = t.count("…") + t.count("。。")
    if marks >= 2:
        return "excited"
    if qmarks >= 2 or (marks and qmarks):
        return "surprised"
    if dots >= 2 or (dots and qmarks):
        return "sad"
    if marks == 1 and len(t) <= 12:
        return "happy"
    if "~" in t or "～" in t:
        return "cute"
    return "normal"


def split_clauses(text: str, max_segments: int = 4, min_chars: int = 3) -> list[str]:
    """一句话 → 几个意群。

    太碎的片段会被并到前一段（每段至少 `min_chars` 个字），
    最后一段吃掉超出的部分 —— 保证段数不超过 `max_segments`。
    每多一段就多一次网络请求（Edge 约 1.5 秒），所以默认最多 4 段。
    """
    t = (text or "").strip()
    if not t:
        return []
    parts = [m.group(0).strip() for m in _SPLIT_RE.finditer(t)]
    parts = [p for p in parts if p]
    if not parts:
        return [t]

    # 合并过短的片段
    merged: list[str] = []
    for p in parts:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] += p
        else:
            merged.append(p)

    # 段数超了就从头两段开始并（保留尾部独立，情绪尾巴通常在这儿）
    while len(merged) > max_segments:
        merged[0] = merged[0] + merged[1]
        del merged[1]
    return merged


def _scale(emotion: dict, factor: float) -> dict:
    """把情绪的增量按系数缩放，再叠回基准值。"""
    base_rate = BASE["rate"]
    return {
        "rate": clamp(int(round(base_rate + (emotion["rate"] - base_rate) * factor))),
        "pitch": clamp(int(round(BASE["pitch"] + (emotion["pitch"] - BASE["pitch"]) * factor)), -100, 100),
        "volume": clamp(int(round(BASE["volume"] + (emotion["volume"] - BASE["volume"]) * factor))),
    }


def gap_after(clause: str) -> int:
    """这个意群后该停多久（毫秒）。顶替用不了的 SSML <break>。"""
    tail = clause.strip()[-1:] if clause.strip() else ""
    if tail in _LONG_GAP:
        return _LONG_GAP[tail]
    return _SHORT_GAP.get(tail, 160)


def contour(text: str, emotion_key: str = "normal",
            max_segments: int = 4) -> list[dict]:
    """一句 → 待合成的分段计划。

    返回 [{"text","rate","pitch","volume","gap_ms"}, ...]。
    单段时 gap_ms=0（没必要在末尾留白）。

    头/中/尾系数用法：段数为 1 时直接用满格（就是该情绪的准确值）；
    2 段用头尾；3 段以上中间几段用"中"，形成"起—平—落"的弧线。
    """
    e = get(emotion_key)
    clauses = split_clauses(text, max_segments=max_segments)
    if not clauses:
        return []

    head, mid, tail = e.get("shape", (1.0, 1.0, 1.0))
    n = len(clauses)
    out: list[dict] = []
    for i, c in enumerate(clauses):
        if n == 1:
            factor = 1.0                      # 单段＝不做起伏，用满格
        elif i == 0:
            factor = head
        elif i == n - 1:
            factor = tail
        else:
            factor = mid
        factor = min(factor, 1.0)             # 保险：任何情况都不许超速
        p = _scale(e, factor)
        out.append({"text": c, "rate": p["rate"], "pitch": p["pitch"],
                    "volume": p["volume"],
                    "gap_ms": gap_after(c) if i < n - 1 else 0})
    return out


def plan_for(text: str, emotion_key: str | None = None,
             max_segments: int = 4) -> tuple[str, list[dict]]:
    """一步到位：判情绪 + 出分段计划 → (情绪名, 计划)。"""
    key = emotion_key if emotion_key in EMOTIONS else detect(text)
    return key, contour(text, key, max_segments=max_segments)


def table_for_page() -> list[dict]:
    """给设置页/文档：情绪清单。"""
    return [{"key": k, "name": v["name"], "rate": v["rate"], "pitch": v["pitch"],
             "volume": v["volume"], "desc": v["desc"]} for k, v in EMOTIONS.items()]
