# -*- coding: utf-8 -*-
"""回归：菜谱该查、论文该拒、闲聊要陪。

⚠️ 这是**人工看结果**的脚本，不是回归测试（只打印，没有断言）。
默认用假模型跑通流程；想看真模型的回答，在 config.json 填好 llm.api_key
后加 `--real`。
"""
import os
import sys
import time
from pathlib import Path

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as botmod  # noqa: E402

USE_REAL = "--real" in sys.argv

lines = []


def log(x=""):
    lines.append(str(x))
    print(x)


b = botmod.CatBot()


class FakeLLM:
    """假模型：不联网、不要 Key。让这份脚本在没配 Key 时也能跑通流程。"""

    model = "fake"
    api_base = "(本地假模型)"

    def chat(self, system_prompt, history, user_line):
        return "（假模型）喵～"

    def chat_with_tools(self, system_prompt, history, user_line,
                        registry, ctx, on_ack=None):
        return "（假模型）喵～"


if not USE_REAL:
    b.llm = FakeLLM()

sp_priv = b._system_prompt(False, "小柚", "10001")
sp_group = b._system_prompt(True, "小柚", "10001")

CASES = [
    ("群聊", "张三: 小柚 帮我查一下红烧肉怎么做"),
    ("私聊", "帮我写一篇 2000 字的论文"),
    ("私聊", "今天好累啊"),
    ("私聊", "最近有什么好看的电影吗"),
]

for scene, q in CASES:
    is_g = scene == "群聊"
    sp = sp_group if is_g else sp_priv
    ctx = {"uid": "10001", "key": "g123" if is_g else "p10001", "name": "张三"}
    acks = []
    t0 = time.time()
    try:
        r = b.llm.chat_with_tools(sp, [], q, b.tools, ctx, on_ack=acks.append)
    except Exception as e:
        log(f"--- [{scene}] {q}")
        log(f"    ERR {type(e).__name__}: {e}\n")
        continue
    used = bool(ctx.get("_used_tools"))
    log(f"--- [{scene}] {q}   [{time.time()-t0:.1f}s]  用过工具={used}")
    if acks:
        log(f"    [先发] {acks[0]}")
    log(f"    [回复] {r}")
    log(f"    [字数] {len(r)}")
    log()

Path(__file__).with_name("e2e3_out.txt").write_text("\n".join(lines), encoding="utf-8")
