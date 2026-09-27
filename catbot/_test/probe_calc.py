# -*- coding: utf-8 -*-
"""实测：小柚遇到算术题，会不会真的去用计算器、答案对不对。

跑法: venv/Scripts/python.exe _test/probe_calc.py
注意：走的是 chat_with_tools（和真实聊天同一条路径），
      所以能看出她到底是「调了 calculate」还是「凭感觉瞎猜」。

结果同时写 logs/probe_calc.txt（UTF-8），因为 PowerShell 管道会转码。

来历：2026-09-24 她在群里答「9.11 和 9.8 哪个大」，说成了 9.11 大。
"""

import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402

# (题目, 答案里应该出现的数字)
CASES = [
    ("9.11 和 9.8 哪个大？", "9.8"),                    # ← 本次翻车的那道
    ("0.3 和 1/3 哪个大？", "1/3"),
    ("9.11 减 9.8 等于多少", "-0.69"),
    ("127 乘 38 是多少", "4826"),
    ("一件衣服原价 268 块，打 7 折是多少钱", "187.6"),
    ("我每天存 27 块，存 365 天一共能存多少", "9855"),
    ("2 的 30 次方是多少", "1073741824"),
    ("一根绳子 3.6 米，剪掉 1.15 米还剩多少", "2.45"),
    ("一个班 30 人，其中 60% 是女生，男生多少人", "12"),
]

OUT = []


def say(line=""):
    print(line)
    OUT.append(str(line))


def main():
    b = bot.CatBot()

    # 铁律：测试必须隔离会写盘的东西（记住主人的事会直接写 memory/facts.json）
    try:
        tmp = Path(tempfile.mkdtemp(prefix="probecalc_"))
        b.memory.dir = tmp / "_mem_test"
        b.memory.dir.mkdir(parents=True, exist_ok=True)
        b.memory._cache.clear()
        say(f"记忆已隔离到: {b.memory.dir}")
    except Exception as e:
        say(f"记忆隔离失败（不影响本探针）: {e}")

    # 记下她每次到底调了哪个工具
    called = []
    _orig_call = b.tools.call

    def spy(name, args_json, ctx):
        called.append(name)
        return _orig_call(name, args_json, ctx)

    b.tools.call = spy

    say("=" * 62)
    say(f"模型: {b.cfg['llm']['model']}  reasoning_effort={b.cfg['llm'].get('reasoning_effort')}")
    say(f"工具: {[s['function']['name'] for s in b.tools.schemas()]}")
    say("=" * 62)

    sys_prompt = b._system_prompt(is_group=False, bot_name="小柚", uid="10001")
    history = []
    n_ok = n_calc = 0

    for q, answer in CASES:
        ctx = {"uid": "probe_calc", "key": "_probe_calc", "name": "主人"}
        called.clear()
        t = time.time()
        try:
            raw = b.llm.chat_with_tools(sys_prompt, history, q, b.tools, ctx)
            dt = time.time() - t
            reply = bot.clean_reply(raw, b.cfg["reply"]["task_max_chars"])
            tools_used = list(called)
            hit = answer in reply.replace(" ", "")
            right = "✓ 对" if hit else "✗ 错"
            if hit:
                n_ok += 1
            if "calculate" in tools_used:
                n_calc += 1
            say(f"\n主人 > {q}")
            say(f"小柚 > {reply}")
            say(f"       [{right}] 用了: {tools_used or '没动手'}  {dt:.1f}s  该出现: {answer}")
            history.append({"role": "user", "content": q})
            history.append({"role": "assistant", "content": reply})
        except Exception as e:
            say(f"\n主人 > {q}")
            say(f"  [错误] {type(e).__name__}: {e}")

    say("\n" + "=" * 62)
    say(f"结论：答对 {n_ok}/{len(CASES)}，其中调了计算器 {n_calc}/{len(CASES)}")
    say("看点：答对没？调没调 calculate？有没有凭感觉硬猜？")
    say("（简单题（3+5、30*0.6）不调计算器也算合格——错的是「凭感觉」不是「不用工具」）")

    (ROOT / "logs" / "probe_calc.txt").write_text("\n".join(OUT), encoding="utf-8")
    print("\nwritten", ROOT / "logs" / "probe_calc.txt")


if __name__ == "__main__":
    main()
