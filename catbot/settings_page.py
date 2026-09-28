# -*- coding: utf-8 -*-
"""
小柚 · 设置台（本地可视化配置页）
==================================

    python settings_page.py              打开设置页（浏览器自动弹出）
    python settings_page.py --snapshot   导出一份脱敏的静态预览 HTML

设计要求（都是踩过的坑倒逼出来的）：

1. **零新依赖** —— 只用标准库 http.server，不引入 Flask。
   设置页不该成为机器人能不能跑的前提。

2. **只改点过的字段**（最小写回）
   读 config.json 的**原始内容**，在上面套用改动、原子写回。
   绝不把"默认值 merge 后的完整配置"写回去 —— 那会把用户没配过的
   几十个字段全糊进文件，从此再也分不清哪些是手写的。

3. **schema 是白名单**
   前端提交的是 {"llm.temperature": 0.9, ...}。后端**只接受 SCHEMA 里
   存在的路径**，其余一律拒绝。这样即使有人手动构造请求，也写不进
   任意键、改不了别的文件。

4. **密钥不回传明文**
   GET /api/config 遇到 secret 字段返回 null，只给一个脱敏提示；
   要看真值得单独调 /api/reveal（同样是本机 + token）。

5. **只绑 127.0.0.1 + 随机 token + 校验 Host**
   浏览器里的任意网页都能向 127.0.0.1 发请求（DNS rebinding / CSRF）。
   Host 校验挡 rebinding，token 挡跨站读取，两层都上。

6. **校验在服务端再走一遍**
   前端已经拦了一次（数字范围、格式），但服务端必须独立校验 ——
   页面可以被绕过，配置写坏了是机器人启动就崩。
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import secrets
import socket
import sys
import ast
import contextlib
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="gbk", errors="replace", line_buffering=True)
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
TEMPLATE_FILE = BASE_DIR / "settings.html"
LOG_DIR = BASE_DIR / "logs"
PORT_START = 8765
PORT_TRIES = 12
MAX_BODY = 1 << 20          # 请求体上限 1MB，正常请求只有几 KB


# ==========================================================================
# 1. 配置项定义（这张"表"的骨架）
# ==========================================================================

def F(path, name, desc, type_, default, group, **kw):
    d = {"path": path, "name": name, "desc": desc, "type": type_,
         "default": default, "group": group}
    d.update(kw)
    return d


def edge_voice_options() -> list[dict]:
    """从 voice.py 抽 EDGE_VOICES，翻译成下拉选项。

    不 import voice —— 那会拖进 requests/edge_tts 一整条链，设置页就没法在
    "机器人还没配好"的时候打开了。只用 ast 把那个 dict 字面量读出来。
    这样加音色只需要改 voice.py 一处，设置页自动跟上，不会两边漂移。
    """
    fallback = [{"v": "zh-CN-XiaoyiNeural", "t": "晓伊"}]
    try:
        src = (BASE_DIR / "voice.py").read_text(encoding="utf-8")
        m = re.search(r"^EDGE_VOICES[^=\n]*=\s*(\{.*?^\})", src, re.S | re.M)
        if not m:
            return fallback
        d = ast.literal_eval(m.group(1))
        out = [{"v": k, "t": f"{v[0]} —— {v[1]}"} for k, v in d.items()]
        return out or fallback
    except Exception as e:
        _log("EDGE_VOICES 抽取失败，用兜底音色: %s" % e)
        return fallback


GROUPS = [
    {"id": "llm",        "title": "模型与对话",   "icon": "🧠", "desc": "她用什么脑子说话"},
    {"id": "reply",      "title": "说话方式",     "icon": "💬", "desc": "一次说多少、隔多久回"},
    {"id": "coalesce",   "title": "别抢话",       "icon": "⏳", "desc": "等对方把话说完再回"},
    {"id": "trigger",    "title": "什么时候理你", "icon": "📣", "desc": "群里要 @ 吗、谁能用"},
    {"id": "memory",     "title": "记忆",         "icon": "📚", "desc": "记不记得住上文"},
    {"id": "tools",      "title": "联网手脚",     "icon": "🔍", "desc": "搜资料 / 读网页 / 查天气 / 算数"},
    {"id": "corpus",     "title": "梗库自动更新", "icon": "🌐", "desc": "她自己上网补新梗"},
    {"id": "voice",      "title": "耳朵和嗓子",   "icon": "🔊", "desc": "听语音、发语音、换音色"},
    {"id": "vision",     "title": "眼睛",         "icon": "👀", "desc": "看得懂图"},
    {"id": "proactive",  "title": "主动早安",     "icon": "☀️", "desc": "每天定时冒个泡"},
    {"id": "nudge",      "title": "冷场暖场",     "icon": "🌙", "desc": "没人说话时她起个头"},
    {"id": "rate_limit", "title": "防刷屏",       "icon": "🚦", "desc": "限速与并发"},
    {"id": "watch",      "title": "自动重启",     "icon": "♻️", "desc": "改完自动生效 / 卡住自己爬起来"},
    {"id": "notify",     "title": "掉线提醒",     "icon": "🔔", "desc": "她掉线时弹窗告诉你"},
    {"id": "conn",       "title": "连接（高级）", "icon": "🔌", "desc": "端口与人设文件，一般不用动"},
]

SCHEMA = [
    # ---------------- 模型与对话 ----------------
    F("llm.provider", "推理位置", "她的大脑在哪跑。云端更快更聪明（要 Key），本地免费（要显卡）。",
      "select", "ollama", "llm",
      options=[{"v": "openai", "t": "云端 API（DeepSeek 等）"},
               {"v": "ollama", "t": "本地 Ollama"}]),
    F("llm.model", "模型名", "用哪个模型。云端填服务商的模型名，本地填 ollama 里的名字。",
      "text", "qwen2.5:7b-instruct", "llm"),
    F("llm.api_base", "接口地址", "云端接口的地址。DeepSeek 是 https://api.deepseek.com/v1",
      "text", "", "llm", advanced=True, hint="只有云端才要"),
    F("llm.api_key", "接口密钥", "云端密钥。填了就存在本机 config.json 里，别把文件发给别人。",
      "secret", "", "llm", advanced=True),
    F("llm.ollama_url", "本地服务地址", "本地 Ollama 的地址。没改过端口就别动。",
      "text", "http://127.0.0.1:11434", "llm", advanced=True),
    F("llm.temperature", "发挥度", "小＝稳重复读，大＝跳脱有惊喜。0.8 左右最像真人。",
      "float", 0.85, "llm", min=0, max=2, step=0.05),
    F("llm.max_tokens", "回复长度上限", "单次回复最多生成多少 token。太小会把话切断。",
      "int", 220, "llm", min=60, max=2000, step=10, unit="token"),
    F("llm.timeout", "等待超时", "等模型回答的最长时间，超了就报错。",
      "int", 120, "llm", min=10, max=600, step=5, unit="秒"),
    F("llm.reasoning_effort", "思考档位", "关掉反应最快；打开更聪明但更慢、更费钱。",
      "select", "none", "llm",
      options=[{"v": "none", "t": "关（最快）"}, {"v": "low", "t": "低"},
               {"v": "medium", "t": "中"}, {"v": "high", "t": "高（最慢）"}]),

    # ---------------- 说话方式 ----------------
    F("reply.max_chars", "日常字数上限", "平时一次最多说多少字。太长就不像聊天了。",
      "int", 180, "reply", min=40, max=1000, step=10, unit="字"),
    F("reply.task_max_chars", "干活时字数上限", "查资料、讲正经事时可以更长一点。",
      "int", 600, "reply", min=100, max=3000, step=50, unit="字"),
    F("reply.split_threshold", "分条阈值", "超过多少字就拆成几条发，像真人分段。",
      "int", 120, "reply", min=40, max=600, step=10, unit="字"),
    F("reply.at_sender_in_group", "群里回复时 @ 人", "在群里回话时顺手 @ 一下对方，不容易漏看。",
      "bool", True, "reply"),
    F("reply.typing_delay", "打字延迟", "收到消息后等多久再回（在区间里随机取），显得像在打字。",
      "pair", [0.8, 2.2], "reply", min=0.5, max=5, step=0.1, unit="秒",
      labels=["最少", "最多"]),
    F("reply.on_error", "出错时说的话", "模型挂了 / 超时了，她替你回这一句。",
      "text", "（小柚的脑袋卡住了…主人稍等一下喵）", "reply"),
    F("reply.chunk.enabled", "一句一句发", "开着更像真人打字，一句一句往外蹦；关掉就整条一次发完。",
      "bool", True, "reply"),
    F("reply.chunk.gap", "句间停顿", "两条之间的间隔（在这区间里随机取）。想更从容就拉大。",
      "pair", [0.8, 2.6], "reply", min=0.2, max=15, step=0.1, unit="秒",
      labels=["最少", "最多"]),
    F("reply.chunk.gap_jitter", "停顿抖动", "在随机停顿上再加一点随机，避免节奏规律得像机器。",
      "float", 0.35, "reply", min=0, max=3, step=0.05, unit="秒"),
    F("reply.chunk.min_chars", "过短的句子并入上句", "短于这个字数（如「嗯。」）就并进上一句，不然像卡带。",
      "int", 6, "reply", min=1, max=30, step=1, unit="字"),
    F("reply.chunk.max_chunks", "最多分几句", "超过的会并到最后一句，防止把一句话拆成刷屏。",
      "int", 6, "reply", min=1, max=20, step=1, unit="句"),
    F("reply.chunk.per_char", "长句多等一会", "上一句越长，下一句准备得越久（每字秒数）。",
      "float", 0.012, "reply", min=0, max=0.1, step=0.002, unit="秒/字"),
    F("reply.chunk.per_char_cap", "长句加成上限", "按字数加的等待最多加这么多秒。",
      "float", 0.8, "reply", min=0, max=5, step=0.1, unit="秒"),
    F("reply.chunk.long_para", "长段落不拆", "单句超过这个字数就整句发，长段落拆开读着是断的。",
      "int", 60, "reply", min=20, max=300, step=10, unit="字"),
    F("reply.chunk.only_short_mode", "查资料时整条发", "认真模式（报告、长结论）不分句，免得读着散。",
      "bool", True, "reply"),

    # ---------------- 别抢话（等对方把话说完）----------------
    F("coalesce.enabled", "别抢话", "开着的话，对方把一段话拆成几条连发时，她等他说完再一起回。",
      "bool", True, "coalesce"),
    F("coalesce.quiet", "等多久算说完", "最后一条之后安静这么久，就当对方说完了。太小仍会抢话，太大会显得迟钝。",
      "float", 1.8, "coalesce", min=0.3, max=10, step=0.1, unit="秒"),
    F("coalesce.quiet_max", "最长等多久", "对方一直在发、静默等不到时，最多等这么久就强制回，别晾着。",
      "float", 4.0, "coalesce", min=1, max=30, step=0.5, unit="秒"),
    F("coalesce.max_parts", "最多并几条", "攒到这么多条就先回一批，免得被一句话没完的话卡住。",
      "int", 5, "coalesce", min=1, max=20, step=1, unit="条"),
    F("coalesce.private", "私聊生效", "一对一聊天最容易遇到连发，建议开。",
      "bool", True, "coalesce"),
    F("coalesce.group", "群聊生效", "群里人多嘴杂，可能要等很久才安静 —— 默认关。",
      "bool", False, "coalesce"),
    F("coalesce.reset_on_reply", "她回完就清空", "她刚回过话就把攒的旧话清掉，避免把上一次的内容并进来。",
      "bool", True, "coalesce"),

    # ---------------- 什么时候理你 ----------------
    F("trigger.group_at", "群里要 @ 才理", "开着的话，群里只有 @ 她 或者提到关键词才说话。",
      "bool", True, "trigger"),
    F("trigger.group_keywords", "群里关键词", "消息里出现这些词也会理她（一行一个）。",
      "list", ["小柚"], "trigger"),
    F("trigger.private", "私聊直接回", "私聊不用 @，看到就回。",
      "bool", True, "trigger"),
    F("trigger.ignore_self", "不理自己发的", "防止她对着自己的消息自问自答。",
      "bool", True, "trigger"),
    F("whitelist.groups", "只理这些群", "填群号，一行一个。留空 = 所有群都理。",
      "list", [], "trigger"),
    F("whitelist.users", "只理这些人", "填 QQ 号，一行一个。留空 = 所有人都理。",
      "list", [], "trigger"),

    # ---------------- 记忆 ----------------
    F("memory.enabled", "开启记忆", "关掉她就完全记不住刚才聊过什么，每条消息都是新对话。",
      "bool", True, "memory"),
    F("memory.max_turns", "记住几轮", "带着最近多少轮对话去问她。越大越连贯，也越费 token。",
      "int", 12, "memory", min=2, max=60, unit="轮"),

    # ---------------- 联网手脚 ----------------
    F("tools.enabled", "开启联网能力", "关掉她就只会聊天：不能搜资料、读网页、查天气、记事、算数。",
      "bool", True, "tools"),
    F("tools.search_provider", "搜索通道", "必应 + 360 不用 Key 直接能用；博查要自己申请 Key。",
      "select", "bing", "tools",
      options=[{"v": "bing", "t": "必应 + 360（免 Key）"},
               {"v": "bocha", "t": "博查 AI（需 Key）"}]),
    F("tools.bocha_key", "博查密钥", "只有搜索通道选「博查 AI」时才需要填。",
      "secret", "", "tools"),
    F("tools.max_rounds", "最多查几轮", "一次提问里最多连续查几轮资料，防止她查到停不下来。",
      "int", 3, "tools", min=1, max=6, unit="轮"),
    F("tools.max_tokens", "资料回答预算", "查完资料后写答案的字数预算，比日常闲聊宽松。",
      "int", 700, "tools", min=200, max=2000, step=50, unit="token"),
    F("tools.timeout", "联网超时", "单次搜索 / 抓网页的最长等待时间。",
      "int", 12, "tools", min=5, max=60, unit="秒"),
    F("tools.max_facts_per_user", "每人最多记几条", "长期记忆（记你喜好之类的）每人最多留多少条。",
      "int", 30, "tools", min=5, max=200, step=5, unit="条"),

    # ---------------- 梗库自动更新 ----------------
    F("corpus.enabled", "自动更新梗库", "开着她就定期上网把新梗补进 memes.md 的「自动区」。"
      "你自己手写的梗永远不会被碰，也不会被覆盖。",
      "bool", True, "corpus"),
    F("corpus.refresh_days", "多久抓一次", "梗的流行周期是周级别的，天天抓意义不大。",
      "int", 7, "corpus", min=1, max=90, unit="天"),
    F("corpus.max_items", "自动区保留几条", "只留最近抓到的，超出的淘汰最旧的。"
      "留太多会稀释她挑梗的准头。",
      "int", 24, "corpus", min=4, max=80, step=2, unit="条", advanced=True),

    # ---------------- 耳朵和嗓子 ----------------
    F("voice.enabled", "开启语音", "能不能听语音消息、能不能发语音。关掉就纯打字。",
      "bool", True, "voice"),
    F("voice.speak_probability", "随机发语音概率", "每句回复变成语音的概率。0＝从不，1＝每次都发。",
      "float", 0.15, "voice", min=0, max=1, step=0.05),
    F("voice.speak_on", "必发语音的词", "说到这些词一定发语音（一行一个）。",
      "list", ["晚安", "喵呜~", "喜欢你", "想你", "抱抱"], "voice"),
    F("voice.max_chars", "语音长度上限", "超过这个字数的回复只打字、不念出来——长话念着累。",
      "int", 55, "voice", min=10, max=200, step=5, unit="字"),
    F("voice.rate", "系统语音语速", "只有用「系统语音（离线）」时才生效。1 是正常，小于 1 更慢。",
      "float", 1, "voice", min=0.5, max=2, step=0.1),
    F("voice.tts_provider", "用哪个嗓子",
      "自动＝本机 GPT-SoVITS → Edge 神经网络音色 → 系统语音，逐级兜底。"
      "选了也不是“只认一个” —— 哪一级不通就往下退，保证她不会变成哑巴。",
      "select", "auto", "voice",
      options=[{"v": "auto", "t": "自动（推荐）"},
               {"v": "gptsovits", "t": "本机 GPT-SoVITS（优先）"},
               {"v": "edge", "t": "Edge TTS（联网）"},
               {"v": "sapi", "t": "只用系统语音（离线）"}]),
    F("voice.edge_voice", "音色", "Edge 的中文音色。晓伊是活泼少女、最贴小柚；晓北带东北味、晓妮带陕西味。",
      "select", "zh-CN-XiaoyiNeural", "voice", options=edge_voice_options()),
    F("voice.edge_rate", "语速微调", "在音色自带语速上再调。正值更快、听着更雀跃。",
      "int", 8, "voice", min=-50, max=100, step=1, unit="%"),
    F("voice.edge_pitch", "音调微调", "正值更尖更幼（萝莉感），负值更低沉。想更软萌就往上调。",
      "int", 10, "voice", min=-50, max=50, step=1, unit="Hz"),
    F("voice.edge_volume", "音量微调", "一般不用动。",
      "int", 0, "voice", min=-50, max=50, step=1, unit="%", advanced=True),

    # ---- 本机 GPT-SoVITS（离线、可克隆音色）----
    F("voice.gptsovits_url", "本机语音服务地址", "本机 GPT-SoVITS 服务的地址，没改过端口就别动。",
      "text", "http://127.0.0.1:9880", "voice", advanced=True),
    F("voice.gptsovits_ref_audio", "参考音色文件",
      "决定她像谁、什么调子。留空＝用 参考音色/小柚_默认参考.wav。"
      "换成你自己录的 3~10 秒干净单人音频，就是专属音色。",
      "text", "", "voice", hint="留空即用默认"),
    F("voice.gptsovits_ref_text", "参考音色的文本",
      "参考音频里逐字念的内容，必须和音频对得上，否则会念飘。留空＝读同名 .txt。",
      "text", "", "voice"),
    F("voice.gptsovits_ref_lang", "参考音色语种", "参考音频说的是什么话。中文填 zh。",
      "select", "zh", "voice",
      options=[{"v": "zh", "t": "中文"}, {"v": "en", "t": "英语"},
               {"v": "ja", "t": "日语"}, {"v": "yue", "t": "粤语"}]),
    F("voice.gptsovits_speed", "语速倍率", "1.0 是原速；调小更慢更柔，调大更雀跃。",
      "float", 1.0, "voice", min=0.5, max=2.0, step=0.05),
    F("voice.gptsovits_temperature", "发挥度",
      "情绪起伏的幅度。调大更有戏，也更容易念飘；1.0 是稳妥值。",
      "float", 1.0, "voice", min=0.5, max=1.5, step=0.05),
    F("voice.gptsovits_split", "长句切分",
      "cut0 不切句（短句最连贯，推荐）；cut5 按标点切成多段再拼起来。",
      "select", "cut0", "voice", advanced=True,
      options=[{"v": "cut0", "t": "不切（推荐）"},
               {"v": "cut2", "t": "按标点切"},
               {"v": "cut5", "t": "按句子切（官方默认）"}]),
    F("voice.gptsovits_loudness", "响度归一",
      "她说话的音量。本机克隆的原始输出偏轻（比 Edge 轻 8~10 dB），"
      "这里填目标响度（LUFS，**数值越小越响**）：−16 是语音常用值，"
      "想和 Edge 完全一样响就填 −21，填 0 ＝ 关掉归一用原始音量。",
      "float", -16.0, "voice", min=-30.0, max=0.0, step=1.0, unit="LUFS"),

    F("voice.group_enabled", "群里也发语音", "默认只在私聊发语音，群里安静一点比较好。",
      "bool", False, "voice"),

    # ---------------- 眼睛 ----------------
    F("vision.enabled", "开启看图", "能不能看懂你发的图片。要一个支持视觉的模型。",
      "bool", True, "vision"),
    F("vision.model", "看图用的模型", "处理图片时单独用这个模型（要支持视觉）。",
      "text", "deepseek-chat", "vision"),
    F("vision.max_images", "最多看几张", "一条消息里最多处理几张图，防止刷屏烧钱。",
      "int", 3, "vision", min=1, max=10, unit="张"),

    # ---------------- 主动早安 ----------------
    F("proactive.enabled", "开启主动消息", "总开关：管早安，也管下面的冷场暖场。关掉她就只被动回话。",
      "bool", True, "proactive"),
    F("proactive.owner_qq", "主人 QQ 号", "早安发给他。留空就从记忆里推断（可能推断错）。",
      "text", "", "proactive", hint="如 10001"),
    F("proactive.greeting_time", "早安时间", "每天几点说早安。她说完就去忙别的了。",
      "time", "08:30", "proactive"),
    F("proactive.city", "所在城市", "填了早安才会顺带报天气；不填只能报时间。",
      "text", "", "proactive", hint="如 北京"),

    # ---------------- 冷场暖场 ----------------
    F("proactive.nudge.enabled", "开启冷场暖场", "长时间没人说话时，她自己起个头把话接回来。",
      "bool", True, "nudge"),
    F("proactive.nudge.silence_min", "静默多久算冷场", "想更积极改小（比如 20），想安静改大。",
      "int", 45, "nudge", min=5, max=600, unit="分"),
    F("proactive.nudge.silence_max", "超过多久就不打扰", "静默太久就默认大家散了，不再冒泡。",
      "int", 240, "nudge", min=30, max=1440, unit="分"),
    F("proactive.nudge.cooldown", "两次暖场的间隔", "同一个会话里，两次主动开口至少隔这么久。",
      "int", 40, "nudge", min=5, max=600, unit="分"),
    F("proactive.nudge.private_max_per_day", "私聊每天最多", "0 = 私聊完全不主动。",
      "int", 3, "nudge", min=0, max=20, unit="次"),
    F("proactive.nudge.group_enabled", "群里也暖场", "群里也允许她起头。默认开着，但限制更严。",
      "bool", True, "nudge"),
    F("proactive.nudge.group_silence_min", "群里静默多久才开口", "群里比私聊更克制，门槛更高。",
      "int", 60, "nudge", min=10, max=600, unit="分"),
    F("proactive.nudge.group_max_per_day", "每个群每天最多", "0 = 群里完全不主动。",
      "int", 1, "nudge", min=0, max=10, unit="次"),
    F("proactive.nudge.quiet_hours", "免打扰时段", "这段时间绝对不主动开口（睡觉时间）。可以跨午夜。",
      "timerange", ["23:30", "09:00"], "nudge", labels=["从", "到"]),
    F("proactive.nudge.after_greet_grace", "早安后的静默期", "刚说完早安，这么久之内不再暖场，免得一天两条。",
      "int", 3600, "nudge", min=0, max=7200, step=300, unit="秒"),

    # ---------------- 防刷屏 ----------------
    F("rate_limit.per_session_cooldown", "同一人的冷却", "同一个人两次请求之间至少要隔多久。",
      "int", 3, "rate_limit", min=0, max=60, unit="秒"),
    F("rate_limit.global_per_minute", "全局每分钟上限", "所有会话加起来，一分钟最多处理多少条。",
      "int", 20, "rate_limit", min=1, max=120, unit="条/分"),
    F("rate_limit.max_concurrent", "同时处理数", "同时在跑的请求上限，太高容易把机器压住。",
      "int", 2, "rate_limit", min=1, max=10, unit="个"),

    # ---------------- 自动重启 ----------------
    # 这两组是给守护进程看的（daemon.py 每轮重读，改完不用重启守护进程）。
    # 人设文件 persona.md / memes.md 不在监听范围内 —— 它们本来就存盘即生效。
    F("watch.enabled", "改完自动重启", "改完 bot.py / tools.py / voice.py / config.json 存盘，"
      "它自己重启让改动生效，不用再点「保存并重启」。",
      "bool", True, "watch"),
    F("watch.interval_sec", "多久扫一次改动", "隔多久看一眼那几个文件的修改时间。",
      "int", 2, "watch", min=1, max=60, unit="秒", advanced=True),
    F("watch.settle_sec", "改完等多久才动手", "编辑器一次存盘常连着写好几个文件，"
      "等它停稳再重启，省得多起一次。",
      "int", 3, "watch", min=0, max=60, unit="秒", advanced=True),
    F("watch.cooldown_sec", "两次重启的最小间隔", "连着改文件时，两次自动重启至少隔这么久。",
      "int", 20, "watch", min=0, max=600, unit="秒", advanced=True),
    F("health.enabled", "卡死自动重启", "进程还在、却不回话（线程池卡住或掉线）时，"
      "自动重启把她救回来。",
      "bool", True, "watch"),
    F("health.interval_sec", "多久体检一次", "体检就是看一眼她写的心跳文件，很轻。",
      "int", 20, "watch", min=5, max=600, unit="秒", advanced=True),
    F("health.hb_stale_sec", "心跳多久没动算僵死", "超过这个时间没写心跳，就当整个进程冻住了。",
      "int", 90, "watch", min=30, max=900, step=10, unit="秒", advanced=True),
    F("health.busy_sec", "一条消息最多算多久", "单条消息处理超过这个时长就认定卡死。"
      "查资料 + 多轮工具调用偶尔会久，别调得太小。",
      "int", 600, "watch", min=60, max=3600, step=60, unit="秒", advanced=True),
    F("health.ws_down_sec", "断连多久算异常", "和协议层的连接断了这么久还没连上，就重启一次。",
      "int", 300, "watch", min=60, max=1800, step=30, unit="秒", advanced=True),
    F("health.grace_sec", "刚启动的豁免期", "刚起来的她别急着审判，给这么久宽限。",
      "int", 120, "watch", min=0, max=600, step=10, unit="秒", advanced=True),
    F("health.max_per_hour", "一小时最多重启几次", "防重启风暴：超过就先停手，"
      "等 daemon.log 里报的原因解决了再说。",
      "int", 6, "watch", min=1, max=30, unit="次", advanced=True),

    # ---------------- 掉线提醒 ----------------
    F("notify.enabled", "掉线时提醒我", "被腾讯踢下线之后，**必须有人扫一次码**她才回得了话。"
      "在那之前她一句话都说不了 —— 开这个，她卡在登录超过 2 分钟就弹窗告诉你。",
      "bool", True, "notify"),
    F("notify.popup", "弹窗（关掉只记日志）", "关掉就完全不打扰，只写进日志和状态面板 —— "
      "适合你本来就常看状态面板的情况。",
      "bool", True, "notify"),
    F("notify.sound", "弹窗时响一声", "配合弹窗的提示音。嫌吵就关掉，弹窗还在。",
      "bool", True, "notify"),
    F("notify.min_interval_min", "两次提醒的最小间隔", "避免连着弹。"
      "一次掉线最多提醒两条（首次 + 跨过免打扰时段后的补充）。",
      "int", 30, "notify", min=5, max=600, step=5, unit="分钟", advanced=True),
    F("notify.quiet_hours", "免打扰时段", "这段时间**绝对不弹窗**，等时段过了再提醒你。"
      "而被踢最频繁的恰恰是半夜，所以这个默认和冷场暖场一样。",
      "timerange", ["23:30", "09:00"], "notify", labels=["从", "到"]),

    # ---------------- 连接（高级） ----------------
    F("napcat.ws_url", "事件通道地址", "NapCat 推送消息的 WebSocket 地址。",
      "text", "ws://127.0.0.1:3001", "conn", advanced=True),
    F("napcat.http_url", "发送通道地址", "NapCat 收消息、发消息的 HTTP 地址。",
      "text", "http://127.0.0.1:3000", "conn", advanced=True),
    F("napcat.access_token", "NapCat 令牌", "NapCat 开了鉴权才要填，没开就留空。",
      "secret", "", "conn", advanced=True),
    F("persona_file", "人设文件", "她的性格写在这个文件里。改成别的名字就换一套性格。",
      "text", "persona.md", "conn", advanced=True),
    F("memes_file", "梗库文件", "热梗清单放这儿。想给她换一批梗就改这个文件，存盘即生效。",
      "text", "memes.md", "conn", advanced=True),
]

SCHEMA_BY_PATH = {it["path"]: it for it in SCHEMA}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 校验时表示"这一项跳过，不写"（密钥留空 = 保持原样）
_SKIP = object()


# ==========================================================================
# 2. 读写与校验
# ==========================================================================

def _log(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "settings.log", "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def load_raw() -> dict:
    """读 config.json 的**原始内容**（不 merge 默认值）。"""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log("config.json 解析失败: %s" % e)
        raise


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def defaults() -> dict:
    """从 bot.py 抽 DEFAULT_CONFIG，失败则退回 SCHEMA 里写的默认值。

    不 import bot —— 那会把 requests/websocket/voice 整条依赖链拖进来，
    设置页就没法在"机器人还没配好"的时候打开了。

    注意用 ast.literal_eval 而不是 json.loads：DEFAULT_CONFIG 是 **Python
    字面量**，段落里带着 `#` 注释（比如"冷场暖场看静默了多久"），
    json.loads 会在第一个注释处直接报错。这条曾经被 try/except 吞掉，
    表现是"默认值永远走兜底"，直到测试把它揪出来。
    """
    try:
        src = (BASE_DIR / "bot.py").read_text(encoding="utf-8")
        m = re.search(r"^DEFAULT_CONFIG\s*=\s*(\{.*?^\})\s*$", src, re.S | re.M)
        if m:
            data = ast.literal_eval(m.group(1))
            if isinstance(data, dict) and data:
                return data
    except Exception as e:
        _log("DEFAULT_CONFIG 抽取失败，用 SCHEMA 默认值: %s" % e)
    out: dict = {}
    for it in SCHEMA:
        _set_in(out, it["path"], copy.deepcopy(it["default"]))
    return out


def effective() -> dict:
    """当前生效的完整配置 = 默认值 + 用户配置。"""
    try:
        return _deep_merge(defaults(), load_raw())
    except Exception:
        return _deep_merge(defaults(), {})


def get_in(data: dict, path: str, default=None):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_in(data: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def mask_secret(v) -> str:
    s = str(v or "")
    if not s:
        return ""
    if len(s) <= 6:
        return "*" * len(s)
    if len(s) <= 12:
        return s[:2] + "*" * (len(s) - 4) + s[-2:]
    return s[:5] + "*" * 6 + s[-4:]


def coerce(item: dict, raw):
    """把前端传来的值校验并转成正确类型。不合法抛 ValueError（中文说明）。"""
    t = item["type"]

    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on", "开"):
            return True
        if s in ("false", "0", "no", "off", "关"):
            return False
        raise ValueError("只能是开或关")

    if t in ("int", "float"):
        if isinstance(raw, bool) or raw is None:
            raise ValueError("要填数字")
        if isinstance(raw, str):
            raw = raw.strip()
            if raw == "":
                raise ValueError("不能为空")
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise ValueError("要填数字")
        if num != num or num in (float("inf"), float("-inf")):
            raise ValueError("数字不合法")
        if t == "int":
            if abs(num - round(num)) > 1e-9:
                raise ValueError("要填整数")
            num = int(round(num))
            lo, hi = item.get("min"), item.get("max")
            if lo is not None and num < lo:
                raise ValueError("不能小于 %s" % lo)
            if hi is not None and num > hi:
                raise ValueError("不能大于 %s" % hi)
            return num
        lo, hi = item.get("min"), item.get("max")
        step = item.get("step")
        if step:
            num = round(num / step) * step
            num = round(num, 6)
        if lo is not None and num < lo:
            raise ValueError("不能小于 %s" % lo)
        if hi is not None and num > hi:
            raise ValueError("不能大于 %s" % hi)
        return num

    if t == "text":
        s = "" if raw is None else str(raw)
        s = s.strip()
        if len(s) > 400:
            raise ValueError("太长了（最多 400 字）")
        return s

    if t == "secret":
        if raw is None:
            return _SKIP
        s = str(raw).strip()
        if s == "":
            return _SKIP                     # 空 = 保持原样，不清空
        if len(s) > 400:
            raise ValueError("太长了")
        return s

    if t == "select":
        allowed = [o["v"] for o in item.get("options", [])]
        s = str(raw).strip()
        if s not in allowed:
            raise ValueError("只能选：%s" % " / ".join(allowed))
        return s

    if t == "time":
        s = str(raw or "").strip()
        if not _TIME_RE.match(s):
            raise ValueError("时间要写成 HH:MM（如 08:30）")
        return s

    if t == "list":
        if isinstance(raw, list):
            items = raw
        else:
            items = str(raw or "").splitlines()
        out, seen = [], set()
        for x in items:
            s = str(x).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        if len(out) > 500:
            raise ValueError("最多 500 项")
        for s in out:
            if len(s) > 120:
                raise ValueError("单项太长（最多 120 字）")
        return out

    if t in ("pair", "timerange"):
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError("要两个值")
        a, b = raw[0], raw[1]
        if t == "timerange":
            sa, sb = str(a or "").strip(), str(b or "").strip()
            if not _TIME_RE.match(sa) or not _TIME_RE.match(sb):
                raise ValueError("时间要写成 HH:MM")
            return [sa, sb]
        lo, hi, step = item.get("min"), item.get("max"), item.get("step") or 0.1
        vals = []
        for x in (a, b):
            try:
                n = float(x)
            except (TypeError, ValueError):
                raise ValueError("要填数字")
            if lo is not None and n < lo:
                raise ValueError("不能小于 %s" % lo)
            if hi is not None and n > hi:
                raise ValueError("不能大于 %s" % hi)
            vals.append(round(n / step) * step)
        vals = [round(v, 6) for v in vals]
        if vals[0] > vals[1]:
            raise ValueError("前一个不能大于后一个")
        return vals

    raise ValueError("不认识的类型 %s" % t)


def validate_changes(changes: dict) -> tuple[dict, dict]:
    """校验一批改动。返回 (可写入的 {path: value}, {path: 错误说明})。"""
    good, errors = {}, {}
    if not isinstance(changes, dict):
        return {}, {"*": "请求格式不对"}
    if len(changes) > 200:
        return {}, {"*": "一次改太多了（上限 200 项）"}

    for path, raw in changes.items():
        item = SCHEMA_BY_PATH.get(path)
        if item is None:
            errors[path] = "不是可配置项"
            continue
        try:
            val = coerce(item, raw)
        except ValueError as e:
            errors[path] = str(e)
            continue
        if val is _SKIP:
            continue
        good[path] = val
    return good, errors


def apply_changes(changes: dict) -> tuple[list, dict]:
    """把改动写回 config.json。返回 (生效的路径列表, 错误 map)。"""
    good, errors = validate_changes(changes)
    if errors:
        return [], errors
    if not good:
        return [], {}

    try:
        data = load_raw()
    except Exception as e:
        return [], {"*": "读不到 config.json：%s" % e}

    for path, val in good.items():
        _set_in(data, path, val)

    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        with contextlib.suppress(Exception):
            tmp.unlink(missing_ok=True)
        return [], {"*": "写入失败：%s" % e}

    _log("已保存 %d 项：%s" % (len(good), ", ".join(sorted(good))))
    return sorted(good), {}


# ==========================================================================
# 3. 运行状态（复用 service_ctl 的探测函数，状态逻辑只有一份）
# ==========================================================================

def _sc():
    try:
        import service_ctl
        return service_ctl
    except Exception as e:
        _log("service_ctl 载入失败: %s" % e)
        return None


def build_status() -> dict:
    items = []

    def add(key, label, state, text, detail=""):
        items.append({"key": key, "label": label, "state": state,
                      "text": text, "detail": detail})

    sc = _sc()
    if sc is None:
        return {"items": [{"key": "sys", "label": "状态模块", "state": "off",
                           "text": "读不到", "detail": "service_ctl.py 载入失败，看 logs/settings.log"}]}

    # 守护进程
    dpid = sc.read_pid("daemon")
    if sc.pid_alive(dpid):
        age = sc.heartbeat_age()
        det = "最近一次巡检 %s" % ("未知" if age is None else "%d 秒前" % int(age))
        if age is not None and age > 150:
            add("daemon", "守护进程", "warn", "可能卡住了", det + "（正常约 45 秒一次）")
        else:
            add("daemon", "守护进程", "ok", "pid %d" % dpid, det)
    else:
        add("daemon", "守护进程", "off", "没在跑",
            "点「启动」可拉起；或双击根目录的「一键启动.bat」。")

    # 协议层
    if sc.protocol_up():
        add("napcat", "协议层", "ok", "端口正常", "HTTP 3000 / WS 3001 都在监听。")
    else:
        npid = sc.napcat_pid()
        issue = sc._napcat_login_issue()
        qr = sc._qr_age()
        kind = issue[0] if issue else ""
        if kind == "risk":
            add("napcat", "协议层", "bad", "被风控拦截",
                "服务器原话：%s\n\n解法：手机上用最新版手机QQ登录这个号，按提示做完安全验证。\n"
                "风控没解除前，扫码和快速登录都会被拒 —— 先别扫了。" % issue[1])
        elif sc.napcat_alive() and qr is not None and qr < 240:
            add("napcat", "协议层", "warn", "需要扫码",
                "QQ 登录态过期了，要人工扫一次。\n双击根目录的「扫码登录.bat」。\n"
                "备用入口：%s" % sc._webui_url())
        elif sc.napcat_alive():
            add("napcat", "协议层", "warn", "正在登录", "进程在（pid %d）但端口没起。" % npid)
        else:
            add("napcat", "协议层", "off", "没在跑", "点「重启」会把它一起拉起来。")

    # 推理后端
    provider = sc.llm_provider()
    if provider == "ollama":
        if sc.ollama_up():
            add("llm", "推理后端", "ok", "本地就绪", "Ollama 11434 在监听。")
        else:
            add("llm", "推理后端", "bad", "本地没起",
                "Ollama 没在跑 —— 她收得到消息但回不了话。\n点「重启」会把它一起拉起来。")
    else:
        ok, detail = sc.cloud_up()
        add("llm", "推理后端", "ok" if ok else "bad",
            "云端 %s" % sc.llm_model(), detail +
            ("" if ok else "\n本地 Ollama 与当前配置无关，不用管它。"))

    # 猫娘大脑
    bpid = sc.read_pid("bot")
    if sc.pid_alive(bpid):
        add("bot", "猫娘大脑", "ok", "pid %d" % bpid, "正在和协议层通信。")
    else:
        add("bot", "猫娘大脑", "off", "没在跑", "协议层起来后守护进程会自动拉起它。")

    # 开机自启
    try:
        if (sc.STARTUP_DIR / sc.AUTOSTART_NAME).exists():
            add("auto", "开机自启", "ok", "已安装", "登录 Windows 后自动静默启动。")
        elif (sc.STARTUP_DIR / sc.AUTOSTART_LEGACY).exists():
            add("auto", "开机自启", "warn", "是旧版", "跑「开机自启-安装.bat」升级成无窗口版。")
        else:
            add("auto", "开机自启", "off", "没装", "跑「开机自启-安装.bat」可以装上。")
    except Exception:
        pass

    acct = BASE_DIR / "napcat_account.txt"
    if acct.exists():
        try:
            add("account", "登录账号", "ok", acct.read_text().strip(), "登录态有效时免扫码。")
        except Exception:
            pass

    return {"items": items}


def run_service(action: str) -> tuple[bool, str]:
    """执行启动/重启/停止等操作，把它的控制台输出抓回来展示在页面上。"""
    sc = _sc()
    if sc is None:
        return False, "service_ctl.py 载入失败"
    fn = sc.COMMANDS.get(action)
    if fn is None:
        return False, "不认识的操作：%s" % action
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = fn()
    except Exception as e:
        return False, "执行出错：%s\n%s" % (e, buf.getvalue())
    out = buf.getvalue().strip()
    return rc == 0, out


# ==========================================================================
# 4. HTTP 服务
# ==========================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoYouSettings/1.0"
    protocol_version = "HTTP/1.1"
    token = ""

    # ---- 基础设施 ----

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _text(self, text, code=200):
        self._send(code, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _host_ok(self) -> bool:
        """只接受来自本机的 Host —— 浏览器里的任意网页都能打 127.0.0.1，
        Host 校验挡的是 DNS rebinding（把 evil.com 解析到 127.0.0.1）。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _token_ok(self) -> bool:
        if not self.token:
            return True
        got = self.headers.get("X-Token") or ""
        if not got:
            q = self.path.split("?", 1)
            if len(q) == 2:
                for pair in q[1].split("&"):
                    if pair.startswith("t="):
                        got = pair[2:]
                        break
        return secrets.compare_digest(got, self.token)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except Exception:
            return {}

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json({"ok": False, "error": "只允许从本机访问"}, 403)
            return False
        if not self._token_ok():
            self._json({"ok": False, "error": "令牌无效 —— 请从「设置页.bat」打开"}, 403)
            return False
        return True

    # ---- 路由 ----

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/favicon.ico",):
            self._send(204, b"", "text/plain")
            return
        if not self._host_ok():
            self._json({"ok": False, "error": "只允许从本机访问"}, 403)
            return
        if path in ("/", "/index.html", "/settings.html"):
            if not self._token_ok():
                self._send(403, PAGE_NO_TOKEN.encode("utf-8"), "text/html; charset=utf-8")
                return
            self._send(200, render_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            if not self._guard():
                return
            self._json(build_status())
            return
        if path == "/api/config":
            if not self._guard():
                return
            data = effective()
            values = copy.deepcopy(data)
            hints = {}
            for it in SCHEMA:
                if it["type"] != "secret":
                    continue
                real = get_in(data, it["path"], "")
                hints[it["path"]] = mask_secret(real)
                _set_in(values, it["path"], None)      # 明文一律不回传
            self._json({"ok": True, "values": values, "hints": hints})
            return
        self._json({"ok": False, "error": "没有这个接口"}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        body = self._body()

        if path == "/api/save":
            saved, errors = apply_changes(body.get("changes") or {})
            ok = not errors
            self._json({"ok": ok, "saved": saved, "errors": errors,
                        "restart_needed": bool(saved)})
            return

        if path == "/api/reveal":
            item = SCHEMA_BY_PATH.get(str(body.get("path") or ""))
            if item is None or item["type"] != "secret":
                self._json({"ok": False, "error": "不是密钥项"}, 400)
                return
            real = get_in(effective(), item["path"], "")
            self._json({"ok": True, "value": str(real or "")})
            return

        if path == "/api/service":
            action = str(body.get("action") or "").strip()
            if action not in ("start", "restart", "stop", "logs", "install", "uninstall"):
                self._json({"ok": False, "error": "不支持的操作"}, 400)
                return
            ok, out = run_service(action)
            self._json({"ok": ok, "output": out})
            return

        self._json({"ok": False, "error": "没有这个接口"}, 404)


# 没带令牌直接访问时看到的一页 —— 说清"怎么才是对的打开方式"。
PAGE_NO_TOKEN = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>小柚 · 设置台</title><style>
body{background:#0b0c14;color:#e8eaf2;font-family:-apple-system,"Microsoft YaHei",sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{max-width:520px;padding:34px 38px;background:#171a27;border:1px solid #252a3d;border-radius:14px}
h1{margin:0 0 12px;font-size:19px}
p{color:#8b90a8;line-height:1.8;font-size:14px;margin:8px 0}
code{background:#12141f;padding:2px 7px;border-radius:5px;font-size:13px;color:#4fe3d0}
</style></head><body><div class="box">
<h1>请从「设置页.bat」打开</h1>
<p>这个页面带一次性令牌，直接输网址是不行的 —— 这是防止别的网页偷偷改你的配置。</p>
<p>关掉这个标签页，去项目根目录双击 <code>设置页.bat</code> 即可。</p>
</div></body></html>"""


# ==========================================================================
# 5. 渲染页面
# ==========================================================================

def render_page(snapshot: bool = False) -> str:
    html = TEMPLATE_FILE.read_text(encoding="utf-8")

    if snapshot:
        values = effective()
        hints = {}
        for it in SCHEMA:
            if it["type"] != "secret":
                continue
            hints[it["path"]] = mask_secret(get_in(values, it["path"], ""))
            _set_in(values, it["path"], None)
        status = build_status()
        token = ""
    else:
        values = effective()
        hints = {}
        for it in SCHEMA:
            if it["type"] != "secret":
                continue
            hints[it["path"]] = mask_secret(get_in(values, it["path"], ""))
            _set_in(values, it["path"], None)
        status = build_status()
        token = Handler.token

    def as_json(o):
        # 配置里不可能有 </script>，但真出现了会把页面截断 —— 一律转义掉
        return json.dumps(o, ensure_ascii=False).replace("</", "<\\/")

    repl = {
        "__SCHEMA_JSON__": as_json(SCHEMA),
        "__GROUPS_JSON__": as_json(GROUPS),
        "__VALUES_JSON__": as_json(values),
        "__HINTS_JSON__": as_json(hints),
        "__STATUS_JSON__": as_json(status),
        "__SNAPSHOT__": "true" if snapshot else "false",
        "__TOKEN__": as_json(token),
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


# ==========================================================================
# 6. 启动
# ==========================================================================

def _make_server() -> tuple[ThreadingHTTPServer, int]:
    last = None
    for port in range(PORT_START, PORT_START + PORT_TRIES):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            srv.daemon_threads = True
            return srv, port
        except OSError as e:
            last = e
            continue
    raise SystemExit("端口 %d~%d 全被占了，起不来（%s）"
                     % (PORT_START, PORT_START + PORT_TRIES - 1, last))


def run_server() -> int:
    if not TEMPLATE_FILE.exists():
        print("  [错误] 找不到 settings.html —— 它应该和本脚本在同一目录。")
        return 1
    if not CONFIG_FILE.exists():
        print("  [警告] 还没生成 config.json（先跑一次机器人会有）。")

    Handler.token = secrets.token_urlsafe(16)
    srv, port = _make_server()
    url = "http://127.0.0.1:%d/?t=%s" % (port, Handler.token)

    print("=" * 58)
    print("  小柚 · 设置台")
    print("=" * 58)
    print()
    print("  已启动：%s" % url)
    print()
    print("  浏览器会自动打开。没弹出来的话，把上面这行复制到浏览器。")
    print("  * 改完在页面上点「保存」，再点「保存并重启」才会生效。")
    print("  * 关掉本窗口 = 关闭设置页（配置已经写进 config.json，不会丢）。")
    print()

    threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)),
                     daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  设置页已关闭。")
    finally:
        with contextlib.suppress(Exception):
            srv.server_close()
    return 0


def main() -> int:
    if "--snapshot" in sys.argv:
        out = BASE_DIR.parent / "设置页-预览.html"
        out.write_text(render_page(snapshot=True), encoding="utf-8")
        print("已生成静态预览：%s" % out)
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    try:
        return run_server()
    except SystemExit as e:
        print("  [错误] %s" % e)
        return 1
    except Exception:
        import traceback
        traceback.print_exc()
        _log("启动失败: %s" % traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
