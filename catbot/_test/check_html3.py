"""HTML 结构完整性校验：标签配平 + 常见踩坑。

改 HTML 时最容易漏闭合标签，肉眼在几百行里很难看出来，所以留一个机器检查。
默认校验 catbot/settings.html（设置页），也可以命令行传别的文件：

    python _test/check_html3.py                  # 默认查 settings.html
    python _test/check_html3.py 某个页面.html     # 查指定文件

⚠️ `<script>` 和 `<style>` 里的内容是 JS / CSS，里面出现的 `<` `>`（比如
`a < b`、`i < len`）会被朴素的标签扫描误认成标签，所以先把这两块整段挖掉再扫。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTML = (Path(sys.argv[1]) if len(sys.argv) > 1
        else HERE.parent / "settings.html")
if not HTML.exists():
    print(f"[--] 找不到文件：{HTML}")
    sys.exit(2)
text = HTML.read_text(encoding="utf-8")
print(f"校验文件 : {HTML.name}")

# 先把 script / style 整段替换成等量换行，保证行号不变，再做标签配平。
def _blank(m: re.Match) -> str:
    return "\n" * m.group(0).count("\n")

scannable = re.sub(r"<script\b.*?</script>", _blank, text,
                   flags=re.DOTALL | re.IGNORECASE)
scannable = re.sub(r"<style\b.*?</style>", _blank, scannable,
                   flags=re.DOTALL | re.IGNORECASE)

VOID = {"br", "hr", "img", "input", "meta", "link", "source", "area", "col"}
lines = scannable.split("\n")

stack = []
errors = []
for i, line in enumerate(lines, 1):
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(/?)>", line):
        closing, tag, attrs, selfclose = m.groups()
        tag = tag.lower()
        if tag in VOID or selfclose:
            continue
        if closing:
            if not stack:
                errors.append(f"第 {i} 行：多了 </{tag}>")
            elif stack[-1][0] != tag:
                errors.append(
                    f"第 {i} 行：</{tag}> 与第 {stack[-1][1]} 行的 <{stack[-1][0]}> 不配对")
                stack.pop()
            else:
                stack.pop()
        else:
            stack.append((tag, i))

print(f"文件大小 : {len(text)} 字符 / {len(text.splitlines())} 行")
print(f"未闭合   : {len(stack)}")
for tag, ln in stack[:10]:
    print(f"    <{tag}> 开在第 {ln} 行，没关")
print(f"配对错误 : {len(errors)}")
for e in errors[:10]:
    print("    " + e)

# 关键结构计数
for tag in ("table", "tr", "td", "th", "div", "section"):
    o = len(re.findall(rf"<{tag}\b", scannable))
    c = len(re.findall(rf"</{tag}>", scannable))
    flag = "OK " if o == c else "!! "
    print(f"{flag}<{tag}>  {o} 开 / {c} 闭")

print()
print("结果：", "结构完整" if not stack and not errors else "有问题，见上")
sys.exit(0 if not stack and not errors else 1)
