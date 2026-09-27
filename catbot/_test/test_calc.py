# -*- coding: utf-8 -*-
"""
计算器（tools._safe_calc / calculate 工具）单元测试 —— 纯函数，不联网。

来历：2026-09-24 小柚在群里把「9.11 和 9.8 哪个大」答反了（她说 9.11 大）。
根因是大模型按字符串比数字，不是偶发。所以加了真计算器，这个文件盯住它。

运行:
    python _test/test_calc.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools  # noqa: E402
from tools import ToolRegistry, _safe_calc  # noqa: E402

PASS = FAIL = 0


def check(name, got, expect):
    global PASS, FAIL
    ok = got == expect
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望: {expect!r}\n         实际: {got!r}")


def check_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("\n=== 1. 基本算术 ===")
check("加法", _safe_calc("3+5"), "3+5 = 8")
check("乘法", _safe_calc("127*38"), "127*38 = 4826")
check("四则混合", _safe_calc("(1+0.6)*300"), "(1+0.6)*300 = 480")
check("除法整除不留小数", _safe_calc("10/2"), "10/2 = 5")
check("取整除法", _safe_calc("7//2"), "7//2 = 3")
check("取余", _safe_calc("7%3"), "7%3 = 1")
check("负号", _safe_calc("-3+10"), "-3+10 = 7")
check("幂", _safe_calc("2**10"), "2**10 = 1024")

print("\n=== 2. 小数精度（float 会在这些地方露馅）===")
# float 下 0.3-0.1 = 0.19999999999999998，Decimal 下就是 0.2
check("0.3-0.1 无浮点噪声", _safe_calc("0.3-0.1"), "0.3-0.1 = 0.2")
check("0.1+0.2 无浮点噪声", _safe_calc("0.1+0.2"), "0.1+0.2 = 0.3")
check("9.11-9.8 精确", _safe_calc("9.11-9.8"), "9.11-9.8 = -0.69")
check("大整数乘法不丢位", _safe_calc("987654321*123456789"),
      "987654321*123456789 = 121932631112635269")
check("2 的 30 次方", _safe_calc("2**30"), "2**30 = 1073741824")
check_true("循环小数收尾带省略号",
           _safe_calc("1/7").endswith("…"), _safe_calc("1/7"))

print("\n=== 3. 比大小（本次翻车的地方）===")
check("9.11 > 9.8 应不成立", _safe_calc("9.11 > 9.8"), "9.11 > 9.8 = 不成立")
check("9.8 > 9.11 应成立", _safe_calc("9.8 > 9.11"), "9.8 > 9.11 = 成立")
check("0.3 > 1/3 应不成立", _safe_calc("0.3 > 1/3"), "0.3 > 1/3 = 不成立")
check("相等判断", _safe_calc("0.1+0.2 == 0.3"), "0.1+0.2 == 0.3 = 成立")
check("不等判断", _safe_calc("1 != 2"), "1 != 2 = 成立")
check("链式比较", _safe_calc("1 < 2 < 3"), "1 < 2 < 3 = 成立")

print("\n=== 4. 函数与常量 ===")
check("sqrt 保整数", _safe_calc("sqrt(16)"), "sqrt(16) = 4")
check_true("sqrt(2) 近似", _safe_calc("sqrt(2)").startswith("sqrt(2) = 1.41421356"),
           _safe_calc("sqrt(2)"))
check("abs", _safe_calc("abs(-7)"), "abs(-7) = 7")
check("round", _safe_calc("round(3.14159, 2)"), "round(3.14159, 2) = 3.14")
check("pi 能用", _safe_calc("round(pi, 4)"), "round(pi, 4) = 3.1416")
check("max", _safe_calc("max(3, 9, 5)"), "max(3, 9, 5) = 9")

print("\n=== 5. 安全：绝不能执行代码 ===")
# 这些都是"提示注入"想让她执行的。一句都不能过。
DANGER = [
    "__import__('os').system('echo hi')",
    "os.system('dir')",
    "open('x','w')",
    "().__class__.__bases__[0].__subclasses__()",
    "exec('1+1')",
    "eval('1+1')",
    "1 if True else 2",
    "lambda: 1",
    "[x for x in range(3)]",
    "'a'*3",
    "print(1)",
    "globals()",
    "a+1",
    "[1,2][0]",
    "9**9**9",              # 内存炸弹
    "2**999999",            # 指数超限
]
for expr in DANGER:
    got = _safe_calc(expr)
    ok = got.startswith("（")            # 一律应是"算不了/看不懂"，不是结果
    check_true(f"拒绝执行: {expr[:34]}", ok, f"→ {got!r}")

print("\n=== 6. 坏输入不崩、返回人话 ===")
for expr, expect_prefix in [
    ("", "（"),
    ("   ", "（"),
    ("((((", "（"),
    ("1+", "（"),
    ("1/0", "（"),
    ("0/0", "（"),
    ("3%%", "（"),
    ("50%*3", "（"),        # 百分号语法错误，得提示她写小数
    ("x" * 500, "（"),
    (None, "（"),
]:
    got = _safe_calc(expr)
    check_true(f"坏输入不崩: {str(expr)[:24]!r}", isinstance(got, str) and got.startswith(expect_prefix),
               f"→ {got!r}")

print("\n=== 7. 不写盘、不改全局精度 ===")
from decimal import Decimal, getcontext  # noqa: E402

before = getcontext().prec
_safe_calc("1/7")
check("全局 Decimal 精度没被污染", getcontext().prec, before)
check_true("普通参数照旧精确", _safe_calc("0.1+0.2") == "0.1+0.2 = 0.3")

print("\n=== 8. 工具注册与调度 ===")
reg = ToolRegistry({"tools": {"enabled": True, "timeout": 12}})
names = [t["function"]["name"] for t in reg.schemas()]
check_true("calculate 已注册", "calculate" in names, names)
check("经 call() 走一遍", reg.call("calculate", '{"expression": "9.11 > 9.8"}', {}),
      "9.11 > 9.8 = 不成立")
check("经 call() 走一遍（乘法）", reg.call("calculate", '{"expression": "30*0.6"}', {}),
      "30*0.6 = 18")
# 参数写坏不能抛异常（照旧兜住）
for bad in ("{", '{"expression": 123}', '{"expression": null}', "null", "", None, '{"x":1}'):
    try:
        r = reg.call("calculate", bad, {})
        check_true(f"坏参数不崩: {str(bad)[:18]!r}", isinstance(r, str) and len(r) > 0, repr(r))
    except Exception as e:
        check_true(f"坏参数不崩: {str(bad)[:18]!r}", False, f"抛了 {type(e).__name__}")
check_true("工具参数是中文提示时不崩",
           isinstance(reg.call("calculate", '{"expression": "三加五"}', {}), str))

print("\n=== 9. tools 关掉时 shemas 为空 ===")
off = ToolRegistry({"tools": {"enabled": False}})
check("关闭后无工具", off.schemas(), [])

print(f"\n{'=' * 50}")
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 50)
sys.exit(1 if FAIL else 0)
