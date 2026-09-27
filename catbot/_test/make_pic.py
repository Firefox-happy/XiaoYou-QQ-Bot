"""造一张测试图片，用来试"她看不看得见图"。
需要 PIL（项目主 venv 里没有，单独装：pip install pillow）。
写成脚本而不是命令行，避免中文经 PowerShell 参数变成乱码。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "tmp"
OUT.mkdir(parents=True, exist_ok=True)

TEXT = "报销单 合计1280元"
im = Image.new("RGB", (620, 170), "white")
d = ImageDraw.Draw(im)
font = None
for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
          r"C:\Windows\Fonts\simsun.ttc"):
    try:
        font = ImageFont.truetype(p, 44)
        break
    except Exception:
        continue
d.text((24, 58), TEXT, fill="black", font=font)
d.rectangle([0, 0, 619, 169], outline="#cccccc")
target = OUT / "e2e_pic.png"
im.save(target)
print("saved", target, target.stat().st_size, "bytes")
