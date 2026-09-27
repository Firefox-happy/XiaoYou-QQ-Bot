# -*- coding: utf-8 -*-
"""生成脱敏的 config.example.json。

为什么要脚本而不用手改：config.json 有 90+ 项、分十几组，手抄必漏；
而且「哪些字段算敏感」这条判断应该只有一处 —— 就是下面 SENSITIVE 表。
以后新增配置项，重跑一次这个脚本即可，示例文件不会漂移。

用法（在 catbot/ 目录下）：
    python _make_example_config.py            # 写入 config.example.json
"""
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
OUT = HERE / "config.example.json"

# 需要脱敏的字段：路径 -> 替换值
# ⚠️ 用路径而不是"字段名匹配"：`owner_qq` 和 `city` 这些要清空，
#    但 `model` / `api_base` 这类要保留原样，纯按名字猜一定会误伤。
SENSITIVE = {
    "llm.api_key": "",
    "llm.api_base": "https://api.deepseek.com/v1",
    "tools.bocha_key": "",
    "napcat.access_token": "",
    "proactive.owner_qq": "",
    "proactive.city": "",
    "whitelist.groups": [],
    "whitelist.users": [],
}


def _set(d, path, value):
    """按点分路径写值，中间缺的层自动建。"""
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def main():
    # 以出厂默认值为基线 —— 比拿别人的 config.json 干净，
    # 而且能保证示例里带全所有组（含用户还没保存过的新组，比如 notify）。
    import bot
    cfg = json.loads(json.dumps(bot.DEFAULT_CONFIG, ensure_ascii=False))

    for path, value in SENSITIVE.items():
        _set(cfg, path, value)

    # 示例文件默认走"云端"这条更容易上手的路（不用先装 Ollama）：
    # provider=openai + 本地 ollama_url 留着，用户两种都能切。
    # ⚠️ provider / model / api_base 是**一套**的：只把 provider 改成 openai、
    #    却留着出厂默认的 model=qwen2.5:7b-instruct（那是本地模型名），
    #    用户照抄后就会拿本地模型名去请求云端接口，必然报 model 不存在。
    _set(cfg, "llm.provider", "openai")
    _set(cfg, "llm.model", "deepseek-flash")

    OUT.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("已写入 %s（%d 组）" % (OUT.name, len(cfg)))
    for k in SENSITIVE:
        print("  已脱敏 " + k)


if __name__ == "__main__":
    main()
