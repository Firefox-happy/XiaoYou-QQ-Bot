# -*- coding: utf-8 -*-
"""实测第二轮：数学题需要「写过程」时，字数上限会不会把答案砍掉。

跑法: venv/Scripts/python.exe _test/probe_math2.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402

CASES = [
    ("详细讲讲怎么解方程 2x²-5x+3=0，要有过程", "x=1 或 x=1.5"),
    ("火车每小时跑120.5公里，跑了3小时20分钟，一共多远", "约401.7公里"),
    ("一个长方形长12.6米宽7.4米，周长和面积各是多少", "周长40米，面积93.24平方米"),
]

OUT = []


def say(line=""):
    print(line)
    OUT.append(str(line))


def main():
    b = bot.CatBot()
    limit = b.cfg["reply"]["max_chars"]
    say("=" * 62)
    say(f"普通聊天字数上限 max_chars = {limit}")
    say("=" * 62)

    sys_prompt = b._system_prompt(is_group=False, bot_name="小柚", uid="10001")
    history = []

    for q, answer in CASES:
        ctx = {"uid": "probe_math", "key": "_probe_math", "name": "主人"}
        t = time.time()
        raw = b.llm.chat_with_tools(sys_prompt, history, q, b.tools, ctx)
        dt = time.time() - t
        reply = bot.clean_reply(raw, limit)
        cut = "  ← 被截断了！" if len(raw) > len(reply) + 5 else ""
        say(f"\n主人 > {q}")
        say(f"小柚 > {reply}")
        say(f"       [原文{len(raw)}字 → 发出{len(reply)}字]{cut} {dt:.1f}s  正确应为: {answer}")
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": reply})

    (ROOT / "logs" / "probe_math2.txt").write_text("\n".join(OUT), encoding="utf-8")


if __name__ == "__main__":
    main()
