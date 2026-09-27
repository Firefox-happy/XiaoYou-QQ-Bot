# -*- coding: utf-8 -*-
"""语音模块闭环自测：SAPI 说一句 → ffmpeg 转码 → vosk 听写回来。
一次验证合成、转码、识别三段链路。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import voice

OUT = []
def log(*a):
    OUT.append(" ".join(str(x) for x in a))

log("=== 链路可用性 ===")
log(f"  ffmpeg      : {voice.ffmpeg_exe()}")
log(f"  模型在位    : {voice.model_ready()}")
log(f"  听写就绪    : {voice.ready()}")
log(f"  已装语音    : {voice.voices_installed()}")
log(f"  能说中文    : {voice.whisper_supported()}")

log("")
log("=== 1. 合成（她说）===")
SENT = "主人早上好呀，今天北京晴，十八到二十六度，记得吃早饭喵"
t0 = __import__("time").time()
mp3 = voice.speak(SENT, rate=1)
log(f"  输入: {SENT}")
if mp3:
    log(f"  产出: {mp3.name}  {mp3.stat().st_size} B  {__import__('time').time()-t0:.1f}s")
    log(f"  格式: {mp3.suffix}")
else:
    log("  失败：没产出语音文件")

log("")
log("=== 2. 听写（她听）===")
if mp3:
    # 2a. 直接听 mp3（跨格式，最贴近真实场景）
    t0 = __import__("time").time()
    heard = voice.hear(mp3)
    log(f"  [听 mp3] {__import__('time').time()-t0:.1f}s -> {heard!r}")

    # 2b. 听 wav 原文件
    wav = voice.text_to_wav(SENT, BASE / "tmp" / "roundtrip.wav")
    log(f"  [合成 wav] {'OK' if wav else 'FAIL'}")
    if wav:
        t0 = __import__("time").time()
        h2 = voice.hear(BASE / "tmp" / "roundtrip.wav")
        log(f"  [听 wav] {__import__('time').time()-t0:.1f}s -> {h2!r}")

    # 2c. 短句测试
    for s in ["你好呀", "今天天气怎么样", "帮我查一下小米十七的价格"]:
        f = voice.speak(s, rate=1)
        if f:
            got = voice.hear(f)
            log(f"  [短句] 说={s!r} 听={got!r}")
        else:
            log(f"  [短句] 合成失败: {s}")

log("")
log("=== 3. 异常输入（不能崩）===")
bad = BASE / "tmp" / "not_audio.bin"
bad.write_bytes(b"this is not audio at all" * 100)
log(f"  喂垃圾数据 -> {voice.hear(bad)!r}")
log(f"  喂不存在的文件 -> {voice.hear(BASE / 'tmp' / 'nope.silk')!r}")
log(f"  空文本合成 -> {voice.speak('')}")

(BASE / "_test" / "voice_out.txt").write_text("\n".join(OUT), encoding="utf-8")
print("done")
