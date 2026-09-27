# -*- coding: utf-8 -*-
"""情绪语音 · 活体探针：生成可试听的样带 + 一个自带音频的试听页。

**为什么要有它**：主人问"怎么让她的语音有情绪"，光靠我描述没用 ——
必须让人拿耳朵在**同一句话**上横向对比，才能挑出哪些像、哪些不要。
改过 `voice_emo.EMOTIONS` 里任何参数后，重跑这个脚本就能重新试听。

跑的什么：
  A 组  条级：同一句话 × 10 种情绪（整句一组参数，隔离"情绪"这一个变量）
  B 组  句内：同一句话，平铺一份、分段起伏一份、撒娇尾巴一份
  → 产物落 `语音情绪样带/`，另有 `试听页.html`（音频 base64 内嵌，
    不依赖相对路径，双击就能放）。

顺带每份都测时长与峰值音量 —— 峰值 ≥ 0dB 就是削波，会破音，参数给过头了。

运行:
    python _test/probe_voice_emo.py
"""

import base64
import re
import subprocess
import sys
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
ROOT = TEST_DIR.parent.parent           # 项目根
sys.path.insert(0, str(ROOT / "catbot"))

import voice        # noqa: E402
import voice_emo    # noqa: E402

OUT = ROOT / "语音情绪样带"
TMP = ROOT / "catbot" / "tmp" / "emo_sample"

LINE_GRID = "主人主人，你终于回来啦～小柚等你等得都快睡着了喵。"
LINE_CONTOUR = "主人！你终于回来啦，小柚等你等得，都快睡着了喵……"

FF = voice.ffmpeg_exe()


def synth(text: str, rate: int, pitch: int, volume: int, tag: str) -> Path | None:
    out = TMP / f"{tag}.mp3"
    return out if voice._edge_synth(text, out, voice.DEFAULT_EDGE_VOICE,
                                    rate=rate, pitch=pitch, volume=volume) else None


def join(segs: list[tuple[Path, int]], out: Path) -> bool:
    """按顺序拼接，段间插静音（毫秒）。统一重采样再 concat，避免接缝爆音。

    ⚠️ 两个编号必须分开数：`-i` 进来的音频是 0,1,2…，静音段也各占一个
    滤镜标签号。第一版用一个计数器数了两种东西，于是滤镜去要 [3:a] 这种
    不存在的输入 —— 报 "Invalid file index"。这里 iid 管输入、lid 管标签。
    """
    inputs: list[str] = []
    for p, _ in segs:
        inputs += ["-i", str(p)]
    parts: list[str] = []
    labels: list[str] = []
    for iid, (_, gap) in enumerate(segs):
        parts.append(f"[{iid}:a]aresample=24000,aformat=channel_layouts=mono[a{iid}]")
        labels.append(f"[a{iid}]")
        if gap > 0:
            lid = len(labels)
            parts.append(f"anullsrc=r=24000:cl=mono:d={gap/1000:.3f}[s{lid}]")
            labels.append(f"[s{lid}]")
    fc = ";".join(parts) + ";" + "".join(labels) + \
         f"concat=n={len(labels)}:v=0:a=1[out]"
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", fc, "-map", "[out]",
           "-acodec", "libmp3lame", "-b:a", "64k", "-ar", "24000", "-ac", "1",
           str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=voice.NO_WINDOW)
    if r.returncode != 0:
        print("  拼接失败:", (r.stderr or "")[:200])
        return False
    return out.exists() and out.stat().st_size > 1024


