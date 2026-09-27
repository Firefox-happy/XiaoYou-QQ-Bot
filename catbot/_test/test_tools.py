# -*- coding: utf-8 -*-
"""验证工具箱里每个工具真的能跑通。"""
import os
import sys
import tempfile
import time
from pathlib import Path

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import FactStore, ToolRegistry  # noqa: E402

lines = []


def log(x=""):
    lines.append(str(x))
    print(x)


reg = ToolRegistry({"tools": {"enabled": True, "timeout": 12}})

# 长期记忆必须隔离！
# remember 工具是**真的会落盘**的（catbot/memory/facts.json），
# 以前这个测试直接往主人的真实记忆里塞了「对海鲜过敏」「生日是3月14日」
# 三条假事实 —— 一旦哪天有个 QQ 号 10001 的人来找她说话，她就拿这些
# 编的东西跟人家搭话。所有会写盘的测试都要指到临时目录去。
_TMP_MEM = Path(tempfile.mkdtemp(prefix="toolstest_"))
reg._facts = FactStore(_TMP_MEM / "facts.json", 30)
log(f"长期记忆已隔离到: {_TMP_MEM}")
log(f"已注册工具: {[t['function']['name'] for t in reg.schemas()]}")
log()

ctx = {"uid": "10001", "key": "p10001", "name": "主人"}

CASES = [
    ("web_search", {"query": "DeepSeek V4 Pro 发布时间"}),
    ("read_url", {"url": "https://www.gov.cn/"}),
    ("get_weather", {"city": "北京"}),
    ("get_time", {}),
    ("calculate", {"expression": "9.11 > 9.8"}),      # 比大小：曾答反过
    ("calculate", {"expression": "127*38"}),
    ("calculate", {"expression": "9.11-9.8"}),        # float 会算出噪声
    ("calculate", {"expression": "1/0"}),             # 除零
    ("remember", {"content": "对海鲜过敏"}),
    ("remember", {"content": "对海鲜过敏"}),      # 重复，应被拒
    ("web_search", {"query": ""}),                # 空词
    ("read_url", {"url": "notaurl"}),             # 坏网址
]

for name, args in CASES:
    t0 = time.time()
    out = reg.call(name, __import__("json").dumps(args, ensure_ascii=False), ctx)
    dt = time.time() - t0
    log(f"--- {name}({args})  [{dt:.2f}s]")
    log(out[:700])
    log()

log("== 长期记忆 ==")
log(f"facts: {reg.facts_for('10001')}")
log(f"别人看到的: {reg.facts_for('99999')}")

Path(__file__).with_name("test_tools_out.txt").write_text("\n".join(lines), encoding="utf-8")
