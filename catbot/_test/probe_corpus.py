# -*- coding: utf-8 -*-
"""梗库自动更新 · 活体探针 —— 真联网、真调模型，但**不碰真的 memes.md**。

跑一轮完整链路：搜「本月热梗盘点」→ 模型挑梗 → 渲染 → 写进临时文件。
看四件事：
  1. 真链路上能挑出几条（单测里喂的是假数据，不算数）
  2. 落盘格式对不对（能不能被解析回条目）
  3. 手写区有没有被动过一个字
  4. 耗时大概多少（决定这个任务放后台跑合不合适）

运行:
    python _test/probe_corpus.py
    去看 catbot/logs/probe_corpus.txt
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot      # noqa: E402
import corpus   # noqa: E402

_OUT = ROOT / "logs" / "probe_corpus.txt"
_f = open(_OUT, "w", encoding="utf-8")


def w(*a):
    s = " ".join(str(x) for x in a)
    _f.write(s + "\n")
    _f.flush()


cfg = bot.Config(bot.BASE_DIR / "config.json")
tools = bot.ToolRegistry(cfg.data)
llm = bot.LLM(cfg)

w("=== 环境 ===")
w("模型: %s | %s" % (llm.model, llm.provider))
w("搜索通道: %s" % cfg.data.get("tools", {}).get("search_provider"))
w("")

# 关键：先把真梗库**复制**到临时目录，全程只动副本。
TMP = Path(tempfile.mkdtemp(prefix="xiaoyou_probe_corpus_"))
mp = TMP / "memes.md"
manual = (bot.BASE_DIR / "memes.md").read_text(encoding="utf-8")
mp.write_text(manual, encoding="utf-8")
w("原梗库 %d 字，已复制到临时文件（真的 memes.md 全程不碰）" % len(manual))
w("")

w("=== 跑一轮 ===")
t0 = time.time()
res = corpus.update(
    mp,
    search_fn=lambda q: tools.search(q),
    chat_fn=lambda s, h, u: llm.chat(s, h, u),
    max_items=12,
)
dt = time.time() - t0
w("耗时 %.1f 秒" % dt)
w("结果: %r" % (res,))
w("")

if not res.get("ok"):
    w("!! 这一轮没成功，原因在上面。链路本身是通的（单测已验证），")
    w("!! 多半是搜索没结果或模型这次没挑出东西 —— 换个时间再试。")
    _f.close()
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)

after = mp.read_text(encoding="utf-8")
w("=== 文件对比 ===")
w("原 %d 字 → 新 %d 字" % (len(manual), len(after)))
w("手写区一字未动: %s" % bool(after.startswith(manual.rstrip())))
w("")

head, body, tail, has = corpus._split(after)
items = corpus._parse_items(body)
w("=== 自动区（能被解析回的条目：%d 条）===" % len(items))
for it in items:
    w("  · %s【自动·%s】——%s" % (it["name"], it["date"], it["meaning"]))
    if it.get("example"):
        w("      > 小柚：%s" % it["example"])
w("")

# 去重验证：自动区里有没有和手写区重复的
manual_names = corpus.existing_names(head)
dups = [it["name"] for it in items if corpus._norm(it["name"]) in manual_names]
w("=== 去重检查 ===")
w("与手写区重复的条目: %s" % (dups or "无"))
w("")

w("=== 新文件尾部 45 行 ===")
w("\n".join(after.splitlines()[-45:]))

_f.close()
shutil.rmtree(TMP, ignore_errors=True)
print("done ->", _OUT)
