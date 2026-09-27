#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
小柚 · 工具箱
==============

让小柚除了陪聊，还能真的替主人办点事：

  web_search   联网搜资料
  read_url     读一个网页、抽出正文
  get_weather  查天气
  get_time     看现在几点
  calculate    精确算数（加减乘除、比大小、百分比）—— 大模型心算不可信
  remember     记住主人的事（永久，落盘）

设计原则
--------
1. **全部免 Key、免注册。** 这台机器没有代理，国外服务（DuckDuckGo /
   Google / 维基 / Brave）实测全部 ReadTimeout，所以搜索走**必应中国**：
   连续 6 次请求、每次 10 条结果、0.2 秒、不出验证码。

2. **返回给模型的是纯文本，不是 JSON。** 少一层结构，就少一次解析出错
   的机会；模型直接读得懂，也不用在 prompt 里教它怎么读 JSON。

3. **任何工具失败都返回一句人话，绝不抛异常。** 小柚可以照常说
   "这个没查到喵"，而不是整条消息哑掉 —— 静默失败是最难排查的故障。

4. **请求走 trust_env=False。** 不理会系统代理，行为在任何环境下都一样。
"""

from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, localcontext
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

logger = logging.getLogger("xiaoyou.tools")

BASE_DIR = Path(__file__).resolve().parent

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 一次搜索/抓取最多给模型看多少字 —— 太长会吃掉对话预算
SEARCH_MAX_CHARS = 2200
PAGE_MAX_CHARS = 4500

# 抓网页时最多往内存里读多少字节。超过就截断当读完 ——
# 一个不小心指向大文件的链接（视频、镜像、导出的数据库）会把内存吃爆，
# 而 timeout 拦不住它：慢慢吐字节的连接不会超时。
PAGE_MAX_BYTES = 3_000_000


def _atomic_replace_text(path: Path, text: str) -> None:
    """先写 .tmp 再原子替换。长期记忆被强杀砍成半截就整份读不出来了。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# read_url 能碰到的地址必须过这一关。
# 模型既可能拿到主人发的链接，也可能自己挑链接去读 —— 万一它给出
# http://127.0.0.1:6099/webui?token=... 这种本机地址，等于把管理后台
# 摆到聊天窗里（群里谁 @ 它一下就成）。所以先验地址、再发请求。
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254"}


def _is_local_host(host: str) -> bool:
    """主机名是否指向本机 / 内网。

    目标是拦住"随口就能想到的"那几类：localhost、环回、私有网段、
    链路本地（含云元数据地址）。不追求挡住 DNS rebinding 那类花活 ——
    这台机器上跑的也不是对外的服务，拦到这个程度就够。
    """
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return True
    if h in _BLOCKED_HOSTS or h.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(h)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False          # 是个域名，不是 IP


def _decode(raw: bytes) -> str:
    """bytes → 文字。按常见中文编码试一遍。

    中文站点的 header 常常不带 charset，requests 会退化成 latin-1，
    'æ°é¦é¡µ' 就是那么来的。流式读进来的已经是裸 bytes，只能自己试。
    """
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _clean_html(s: str) -> str:
    """把一段 HTML 变成读得懂的纯文本。

    最后那两行是必须的：网页里 </div> 挨着 </div> 会连出一串空行，
    直接喂给模型会白烧一大截 token，读起来也脏。
    """
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(p|div|li|h[1-6]|tr|section|article)>", "\n", s)
    s = re.sub(r"(?is)<[^>]+>", "", s)
    s = unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = "\n".join(ln for ln in s.split("\n") if ln.strip())
    return s.strip()