def stats(p: Path) -> dict:
    """时长 + 峰值/均值音量。峰值 ≥ 0 dB 就是削波破音。"""
    r = subprocess.run([FF, "-hide_banner", "-i", str(p),
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, creationflags=voice.NO_WINDOW)
    txt = r.stderr or ""
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", txt)
    peak = re.search(r"max_volume: (-?[\d.]+) dB", txt)
    mean = re.search(r"mean_volume: (-?[\d.]+) dB", txt)
    secs = (int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
            if d else 0.0)
    return {
        "secs": secs,
        "dur": "%.1fs" % secs,
        "peak": float(peak.group(1)) if peak else -99.0,
        "mean": float(mean.group(1)) if mean else -99.0,
    }


# --------------------------------------------------------------------------
# 试听页：音频 base64 内嵌 —— 不依赖相对路径，换台机器拷过去照样能放
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>小柚 · 情绪语音样带</title>
<style>
  :root{
    --bg:#0b0c14; --bg2:#12141f; --card:#171a27; --line:#252a3d;
    --pink:#ff6fae; --cyan:#4fe3d0; --purple:#9d7bff;
    --text:#e8eaf2; --dim:#8b90a8;
  }
  *{box-sizing:border-box}
  body{background:var(--bg); color:var(--text); margin:0; padding:34px 22px 60px;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    line-height:1.75; font-size:15px; -webkit-font-smoothing:antialiased}
  .wrap{max-width:880px; margin:0 auto}
  h1{font-size:24px; margin:0 0 6px}
  h1 span{color:var(--pink)}
  .sub{color:var(--dim); font-size:13.5px; margin-bottom:26px}
  h2{font-size:16px; margin:34px 0 6px; color:var(--cyan);
     border-left:3px solid var(--cyan); padding-left:10px}
  .note{color:var(--dim); font-size:13px; margin:0 0 16px}
  .card{background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:13px 15px; margin-bottom:10px}
  .row{display:flex; align-items:baseline; gap:10px; flex-wrap:wrap}
  .name{font-weight:600; font-size:15px}
  .name em{font-style:normal; color:var(--pink); font-size:12.5px; margin-left:6px}
  .meta{color:var(--dim); font-size:12px; font-family:ui-monospace,Consolas,monospace}
  .desc{color:var(--dim); font-size:13px; margin:3px 0 9px}
  audio{width:100%; height:34px; display:block}
  .warn{color:#ffb347}
  .foot{color:var(--dim); font-size:12.5px; margin-top:34px;
        border-top:1px solid var(--line); padding-top:14px}
  code{background:var(--bg2); padding:1px 5px; border-radius:4px;
       font-family:ui-monospace,Consolas,monospace; font-size:12.5px; color:var(--cyan)}
</style></head><body><div class="wrap">
<h1>小柚 · <span>情绪语音样带</span></h1>
<div class="sub">全部是同一副嗓子（晓伊）。请横着听、竖着听：<b>A 组</b>看"哪种情绪对味"，
<b>B 组</b>看"句内起伏值不值"。挑中的我把参数改成默认，挑不中的直接删。</div>
{body}
<div class="foot">
由 <code>_test/probe_voice_emo.py</code> 生成 · 改了
<code>voice_emo.EMOTIONS</code> 后重跑即可刷新本页<br>
音频以 base64 内嵌在本文件里，可以单独拷走播放。
</div>
</div></body></html>
"""

CARD = """<div class="card">
  <div class="row"><span class="name">{name}{tag}</span>
    <span class="meta">{meta}</span></div>
  <div class="desc">{desc}</div>
  <audio controls preload="none" src="data:audio/mpeg;base64,{b64}"></audio>
</div>"""


def card(name: str, desc: str, path: Path, tag: str = "") -> str:
    st = stats(path)
    peak = "%.1f dB" % st["peak"]
    if st["peak"] >= 0:
        peak = '<span class="warn">%s ⚠破音</span>' % peak
    return CARD.format(
        name=name, tag=tag, desc=desc, b64=base64.b64encode(path.read_bytes()).decode(),
        meta="时长 %s · 峰值 %s" % (st["dur"], peak))


def main() -> None:
    if not FF:
        print("没有 ffmpeg，做不了拼接"); sys.exit(1)
    OUT.mkdir(exist_ok=True)
    for f in OUT.iterdir():
        if f.is_file():
            f.unlink()
    TMP.mkdir(parents=True, exist_ok=True)

    blocks: list[str] = []
    ok = bad = 0

    print("\n=== A. 条级对比（同一句话 × 10 种情绪）===")
    a_cards: list[str] = []
    for key in voice_emo.emotion_keys():
        e = voice_emo.get(key)
        seg = synth(LINE_GRID, e["rate"], e["pitch"], e["volume"], f"a_{key}")
        if not seg:
            print("  %-4s 合成失败" % e["name"]); bad += 1; continue
        dst = OUT / f"A_{e['name']}.mp3"
        dst.write_bytes(seg.read_bytes())
        st = stats(dst)
        print("  %-4s %4s  峰值 %6.1f dB  rate%+d pitch%+d vol%+d"
              % (e["name"], st["dur"], st["peak"], e["rate"], e["pitch"], e["volume"]))
        a_cards.append(card(e["name"], e["desc"], dst,
                            tag="<em>%+d%% / %+dHz / %+d%%</em>"
                                % (e["rate"], e["pitch"], e["volume"])))
        ok += 1
    blocks.append(
        '<h2>A 组 · 条级：一句话十种情绪</h2>'
        '<p class="note">同一句「%s」，只换参数。第一张就是她<b>现在</b>的声音。</p>%s'
        % (LINE_GRID, "".join(a_cards)))

    print("\n=== B. 句内起伏（同一句话：平铺 vs 分段）===")
    b_cards: list[str] = []
    e = voice_emo.get("excited")
    seg = synth(LINE_CONTOUR, e["rate"], e["pitch"], e["volume"], "b_flat")
    if seg:
        dst = OUT / "B1_平铺_整句一组参数.mp3"
        dst.write_bytes(seg.read_bytes())
        print("  B1 平铺  %4s  峰值 %6.1f dB" % (stats(dst)["dur"], stats(dst)["peak"]))
        b_cards.append(card("B1 · 平铺", "整句一组参数 —— 现在的做法，从头到尾一条直线。",
                            dst, tag="<em>对照</em>"))
        ok += 1

    plan = voice_emo.contour(LINE_CONTOUR, "excited", max_segments=4)
    print("  分段计划：")
    segs: list[tuple[Path, int]] = []
    detail: list[str] = []
    for i, s in enumerate(plan):
        print("    第%d段 %-14r rate%+d pitch%+d vol%+d 后停 %dms"
              % (i + 1, s["text"], s["rate"], s["pitch"], s["volume"], s["gap_ms"]))
        detail.append("%s <em>%+d/%+d/%+d</em>停%dms"
                      % (s["text"], s["rate"], s["pitch"], s["volume"], s["gap_ms"]))
        p = synth(s["text"], s["rate"], s["pitch"], s["volume"], f"b_seg{i}")
        if p:
            segs.append((p, s["gap_ms"]))
    if segs:
        dst = OUT / "B2_分段_句内起伏.mp3"
        if join(segs, dst):
            print("  B2 分段  %4s  峰值 %6.1f dB" % (stats(dst)["dur"], stats(dst)["peak"]))
            b_cards.append(card(
                "B2 · 分段起伏",
                "开头最冲、后面一路收，段间补静音当停顿：" + " ｜ ".join(detail),
                dst, tag="<em>对照 B1</em>"))
            ok += 1

    plan3 = voice_emo.contour("主人～人家等你好久好久了喵。", "cute", max_segments=3)
    segs3: list[tuple[Path, int]] = []
    for i, s in enumerate(plan3):
        p = synth(s["text"], s["rate"], s["pitch"], s["volume"], f"b3_seg{i}")
        if p:
            segs3.append((p, s["gap_ms"]))
    if segs3:
        dst = OUT / "B3_撒娇尾巴.mp3"
        if join(segs3, dst):
            print("  B3 撒娇  %4s  峰值 %6.1f dB" % (stats(dst)["dur"], stats(dst)["peak"]))
            b_cards.append(card(
                "B3 · 撒娇的尾巴",
                "故意把开头压住、尾巴拔满 —— 尾音翘上去才是撒娇，"
                "平铺念出来像在报菜名。", dst, tag="<em>重点听尾巴</em>"))
            ok += 1

    blocks.append('<h2>B 组 · 句内：起伏做在客户端</h2>'
                  '<p class="note">免费端点一次请求只给一组参数（<code>&lt;break&gt;</code>'
                  '、<code>&lt;emphasis&gt;</code>、多段 <code>&lt;prosody&gt;</code> 实测全被拒），'
                  '所以起伏＝分段合成＋拼静音。</p>%s' % "".join(b_cards))

    page = OUT / "试听页.html"
    page.write_text(PAGE.replace("{body}", "\n".join(blocks)), encoding="utf-8")
    tmp_kb = page.stat().st_size // 1024
    print("\n产物 → %s" % OUT)
    print("  试听页.html  %d KB（音频已内嵌，双击即可播放）" % tmp_kb)
    print("  成功 %d 份，失败 %d 份" % (ok, bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
