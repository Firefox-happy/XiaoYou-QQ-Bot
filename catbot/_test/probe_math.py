# -*- coding: utf-8 -*-
"""实测：小柚遇到数学题会怎么样。

跑法: venv/Scripts/python.exe _test/probe_math.py
注意：走的是 chat_with_tools（和真实聊天同一条路径），
      所以能看出她会不会误以为是「查资料」而去联网搜。

结果同时写 logs/probe_math.txt（UTF-8），因为 PowerShell 管道会转码。
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402

# (题目, 正确答案)
CASES = [
    ("3加5等于几呀", "8"),
    ("127乘38是多少", "4826"),
    ("一个班30个人，其中60%是女生，男生有多少人", "12"),
    ("帮我解一下方程 3x + 7 = 22", "x=5"),
    ("每天存27块钱，存满365天能存多少", "9855"),
    ("圆的半径是5厘米，面积大概是多少", "约78.5（πr²）"),
    ("帮我算个24点：3 3 8 8，怎么凑出24", "8/(3-8/3)=24"),
    ("1除以7的小数点后第100位是什么数字", "8（循环142857）"),
    ("987654321 乘 123456789 等于多少", "121932631112635269"),
    ("2的30次方是多少", "1073741824"),
]

OUT = []


def say(line=""):
    print(line)
    OUT.append(str(line))


def main():
    cfg = bot.Config(ROOT / "config.json")
    b = bot.CatBot()

    say("=" * 62)
    say(f"模型: {b.cfg['llm']['model']}  reasoning_effort={b.cfg['llm'].get('reasoning_effort')}")
    say(f"字数: max_chars={b.cfg['reply']['max_chars']}  task_max_chars={b.cfg['reply']['task_max_chars']}")
    say(f"工具: {[s['function']['name'] for s in b.tools.schemas()]}")
    say("=" * 62)

    sys_prompt = b._system_prompt(is_group=False, bot_name="小柚", uid="10001")
    history = []

    for q, answer in CASES:
        ctx = {"uid": "probe_math", "key": "_probe_math", "name": "主人"}
        t = time.time()
        try:
            raw = b.llm.chat_with_tools(sys_prompt, history, q, b.tools, ctx)
            dt = time.time() - t
            reply = bot.clean_reply(raw, b.cfg["reply"]["max_chars"])
            used = "用过工具" if ctx.get("_used_tools") else "纯聊天"
            say(f"\n主人 > {q}")
            say(f"小柚 > {reply}")
            say(f"       [{used}] {dt:.1f}s  正确应为: {answer}")
            history.append({"role": "user", "content": q})
            history.append({"role": "assistant", "content": reply})
        except Exception as e:
            say(f"\n主人 > {q}")
            say(f"  [错误] {type(e).__name__}: {e}")

    say("\n" + "=" * 62)
    say("看点：答对没？有乱编没？会不会被当成查资料去联网？")

    (ROOT / "logs" / "probe_math.txt").write_text("\n".join(OUT), encoding="utf-8")


if __name__ == "__main__":
    main()