# wttr.in 的 lang=zh 参数是坏的 —— 字段 lang_zh 会返回，但值仍是英文
# （实测 'Light rain shower' 原样躺在 lang_zh 里）。所以自己翻译。
# 顺序有讲究：长的、具体的放前面，否则 "rain" 会先命中 "light rain shower"。
_WEATHER_ZH: list[tuple[str, str]] = [
    ("patchy light rain with thunder", "局部小雨伴雷"),
    ("moderate or heavy rain with thunder", "中到大雨伴雷"),
    ("moderate or heavy freezing rain", "中到大冻雨"),
    ("moderate or heavy sleet", "中到大雨夹雪"),
    ("moderate or heavy rain shower", "中到大阵雨"),
    ("moderate or heavy snow showers", "中到大阵雪"),
    ("thundery outbreaks possible", "可能有雷阵雨"),
    ("patchy light drizzle", "局部毛毛雨"),
    ("patchy light rain", "局部小雨"),
    ("patchy light snow", "局部小雪"),
    ("patchy sleet possible", "局部雨夹雪"),
    ("patchy snow possible", "局部有雪"),
    ("patchy rain nearby", "附近有零星雨"),
    ("patchy rain possible", "可能有零星雨"),
    ("light freezing rain", "小冻雨"),
    ("torrential rain shower", "暴雨"),
    ("light rain shower", "小阵雨"),
    ("light sleet showers", "小雨夹雪"),
    ("light snow showers", "小阵雪"),
    ("blowing snow", "风吹雪"),
    ("blizzard", "暴风雪"),
    ("heavy snow", "大雪"),
    ("moderate snow", "中雪"),
    ("light snow", "小雪"),
    ("heavy rain", "大雨"),
    ("moderate rain", "中雨"),
    ("light rain", "小雨"),
    ("light drizzle", "毛毛雨"),
    ("freezing fog", "冻雾"),
    ("ice pellets", "冰粒"),
    ("sleet", "雨夹雪"),
    ("smoky haze", "烟霾"),
    ("sandstorm", "沙尘暴"),
    ("blowing dust", "扬沙"),
    ("overcast", "阴"),
    ("partly cloudy", "局部多云"),
    ("cloudy", "多云"),
    ("haze", "霾"),
    ("smoke", "烟"),
    ("dust", "浮尘"),
    ("mist", "薄雾"),
    ("fog", "雾"),
    ("clear", "晴"),
    ("sunny", "晴"),
]


def _weather_zh(desc: str) -> str:
    """英文天气描述 → 中文。翻不出来就原样返回，不瞎猜。"""
    d = (desc or "").strip()
    if not d:
        return ""
    low = d.lower()
    for en, zh in _WEATHER_ZH:
        if en == low:
            return zh
    for en, zh in _WEATHER_ZH:
        if en in low:
            return zh
    return d


def _text(resp: requests.Response) -> str:
    """取响应正文，顺手修掉 requests 猜错编码导致的乱码。

    新浪、人民网这些站点的 header 不带 charset，requests 会退化成
    latin-1，中文全变成 'æ°é¦é¡µ' 这种鬼东西。
    """
    enc = (resp.encoding or "").lower()
    if enc in ("", "iso-8859-1", "ascii", "latin-1"):
        resp.encoding = resp.apparent_encoding or "utf-8"
    try:
        return resp.text
    except Exception:
        return resp.content.decode("utf-8", "ignore")


def _unwrap_bing_url(url: str) -> str:
    """必应有时把结果包成 /ck/a?...&u=a1<base64> 跳转，拆回真实地址。"""
    if "bing.com/ck/a" not in url:
        return url
    try:
        qs = parse_qs(urlparse(url).query)
        raw = (qs.get("u") or [""])[0]
        if raw.startswith("a1"):
            import base64
            pad = "=" * (-len(raw[2:]) % 4)
            return base64.urlsafe_b64decode(raw[2:] + pad).decode("utf-8", "ignore")
    except Exception:
        pass
    return url


def _make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False                     # 不受系统代理影响，行为可预测
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


# ==========================================================================
# 精确算术 · 小柚的计算器
# ==========================================================================
#
# 大模型不会算数，它是"猜"的。实测它在群里把 9.11 和 9.8 的大小说反了 ——
# 因为它在按字符串比："9.11" 比 "9.8" 长，就判 9.11 大。
# 提示词里写十遍"你算准点"也没用，这是概率模型的固有毛病。
# 唯一可靠的办法：给它一个真的计算器，让它把算式丢过来。
#
# 两条硬约束：
#   1. **绝不用 eval。** 表达式先 ast.parse 成语法树，再按白名单逐个节点求值。
#      属性访问 / 下标 / lambda / 变量名 / 关键字参数 一律拒绝 ——
#      否则主人（或提示注入）丢一句 __import__("os").system("...") 就是远程执行。
#   2. **数值走 int / Decimal，不走 float。** float 会带来
#      9.11 - 9.8 = -0.6900000000000004 这种噪声，正是要消灭的东西。

CALC_MAX_EXPR = 200      # 表达式长度上限
CALC_MAX_POW = 1000      # 幂的指数上限，挡住 9**9**9 这种内存炸弹
CALC_MAX_FRAC = 16       # 小数最多显示几位（1/7 会算 40 位，但没必要全倒给主人）
CALC_PREC = 40           # 内部计算精度，给 1/3、1/7 这种循环小数留够位数

# abs/round/min/max/floor/ceil 保精度；sqrt/log/sin 这些走 float
# （无理数本来就没有精确的十进制表示，收一下尾巴就够了）
_CALC_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max,
    "floor": math.floor, "ceil": math.ceil,
}
_CALC_FLOAT_FUNCS = {
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
}
_CALC_CONSTS = {"pi": math.pi, "e": math.e}


