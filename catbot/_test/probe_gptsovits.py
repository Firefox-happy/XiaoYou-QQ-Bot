# -*- coding: utf-8 -*-
"""换嗓探针：同几句台词，**Edge 与本机 GPT-SoVITS 各合成一遍**，出对比样带。

为什么要做 A/B 而不是只出新的
-----------------------------
"换了个引擎"这种改动，光听新的是听不出好坏的 —— 没有对照就没有判断。
所以这里把两句放在一起，同一句台词、同一批情绪，直接比。

重点看两件事：
  1. **句读**：长句那条，Edge 那边是客户端切开再拼的，段与段之间会有
     生硬的停顿；GPT-SoVITS 是整句一次出来的，应该连贯得多。
  2. **情绪**：撒娇 / 兴奋那几条，GPT-SoVITS 的起伏应该更贴人。

产物落在项目根 `换嗓试听/`：12 个 mp3 + 一个 `试听页.html`
（音频 base64 内嵌，单独拷走也能放）。

⚠️ 这个脚本要**真的跑**本机语音服务，所以：
   - 服务没起来时它会**直接告诉你并退出**，不会假装成功
   - 第一条最慢（要加载参考音频），后面都快
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import voice  # noqa: E402

ROOT = HERE.parent.parent                     # 项目根
OUT = ROOT / "换嗓试听"
NO_WINDOW = 0x08000000

# 台词：覆盖平常 / 撒娇 / 兴奋 / 害羞 / 委屈，外加一条长的专门看句读
LINES = [
    ("平常", "主人主人，你在干嘛呀？"),
    ("撒娇", "喵呜~ 人家好想你嘛，抱一下好不好…"),
    ("兴奋", "哇！你终于回来啦！我等你好久好久了！"),
    ("害羞", "那个…其实我…有一点点喜欢你来着…"),
    ("委屈", "你怎么这么久都不理我啊…"),
    ("长句", "今天外面下小雨，气温只有十八度，出门记得带伞，别着凉了哦。"),
]

EDGE_PROFILE = {"tts_provider": "edge", "edge_voice": "zh-CN-XiaoyiNeural",
                "edge_rate": 8, "edge_pitch": 10, "edge_volume": 0}
GSV_PROFILE = {"tts_provider": "gptsovits"}


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg")


def stats(p: Path) -> dict:
    """时长 + 峰值（峰值 >= 0dB 就是破音）。"""
    out = {"dur": "?", "peak": -99.0, "kb": p.stat().st_size / 1024 if p.exists() else 0}
    exe = ffmpeg_exe()
    if not exe or not p.exists():
        return out
    try:
        r = subprocess.run([exe, "-hide_banner", "-i", str(p),
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=60,
                           creationflags=NO_WINDOW)
        txt = (r.stderr or "") + (r.stdout or "")
        m = re.search(r"max_volume: (-?[\d.]+) dB", txt)
        if m:
            out["peak"] = float(m.group(1))
        m2 = re.search(r"Duration: (\d+):(\d+):([\d.]+)", txt)
        if m2:
            out["dur"] = "%.1fs" % (int(m2.group(1)) * 3600 + int(m2.group(2)) * 60
                                    + float(m2.group(3)))
    except Exception:
        pass
    return out


def synth_one(text: str, profile: dict, dst: Path) -> tuple[bool, float]:
    t0 = time.time()
    got = voice.speak(text, rate=1, fmt="mp3", profile=profile)
    dt = time.time() - t0
    if not got or not Path(got).exists():
        return False, dt
    shutil.copy2(got, dst)
    return True, dt


def card(title: str, tag: str, desc: str, path: Path) -> str:
    st = stats(path)
    peak = "%.1f dB" % st["peak"]
    warn = ""
    if st["peak"] >= 0:
        peak = "%.1f dB ⚠破音" % st["peak"]
        warn = " warn"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f'''  <div class="card{warn}">
    <div class="ttl">{title}<span class="tag">{tag}</span></div>
    <div class="meta">{desc} · 时长 {st["dur"]} · 峰值 {peak} · {st["kb"]:.0f} KB</div>
    <audio controls preload="none" src="data:audio/mpeg;base64,{b64}"></audio>
  </div>
'''


PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>换嗓对比 · Edge vs 本机 GPT-SoVITS</title>
<style>
  body{{background:#0b1020;color:#e8ecf7;font-family:"Microsoft YaHei",system-ui,sans-serif;
       max-width:880px;margin:0 auto;padding:28px 20px 60px}}
  h1{{font-size:22px;margin:0 0 6px}}
  .sub{{color:#8b96b3;font-size:13px;margin-bottom:22px;line-height:1.7}}
  .grp{{margin:26px 0 10px;font-size:15px;font-weight:700;color:#3ddc97}}
  .line{{color:#8b96b3;font-size:13px;margin:-4px 0 10px}}
  .card{{background:#151b30;border:1px solid #232c47;border-radius:12px;padding:12px 14px;
        margin:9px 0}}
  .card.warn{{border-color:#e05c5c}}
  .ttl{{font-size:14px;font-weight:700;margin-bottom:4px}}
  .tag{{display:inline-block;background:#243050;color:#9fb3e0;font-size:11px;font-weight:700;
       padding:1px 7px;border-radius:9px;margin-left:8px}}
  .meta{{color:#7d89a8;font-size:12px;margin-bottom:8px}}
  audio{{width:100%;height:34px}}
  .note{{background:#12203a;border-left:3px solid #3ddc97;padding:10px 14px;border-radius:6px;
        font-size:13px;line-height:1.8;margin:16px 0}}
</style>
</head>
<body>
<h1>换嗓对比 · Edge vs 本机 GPT-SoVITS</h1>
<div class="sub">
  同一句台词、同一个项目、同一台机器，只换合成引擎。<br>
  音频以 base64 内嵌在本文件里，可以单独拷走播放。
</div>
<div class="note">
  <strong>重点听两条：</strong><br>
  ① <strong>长句</strong>那条 —— Edge 是客户端切开再拼的，段与段之间有生硬停顿；
     GPT-SoVITS 整句一次出来，应该连贯得多。<br>
  ② <strong>撒娇 / 兴奋</strong>那两条 —— GPT-SoVITS 的情绪来自参考音频，起伏更贴人。
</div>
{body}
</body>
</html>
'''


def main() -> int:
    if not voice.gptsovits_alive({}):
        print("本机语音服务没起来（%s）—— 先双击「语音服务-启动.bat」再跑这个。"
              % voice.gptsovits_url({}))
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    cards: list[str] = []
    print("=" * 68)
    print("换嗓对比：Edge  vs  本机 GPT-SoVITS")
    print("=" * 68)

    edges: list[tuple] = []
    gsvs: list[tuple] = []
    for i, (name, text) in enumerate(LINES, 1):
        print(f"\n[{i}] {name} —— {text}")
        e_dst = OUT / f"{i:02d}_{name}_①Edge晓伊.mp3"
        g_dst = OUT / f"{i:02d}_{name}_②本机克隆.mp3"

        ok_e, t_e = synth_one(text, EDGE_PROFILE, e_dst)
        print(f"    Edge         {'OK ' if ok_e else '失败'} {t_e:5.2f}s"
              + (f"  峰值 {stats(e_dst)['peak']:6.1f} dB" if ok_e else ""))
        ok_g, t_g = synth_one(text, GSV_PROFILE, g_dst)
        print(f"    本机克隆     {'OK ' if ok_g else '失败'} {t_g:5.2f}s"
              + (f"  峰值 {stats(g_dst)['peak']:6.1f} dB" if ok_g else ""))

        edges.append((name, text, e_dst, ok_e, t_e))
        gsvs.append((name, text, g_dst, ok_g, t_g))

    # 先 Edge 后本机 —— 同一条台词上下相邻，方便直接 A/B
    for (name, text, e_dst, ok_e, t_e), (_n2, _t2, g_dst, ok_g, t_g) in zip(edges, gsvs):
        cards.append(f'<div class="grp">{name}</div>\n<div class="line">{text}</div>')
        if ok_e:
            cards.append(card("Edge TTS", "晓伊", "原来的嗓子", e_dst))
        else:
            cards.append('<div class="card">Edge TTS 合成失败（多半是断网）</div>')
        if ok_g:
            cards.append(card("本机 GPT-SoVITS", "克隆音色", "新嗓子", g_dst))
        else:
            cards.append('<div class="card">本机 GPT-SoVITS 合成失败，看 GPT-SoVITS\\logs\\api_v2.log</div>')

    page = OUT / "试听页.html"
    page.write_text(PAGE.format(body="\n".join(cards)), encoding="utf-8")

    n_ok_e = sum(1 for x in edges if x[3])
    n_ok_g = sum(1 for x in gsvs if x[3])
    avg_e = sum(x[4] for x in edges if x[3]) / max(1, n_ok_e)
    avg_g = sum(x[4] for x in gsvs if x[3]) / max(1, n_ok_g)

    print("\n" + "=" * 68)
    print(f"Edge        成功 {n_ok_e}/{len(LINES)}   平均 {avg_e:.2f} 秒/条")
    print(f"本机 GPT-SoVITS 成功 {n_ok_g}/{len(LINES)}   平均 {avg_g:.2f} 秒/条")
    print(f"试听页：{page}")
    print("=" * 68)
    return 0 if n_ok_g == len(LINES) else 1


if __name__ == "__main__":
    sys.exit(main())
