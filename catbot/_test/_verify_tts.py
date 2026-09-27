# -*- coding: utf-8 -*-
"""验收新的语音链路：Edge TTS 能否出音、降级是否生效、参数是否管用。"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import voice  # noqa: E402

OUT = HERE / "_tts_result.txt"
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    lines.append(s)
    print(s)


log("=" * 60)
log("edge_available   :", voice.edge_available())
log("whisper_supported:", voice.whisper_supported())
log("describe(edge)   :", voice.describe_engine({"tts_provider": "edge", "edge_voice": "zh-CN-XiaoyiNeural"}))
log("describe(sapi)   :", voice.describe_engine({"tts_provider": "sapi"}))
log("可选音色          :", " / ".join(v["name"] for v in voice.edge_voices()))
log()

TEXT = "主人主人，小柚换新嗓子啦，你听听这个声音喜不喜欢呀～"

log("-" * 60)
log("① 两条路线各合成一条")
for tag, prof in (("edge", {"tts_provider": "edge", "edge_voice": "zh-CN-XiaoyiNeural",
                            "edge_rate": 8, "edge_pitch": 10}),
                  ("sapi", {"tts_provider": "sapi"})):
    t0 = time.time()
    p = voice.speak(TEXT, rate=1, profile=prof)
    dt = time.time() - t0
    if p and p.exists():
        log("  %-5s OK   %5.2fs  %-28s %7d bytes" % (tag, dt, p.name, p.stat().st_size))
        p.unlink(missing_ok=True)
    else:
        log("  %-5s FAIL %5.2fs" % (tag, dt))
log()

log("-" * 60)
log("② 降级：假装没装 edge-tts，auto 应自动走 SAPI")
_orig = voice.edge_available
voice.edge_available = lambda: False
try:
    t0 = time.time()
    p = voice.speak("断网的时候也要能说话哦", profile={"tts_provider": "auto"})
    log("  auto 降级 -> %s  %.2fs  %s" % (
        "OK" if p else "FAIL", time.time() - t0,
        p.name if p else "-"))
    if p:
        p.unlink(missing_ok=True)
finally:
    voice.edge_available = _orig
log()

log("-" * 60)
log("③ 音色切换：换一个音色，产物应不同（文件大小会变）")
for short in ("zh-CN-XiaoyiNeural", "zh-CN-XiaoxiaoNeural", "zh-CN-YunjianNeural"):
    t0 = time.time()
    p = voice.speak("这是一句用来对比音色的测试话。",
                    profile={"tts_provider": "edge", "edge_voice": short,
                             "edge_rate": 0, "edge_pitch": 0})
    if p and p.exists():
        log("  %-30s OK  %6d bytes  %.2fs" % (short, p.stat().st_size, time.time() - t0))
        p.unlink(missing_ok=True)
    else:
        log("  %-30s FAIL" % short)
log()

log("-" * 60)
log("④ 音调参数：pitch 高/低应产生不同波形（大小会变）")
for pitch in (-20, 0, 25):
    p = voice.speak("喵喵喵喵喵。",
                    profile={"tts_provider": "edge", "edge_voice": "zh-CN-XiaoyiNeural",
                             "edge_rate": 0, "edge_pitch": pitch})
    if p and p.exists():
        log("  pitch=%+4dHz  %6d bytes" % (pitch, p.stat().st_size))
        p.unlink(missing_ok=True)
    else:
        log("  pitch=%+4dHz  FAIL" % pitch)
log()

log("-" * 60)
log("⑤ 非法 profile 不应崩")
for bad in ({}, {"tts_provider": "???", "edge_rate": "abc"}, None):
    try:
        p = voice.speak("容错测试", profile=bad)
        log("  profile=%-32r -> %s" % (bad, "OK" if p else "None（没崩）"))
        if p:
            p.unlink(missing_ok=True)
    except Exception as e:
        log("  profile=%r -> 抛异常 %s: %s  ← 不该发生" % (bad, type(e).__name__, e))
log()

OUT.write_text("\n".join(lines), encoding="utf-8")
print("\nwritten", OUT)