class _CalcError(Exception):
    """算式里有不允许的东西。带一句人话，给模型看。"""


def _to_dec(v):
    """int / Decimal → Decimal。**不走 float**，避免二进制尾数噪声。"""
    return v if isinstance(v, Decimal) else Decimal(v)


def _num_float(x: float):
    """float 结果收尾：能整除就还原成整数，否则只留 10 位小数。"""
    try:
        if x == int(x) and abs(x) < 1e15:
            return int(x)
    except (OverflowError, ValueError):
        pass
    return Decimal(repr(round(x, 10)))


def _calc_binop(op, a, b):
    both_int = isinstance(a, int) and isinstance(b, int)
    if isinstance(op, ast.Add):
        return a + b if both_int else _to_dec(a) + _to_dec(b)
    if isinstance(op, ast.Sub):
        return a - b if both_int else _to_dec(a) - _to_dec(b)
    if isinstance(op, ast.Mult):
        return a * b if both_int else _to_dec(a) * _to_dec(b)
    if isinstance(op, ast.Div):
        if b == 0:
            raise _CalcError("不能除以 0")
        # 整数能整除就别退化成小数：10/2 给 5，不是 5.0
        if both_int and a % b == 0:
            return a // b
        return _to_dec(a) / _to_dec(b)
    if isinstance(op, ast.FloorDiv):
        if b == 0:
            raise _CalcError("不能除以 0")
        return a // b if both_int else _to_dec(a) // _to_dec(b)
    if isinstance(op, ast.Mod):
        if b == 0:
            raise _CalcError("不能除以 0")
        return a % b if both_int else _to_dec(a) % _to_dec(b)
    if isinstance(op, ast.Pow):
        # 小数指数（如 **0.5）根本不是精确十进制，退 float
        if isinstance(b, Decimal) and b != b.to_integral_value():
            return _num_float(math.pow(float(a), float(b)))
        exp = int(b)
        if abs(exp) > CALC_MAX_POW:
            raise _CalcError("指数太大了，算不动")
        if isinstance(a, int) and exp >= 0:
            return a ** exp                      # 整数幂保持精确
        return _to_dec(a) ** exp
    raise _CalcError("不支持的运算符")


def _calc_cmp(op, a, b):
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        a, b = _to_dec(a), _to_dec(b)
    if isinstance(op, ast.Gt):
        return a > b
    if isinstance(op, ast.GtE):
        return a >= b
    if isinstance(op, ast.Lt):
        return a < b
    if isinstance(op, ast.LtE):
        return a <= b
    if isinstance(op, ast.Eq):
        return a == b
    if isinstance(op, ast.NotEq):
        return a != b
    raise _CalcError("不支持这种比较")


def _calc_ev(node):
    """按白名单求值。认识的就算，不认识的一律抛 _CalcError。"""
    if isinstance(node, ast.Expression):
        return _calc_ev(node.body)

    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):                  # 先判 bool：True 也是 int
            raise _CalcError("真假值不算数")
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            # 关键：用 repr 而不是直接 Decimal(v)。
            # Decimal(0.1) 会吃进 float 的二进制误差得到
            # 0.1000000000000000055511…，而 Decimal(repr(0.1)) = 0.1。
            return Decimal(repr(v))
        raise _CalcError("算式里只能有数字")

    if isinstance(node, ast.Name):
        c = _CALC_CONSTS.get(node.id.lower())
        if c is None:
            raise _CalcError(f"不认识 {node.id} 这个名字")
        return Decimal(repr(c))

    if isinstance(node, ast.UnaryOp):
        v = _calc_ev(node.operand)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        raise _CalcError("不支持这种正负号写法")

    if isinstance(node, ast.BinOp):
        return _calc_binop(node.op, _calc_ev(node.left), _calc_ev(node.right))

    if isinstance(node, ast.Compare):
        left = _calc_ev(node.left)
        for op, comp in zip(node.ops, node.comparators):
            right = _calc_ev(comp)
            if not _calc_cmp(op, left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.Tuple):              # 给 min/max 这类多参数用
        return tuple(_calc_ev(e) for e in node.elts)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise _CalcError("不支持这种函数写法")
        name = node.func.id.lower()
        args = [_calc_ev(a) for a in node.args]
        if name in _CALC_FLOAT_FUNCS:
            if len(args) != 1:
                raise _CalcError(f"{name} 只要一个数")
            return _num_float(_CALC_FLOAT_FUNCS[name](float(args[0])))
        fn = _CALC_FUNCS.get(name)
        if fn is None:
            raise _CalcError(f"没有 {node.func.id} 这个函数")
        return fn(*args)

    raise _CalcError("算式里有小柚看不懂的东西")


