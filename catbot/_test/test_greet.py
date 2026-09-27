# -*- coding: utf-8 -*-
"""早安文案专项：验证「主动带情报」这条要求到底落没落地。

做法：hook 工具调用入口，看她是真去查了天气，还是只会问「要不要我帮你查」。

⚠️ 这是一个**人工看结果**的脚本，不是回归测试（没有断言、只打印观察）。
默认用假模型跑通流程；想看她**真写出来的**文案，在 config.json 填好
llm.api_key 后加 `--real` 参数：
    python _test/test_greet.py --real
"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import bot as botmod

USE_REAL = "--real" in sys.argv

OUT = []
def log(*a):
    OUT.append(" ".join(str(x) for x in a))


b = botmod.CatBot()
b.api.self_id = "1234567890"

# 用假模型时不联网：这份脚本验的是"有没有去查天气"的流程，
# 不是文案文采。填了 Key 又加 --real 才会真问模型。
if not USE_REAL:
    class FakeLLM:
        model = "fake"
        api_base = "(本地假模型)"

        def chat(self, system_prompt, history, user_line):
            return "早安喵～今天北京晴，18 到 26 度，出门记得带件外套呀～"

        def chat_with_tools(self, system_prompt, history, user_line,
                            registry, ctx, on_ack=None):
            return self.chat(system_prompt, history, user_line)

    b.llm = FakeLLM()

# 隔离：绝不把测试对话写进真实记忆目录。
# 上一轮就是栽在这里 —— e2e 直接调 _handle，测试消息像真消息一样落盘了。
b.memory.dir = BASE / "tmp" / "_mem_test"
b.memory.dir.mkdir(parents=True, exist_ok=True)
b.memory._cache.clear()
# 故意造一段"最近聊过但没听清"的历史：上次就是这个上下文把早安带偏，
# 让她复读了"主人说啥啦小柚没听清"。新提示必须能压住它。
b.memory.append("p10001",
                "（主人发了条语音，可是小柚没听清）",
                "诶？主人说啥啦，小柚没听清喵……（耳朵抖了抖）信号是不是不太好呀～")

calls = []
real_call = b.tools.call


def spy(name, args_json, ctx):
    calls.append(f"{name}({args_json})")
    return real_call(name, args_json, ctx)


b.tools.call = spy

OWNER = "10001"

for label, city in [("有城市配置（北京）", "北京"), ("没配城市", "")]:
    b.cfg.data["proactive"]["city"] = city
    for i in range(1, 3):
        calls.clear()
        log("")
        log(f"=== {label} · 第 {i} 次 ===")
        t0 = time.time()
        try:
            txt = b._compose_greeting(OWNER)
            dt = time.time() - t0
            log(f"  耗时 {dt:.1f}s | 工具调用: {calls or '（没调任何工具）'}")
            log(f"  文案: {txt!r}")
            # 粗判：文案里有没有温度和阴晴这类硬信息
            import re
            has_temp = bool(re.search(r"\d+\s*[-~到至]?\s*\d*\s*(度|°|℃)", txt or ""))
            has_wx = any(k in (txt or "") for k in
                         ("晴", "雨", "阴", "多云", "雪", "风", "雾"))
            log(f"  含温度数字={has_temp} 含天气词={has_wx}")
        except Exception as e:
            log(f"  FAIL {type(e).__name__}: {e}")

(BASE / "_test" / "greet_out.txt").write_text("\n".join(OUT), encoding="utf-8")
print("done")