def _calc_fmt(v) -> str:
    if isinstance(v, bool):
        return "成立" if v else "不成立"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Decimal):
        if not v.is_finite():
            return str(v)
        s = format(v, "f")                       # 定点展开，不出 1E+2 这种
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        # 循环小数（1/7、1/3）内部算了 40 位，显示到 16 位就够，
        # 加个省略号告诉模型"后面还有"，别让它当成精确值
        if "." in s:
            head, frac = s.split(".", 1)
            if len(frac) > CALC_MAX_FRAC:
                s = f"{head}.{frac[:CALC_MAX_FRAC]}…"
        return s or "0"
    if isinstance(v, tuple):
        return ", ".join(_calc_fmt(x) for x in v)
    return str(v)


def _safe_calc(expr: str) -> str:
    """算一个算式，永远返回一句人话（可能是"算不了"），绝不抛异常。"""
    expr = (expr or "").strip()
    if not expr:
        return "（没说算什么）"
    if len(expr) > CALC_MAX_EXPR:
        return "（算式太长了，小柚看不过来）"
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return ("（这个算式小柚看不懂，检查下括号和符号；"
                "百分数要写成 0.2 这样的小数）")
    try:
        with localcontext() as ctx:
            ctx.prec = CALC_PREC                # 给除法留够位数
            val = _calc_ev(tree)
    except _CalcError as e:
        return f"（算不了：{e}）"
    except ZeroDivisionError:
        return "（不能除以 0）"
    except (ArithmeticError, ValueError, OverflowError) as e:
        return f"（算不出来：{type(e).__name__}）"
    except RecursionError:
        return "（算式套得太深了）"
    except Exception as e:                       # 兜底：绝不能把整条消息拖哑
        logger.warning("计算器出错: %s", e)
        return f"（算不出来：{type(e).__name__}）"
    return f"{expr} = {_calc_fmt(val)}"


# ==========================================================================
# 工具箱
# ==========================================================================

class ToolRegistry:
    """工具的注册表和调度器。"""

    def __init__(self, cfg: dict):
        t = (cfg.get("tools") or {})
        self.enabled = bool(t.get("enabled", True))
        self.max_rounds = int(t.get("max_rounds", 3))
        self.max_tokens = int(t.get("max_tokens", 700))
        self.timeout = int(t.get("timeout", 12))
        self.search_provider = (t.get("search_provider") or "bing").lower()
        self.bocha_key = t.get("bocha_key") or ""
        self._session = _make_session()
        self._tools: dict[str, dict] = {}
        self._facts = FactStore(BASE_DIR / "memory" / "facts.json",
                                int(t.get("max_facts_per_user", 30)))
        self._register_all()

    # --- 注册 ---
    def _add(self, name, description, properties, required, func):
        self._tools[name] = {
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            },
            "func": func,
        }

    def _register_all(self):
        self._add(
            "web_search",
            "联网搜索。查实时信息、新闻、价格、天气、比赛结果、人物动态、"
            "或任何你不确定的事实，都必须用它，不许凭记忆瞎编。"
            "query 请给提炼过的【关键词组合】，不要直接丢一整句口语。",
            {"query": {"type": "string",
                       "description": "搜索关键词，如'2026年9月 上映 电影 口碑'，尽量具体"}},
            ["query"], self._web_search,
        )
        self._add(
            "read_url",
            "打开一个网页并读它的正文。主人发来链接、或搜索结果不够详细时用。",
            {"url": {"type": "string", "description": "完整网址，以 http 开头"}},
            ["url"], self._read_url,
        )
        self._add(
            "get_weather",
            "查询某个城市现在的天气和今明两天预报。",
            {"city": {"type": "string", "description": "城市名，如 北京、上海、宜宾"}},
            ["city"], self._get_weather,
        )
        self._add(
            "get_time",
            "获取当前真实日期和时间。问到'今天几号''现在几点'时用。",
            {}, [], self._get_time,
        )
        self._add(
            "calculate",
            "计算器。**凡是算术都必须用它**——加减乘除、比大小、百分比、"
            "打折、算天数、开方，一律把算式丢进来，绝不许自己心算。"
            "尤其**小数比大小**（比如 9.11 和 9.8 谁大、0.3 和 1/3 谁大），"
            "凭感觉答必错，必须用它。"
            "expression 里只写数字和 + - * / // % ** ( ) 和小数点；"
            "百分数写成小数（20%→0.2）；要比大小就直接写比较式。",
            {"expression": {"type": "string",
                            "description": "算式，如 '127*38'、'(1+0.6)*300'、'9.11 > 9.8'"}},
            ["expression"], self._calculate,
        )
        self._add(
            "remember",
            "把主人透露的、以后还用得上的个人信息永久记下来。"
            "比如名字、生日、爱好、忌口、过敏、正在忙的事、养了什么宠物。"
            "只记和主人本人有关的稳定事实，不要记闲聊和一次性的话。",
            {"content": {"type": "string",
                         "description": "要记住的事实，一句话，第三人称，如'对海鲜过敏'"}},
            ["content"], self._remember,
        )
        self._add(
            "get_trending_memes",
            "查现在网上正在流行什么（热榜）或某个梗的意思。"
            "主人问「最近有什么梗」「现在流行什么」时**不要**填 keyword；"
            "主人问「XX 是什么梗」「XX 什么意思」时把词填进 keyword。"
            "日常闲聊不需要用它——你手边已经有一份常用梗清单了，"
            "只有聊到你不认识的、或者想确认还流不流行时才用。",
            {"keyword": {"type": "string",
                         "description": "要查的梗或词，如'真嘟假嘟'。留空＝返回当前热榜"}},
            [], self._get_trending_memes,
        )

    # --- 对外接口 ---
    def schemas(self) -> list[dict]:
        if not self.enabled:
            return []
        return [t["schema"] for t in self._tools.values()]

    def facts_for(self, uid: str) -> list[str]:
        return self._facts.list(uid)

    def search(self, query: str) -> str:
        """给同项目其它模块用的搜索入口（梗库自动更新会调它）。

        对外这件事仍然是 `web_search` 工具；这个方法只是让内部调用方
        不必去碰 `_web_search` 那个私有实现。
        """
        return self._web_search({}, query)

    def call(self, name: str, args_json: str, ctx: dict) -> str:
        """执行工具。永远返回字符串，永远不抛异常。"""
        tool = self._tools.get(name)
        if not tool:
            return f"（没有叫 {name} 的工具）"
        try:
            args = json.loads(args_json) if args_json else {}
            if not isinstance(args, dict):
                args = {}
        except Exception:
            return f"（工具参数不是合法 JSON：{args_json[:120]}）"
        try:
            out = tool["func"](ctx, **args)
            out = (out or "").strip()
            return out or "（查到了，但内容是空的）"
        except requests.exceptions.Timeout:
            logger.warning("工具 %s 超时", name)
            return "（查询超时了，网络可能不太顺）"
        except requests.exceptions.ConnectionError:
            logger.warning("工具 %s 连不上", name)
            return "（连不上那个网站）"
        except Exception as e:
            logger.warning("工具 %s 出错: %s", name, e)
            return f"（查询出错：{type(e).__name__}）"

    # ------------------------------------------------------------------
    # web_search
    # ------------------------------------------------------------------
    def _web_search(self, ctx: dict, query: str = "", **_) -> str:
        query = (query or "").strip()
        if not query:
            return "（没给搜索词）"

        if self.search_provider == "bocha" and self.bocha_key:
            out = self._search_bocha(query)
            if out:
                return out
            logger.warning("博查搜索失败，回退通用通道")

        # 两个源并行抓，再合并去重。
        # 实测：必应结果干净但偏官方（搜"小米17价格"只给官网首页），
        # 360 更贴口语（同样一句给的是"售价4499元起"的新闻）。
        # 并行跑总延迟还是 0.3 秒左右，和单源差不多，没必要二选一。
        #
        # 合并一律**交替**（360 一条、必应一条…），不是"谁先回来谁先占位"。
        # 这条是踩出来的：问"什么是热梗"这类**元查询**时，必应返回的全是
        # 百科词条和日历，而它常常先回来 —— 前 8 条名额当场被垃圾占满，
        # 360 那 6 条真正的梗盘点全被挤出窗口，结果等于只有必应在答。
        # 交替能保证两个源都进得来。
        got: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {
                ex.submit(self._search_360, query): "360",
                ex.submit(self._search_bing, query): "bing",
            }
            for f in as_completed(futs):
                try:
                    got[futs[f]] = f.result() or []
                except Exception as e:
                    logger.warning("%s 搜索失败: %s", futs[f], e)
                    got[futs[f]] = []

        items: list[dict] = []
        a, b = got.get("360", []), got.get("bing", [])
        for i in range(max(len(a), len(b))):
            if i < len(a):
                items.append(a[i])
            if i < len(b):
                items.append(b[i])

        if not items:
            return self._search_sogou(query) or "（搜索没拿到结果，换个说法再试试）"
        return self._format_items(query, items)

    def _format_items(self, query: str, items: list[dict]) -> str:
        """把结构化结果拼成给模型看的文本，顺便按标题去重。

        窗口开到 12 条：两个源交替合并后，8 条太窄 —— 一侧占 6 条，
        另一侧就只剩 2 个位置，等于白抓。真正的长度闸门是
        `SEARCH_MAX_CHARS`，条数只是防呆。
        """
        seen, picked = set(), []
        for it in items:
            title = (it.get("title") or "").strip()
            key = re.sub(r"[\s\-_|·,，。、]+", "", title).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            picked.append(it)
            if len(picked) >= 12:
                break

        lines, used = [f"【{query}】的搜索结果："], 0
        for i, it in enumerate(picked, 1):
            src = it.get("url") or it.get("site") or ""
            desc = (it.get("desc") or "").strip()
            line = f"{i}. {it['title']}"
            if desc:
                line += f"\n   {desc}"
            if src:
                line += f"\n   来源: {src}"
            if used + len(line) > SEARCH_MAX_CHARS:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def _search_bing(self, query: str) -> list[dict]:
        r = self._session.get("https://cn.bing.com/search",
                              params={"q": query}, timeout=self.timeout)
        if r.status_code != 200:
            return []
        html = _text(r)
        # 用前瞻切块，比非贪婪更稳（结果块内部还嵌着别的 li）
        out = []
        for b in re.split(r'(?=<li class="b_algo")', html)[1:]:
            b = b.split("</li>")[0]
            m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not m:
                continue
            url = _unwrap_bing_url(unescape(m.group(1)))
            title = _clean_html(m.group(2)).replace("\n", " ")
            if not title or not url.startswith("http"):
                continue
            m2 = re.search(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', b, re.S)
            if not m2:
                m2 = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
            desc = _clean_html(m2.group(1)).replace("\n", " ") if m2 else ""
            out.append({"title": title, "url": url, "site": "", "desc": desc})
            if len(out) >= 6:
                break
        return out

    def _search_sogou(self, query: str) -> str:
        """搜狗：只在 360+必应都空的时候兜一下，所以返回拼好的文本。"""
        r = self._session.get("https://www.sogou.com/web",
                              params={"query": query}, timeout=self.timeout)
        if r.status_code != 200:
            return ""
        html = _text(r)
        items, seen = [], set()
        # 搜狗真实的 class 是 "vr-title  "（后面带空格），精确匹配会漏掉
        for m in re.finditer(
                r'<h3[^>]*class="[^"]*vr-title[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                html, re.S):
            href, title = unescape(m.group(1)), _clean_html(m.group(2)).replace("\n", " ")
            if not title or title in seen or "so靠谱" in title:
                continue
            seen.add(title)
            if href.startswith("/"):
                href = "https://www.sogou.com" + href
            items.append((title, href))
            if len(items) >= 6:
                break
        if not items:
            return ""
        out = [f"【{query}】的搜索结果（搜狗）："]
        for i, (t, u) in enumerate(items, 1):
            out.append(f"{i}. {t}\n   来源: {u}")
        return "\n".join(out)

    def _search_360(self, query: str) -> list[dict]:
        r = self._session.get("https://www.so.com/s",
                              params={"q": query}, timeout=self.timeout)
        if r.status_code != 200:
            return []
        html = _text(r)
        out = []
        for blk in re.split(r'(?=<li[^>]+class="[^"]*\bres-list\b)', html)[1:]:
            blk = blk.split("</li>")[0]
            m = re.search(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
            if not m:
                continue
            href = unescape(m.group(1))
            title = _clean_html(m.group(2)).replace("\n", " ")
            # 第一条经常是"关于XX，进一步探索更多相关内容"这种自家推广位
            if not title or "进一步探索" in title or "so靠谱" in title:
                continue
            # /s?q=... 是"相关搜索"，不是结果
            if not href.startswith("http"):
                continue

            # 360 把结果包成 so.com/link?m=<超长串>，而且它是 JS 跳转，
            # requests 跟不出真实地址（实测 200 且没有 Location）。
            # 好在它把域名直接写在 g-linkinfo 里了，用域名代替链接。
            site = ""
            ms = re.search(r'<p[^>]*class="[^"]*g-linkinfo[^"]*"[^>]*>(.*?)</p>', blk, re.S)
            if ms:
                site = _clean_html(ms.group(1)).replace("反馈", "").strip()
            url = "" if ("so.com/link" in href or "e.so.com" in href) else href

            desc = ""
            for pat in (r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>',
                        r'<p[^>]*class="[^"]*res-rich[^"]*"[^>]*>(.*?)</p>',
                        r'<div[^>]*class="[^"]*res-rich[^"]*"[^>]*>(.*?)</div>'):
                md = re.search(pat, blk, re.S)
                if md:
                    desc = _clean_html(md.group(1)).replace("\n", " ")
                    break
            out.append({"title": title, "url": url, "site": site, "desc": desc})
            if len(out) >= 8:
                break
        return out

    def _search_bocha(self, query: str) -> str:
        """博查 AI 搜索（可选，需自己去 https://open.bochaai.com 申请 Key）。

        专为喂给大模型设计，结果比扒 HTML 干净得多。
        """
        try:
            r = self._session.post(
                "https://api.bochaai.com/v1/web-search",
                json={"query": query, "summary": True, "count": 6},
                headers={"Authorization": f"Bearer {self.bocha_key}"},
                timeout=self.timeout,
            )
            if r.status_code != 200:
                return ""
            pages = (((r.json().get("data") or {}).get("webPages") or {})
                     .get("value") or [])
        except Exception:
            return ""
        out = [f"【{query}】的搜索结果："]
        for i, p in enumerate(pages[:6], 1):
            out.append(f"{i}. {p.get('name','')}\n   {p.get('summary') or p.get('snippet','')}"
                       f"\n   来源: {p.get('url','')}")
        return "\n".join(out)

    # ------------------------------------------------------------------
    # read_url
    # ------------------------------------------------------------------
    def _read_url(self, ctx: dict, url: str = "", **_) -> str:
        url = (url or "").strip()
        if not url.startswith("http"):
            return "（网址看起来不对，要以 http 开头）"

        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return "（只能读 http / https 的网页）"
        if _is_local_host(u.hostname or ""):
            logger.warning("拒绝读取本机/内网地址: %s", url)
            return "（这个地址指向本机或内网，小柚不去读）"

        r = self._session.get(url, timeout=self.timeout, stream=True,
                              headers={"Referer": "https://cn.bing.com/"})
        if r.status_code == 403:
            return "（这个网站不让小柚进，403了）"
        if r.status_code >= 400:
            return f"（打不开，HTTP {r.status_code}）"

        # 分块收，够了就停。不用 r.text 是因为它会把整个响应体一次性读进内存，
        # 遇到大文件直接吃爆 —— 而慢速吐字节的连接是不会触发超时的。
        chunks: list[bytes] = []
        total = 0
        try:
            for ch in r.iter_content(1 << 15):
                if not ch:
                    continue
                chunks.append(ch)
                total += len(ch)
                if total >= PAGE_MAX_BYTES:
                    logger.warning("网页超过 %d 字节，截断", PAGE_MAX_BYTES)
                    break
        finally:
            r.close()
        html = _decode(b"".join(chunks))
        # 标题
        mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = _clean_html(mt.group(1)) if mt else url

        # 优先取正文容器，取不到就退化成整页
        body = ""
        for pat in (
            r'(?is)<article[^>]*>(.*?)</article>',
            r'(?is)<div[^>]+(?:id|class)="[^"]*(?:article|content|main|post)[^"]*"[^>]*>(.*?)</div>\s*</div>',
            r"(?is)<main[^>]*>(.*?)</main>",
        ):
            m = re.search(pat, html)
            if m:
                cand = _clean_html(m.group(1))
                if len(cand) > len(body):
                    body = cand
        if len(body) < 200:
            body = _clean_html(html)

        if len(body) < 80:
            return f"（{title} 这个页面的正文抓不出来，可能是纯动态网页）"
        if len(body) > PAGE_MAX_CHARS:
            body = body[:PAGE_MAX_CHARS] + "\n…（后面还有内容，先看这些）"
        return f"标题：{title}\n网址：{url}\n正文：\n{body}"

    # ------------------------------------------------------------------
    # get_weather
    # ------------------------------------------------------------------
    def _get_weather(self, ctx: dict, city: str = "", **_) -> str:
        city = (city or "").strip() or "北京"
        r = self._session.get(f"https://wttr.in/{quote(city)}",
                              params={"format": "j1", "lang": "zh"},
                              timeout=self.timeout)
        if r.status_code != 200:
            return f"（查不到 {city} 的天气，换个城市名试试）"
        try:
            d = r.json()
        except Exception:
            return f"（{city} 的天气数据读不出来）"

        cur = (d.get("current_condition") or [{}])[0]

        def desc_of(node) -> str:
            """取天气描述并翻成中文。"""
            v = node.get("weatherDesc")
            en = (v[0] or {}).get("value", "") if isinstance(v, list) and v else ""
            return _weather_zh(en)

        lines = [
            f"{city} 天气：",
            f"- 现在：{desc_of(cur)}，{cur.get('temp_C','?')}°C"
            f"（体感 {cur.get('FeelsLikeC','?')}°C），"
            f"湿度 {cur.get('humidity','?')}%，"
            f"{cur.get('winddir16Point','')}风 {cur.get('windspeedKmph','?')} km/h",
        ]
        days = d.get("weather") or []
        names = ["今天", "明天", "后天"]
        for i, w in enumerate(days[:3]):
            desc = ""
            hrs = w.get("hourly") or []
            if hrs:
                desc = desc_of(hrs[len(hrs) // 2])
            lines.append(
                f"- {names[i] if i < len(names) else w.get('date','')}："
                f"{desc}，{w.get('mintempC','?')}~{w.get('maxtempC','?')}°C"
            )
        lines.append("（数据来源：wttr.in）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # get_time
    # ------------------------------------------------------------------
    def _get_time(self, ctx: dict, **_) -> str:
        now = time.localtime()
        week = "一二三四五六日"[now.tm_wday]
        return (f"现在是 {now.tm_year} 年 {now.tm_mon} 月 {now.tm_mday} 日 "
                f"星期{week} {now.tm_hour:02d}:{now.tm_min:02d}")

    # ------------------------------------------------------------------
    # calculate
    # ------------------------------------------------------------------
    def _calculate(self, ctx: dict, expression: str = "", **_) -> str:
        """精确算一个算式。返回 "算式 = 结果" 这样一行纯文本。"""
        return _safe_calc(str(expression or ""))

    # ------------------------------------------------------------------
    # remember
    # ------------------------------------------------------------------
    def _remember(self, ctx: dict, content: str = "", **_) -> str:
        uid = str(ctx.get("uid") or "")
        content = (content or "").strip()
        if not uid or not content:
            return "（没记成）"
        added = self._facts.add(uid, content)
        return f"记住了：{content}" if added else f"这个已经记过了（{content}）"

    # ------------------------------------------------------------------
    # get_trending_memes
    # ------------------------------------------------------------------
    def _get_trending_memes(self, ctx: dict, keyword: str = "", **_) -> str:
        keyword = str(keyword or "").strip()
        if keyword:
            # 查某个具体的梗。带上"梗"字 —— "真嘟假嘟"这类谐音词不加提示会被
            # 切成"真"+"嘟假嘟"，实测返回一堆"真"字的字典释义（百度百科、
            # 汉语国学），完全跑偏。
            return self._web_search(ctx, f"{keyword} 是什么梗")

        # 不指定词：找最新的热梗盘点。
        #
        # 这里**刻意不用热搜榜**。实测百度/抖音热搜里大半是新闻 —— 政要来访、
        # 讣闻、案件、外交礼炮，混合比例太高。机器过滤（词表 + 长度）只能挡住
        # 一部分，漏进聊天里非常难看，而且词表永远追不上当天的新闻。
        # 换成搜"热梗盘点"：拿到的就是梗本身、连解释一起，既准又没有翻车风险。
        now = time.localtime()
        return self._web_search(
            ctx, f"{now.tm_year}年{now.tm_mon}月 网络热梗 流行语 盘点")


# ==========================================================================
# 主人的事 · 永久记忆
# ==========================================================================

class FactStore:
    """按 QQ 号存"主人的事"，落盘成 JSON。

    和 Memory（最近 20 轮对话）不是一回事：
      Memory     —— 短期上下文，会滚掉，用来接住"刚才那句"
      FactStore  —— 精选的长期事实，不滚，用来接住"主人对海鲜过敏"

    只在小柚主动调用 remember 时才写入，所以里面不会有闲聊垃圾。
    """

    def __init__(self, path: Path, max_per_user: int = 30):
        self.path = path
        self.max_per_user = max_per_user
        self._lock = threading.Lock()
        self._data: dict[str, list[str]] = {}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {str(k): list(v) for k, v in raw.items()
                                  if isinstance(v, list)}
        except Exception as e:
            logger.warning("长期记忆读取失败: %s", e)
            self._data = {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_replace_text(
                self.path, json.dumps(self._data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning("长期记忆写入失败: %s", e)

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[\s，。,.、！!？?~～]+", "", s).lower()

    def add(self, uid: str, fact: str) -> bool:
        fact = fact.strip()[:120]
        if not fact:
            return False
        n = self._norm(fact)
        with self._lock:
            lst = self._data.setdefault(uid, [])
            for old in lst:
                o = self._norm(old)
                # 完全一样，或一方包含另一方（"喜欢猫" vs "喜欢猫咪"）→ 视为重复
                if n == o or (len(n) > 3 and (n in o or o in n)):
                    return False
            lst.append(fact)
            if len(lst) > self.max_per_user:
                del lst[: len(lst) - self.max_per_user]
            self._save()
        return True

    def list(self, uid: str) -> list[str]:
        with self._lock:
            return list(self._data.get(str(uid), []))

    def all(self) -> dict:
        with self._lock:
            return {k: list(v) for k, v in self._data.items()}
