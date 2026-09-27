# -*- coding: utf-8 -*-
"""生成 GPT-SoVITS 的默认参考音色（"底样"）。

为什么需要它
------------
GPT-SoVITS 是**零样本克隆**：它不训练，而是"照着一段 3~10 秒的参考音频
的嗓子和情绪"去念新文本。所以参考音频决定了两件事：

    1. 音色像谁       —— 换个参考音频就是换个人
    2. 情绪什么调     —— 参考音频是兴奋的，念出来就带兴奋

这意味着参考音频必须**干净、单一情绪、无背景音、无拼接静音**。
项目里 `语音情绪样带/` 那批 mp3 是"客户端分段 + 段间插静音"做出来的，
当试听没问题，当参考音频就废了（模型会以为静音是她的说话习惯）。

所以这里单独合成一版：**一整句、不分段、不插静音**，交给 Edge 的晓伊念。
它等价于"把现在的嗓子原样搬进 GPT-SoVITS"，是能立刻跑通的默认值。

想换成真正的专属音色（你自己的录音 / 喜欢的角色）：
直接把这个 wav 换掉，并在同目录放一个同名 .txt 写上**逐字对应的文本**——
prompt_text 和音频对不上，模型念出来会跑调。

用法::

    C:\\...\\envs\\catbot\\Scripts\\python.exe catbot/_make_ref_audio.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # catbot/
ROOT = BASE_DIR.parent                               # 项目根
OUT_DIR = ROOT / "参考音色"

NO_WINDOW = 0x08000000

# 参考音频的文本。**必须和实际念出来的字逐字一致**（标点可省），
# 否则 prompt_text 与音频不匹配，合成会飘。
REF_TEXT = "你好呀，我是小柚，一只住在你手机里的猫娘，以后我会一直陪着你的，请多多关照喵"

# 晓伊 —— 现在的默认嗓子。参考音频的情绪基调会被继承，
# 所以这一段要念得**平常、松弛**，别让底样就带上强烈情绪。
VOICE = "zh-CN-XiaoyiNeural"
RATE = "+8%"
PITCH = "+10Hz"


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    import shutil
    return shutil.which("ffmpeg")


def synth_mp3(dst: Path) -> bool:
    try:
        import edge_tts
    except Exception as e:
        print(f"没有 edge-tts，无法合成参考音频：{e}")
        return False

    async def _go():
        c = edge_tts.Communicate(REF_TEXT, VOICE, rate=RATE, pitch=PITCH)
        await asyncio.wait_for(c.save(str(dst)), timeout=40)

    try:
        asyncio.run(_go())
    except Exception as e:
        print(f"Edge 合成失败：{e}")
        return False
    return dst.exists() and dst.stat().st_size > 1024


def to_wav(src: Path, dst: Path) -> bool:
    """转成 32kHz 单声道 wav —— 这是 GPT-SoVITS 内部的工作采样率。"""
    exe = ffmpeg_exe()
    if not exe:
        print("没找到 ffmpeg")
        return False
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-ar", "32000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=60, creationflags=NO_WINDOW)
    if r.returncode != 0:
        print(f"ffmpeg 失败：{(r.stderr or '')[:200]}")
        return False
    return dst.exists() and dst.stat().st_size > 1024


def wav_seconds(path: Path) -> float:
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mp3 = OUT_DIR / "_tmp_ref.mp3"
    wav = OUT_DIR / "小柚_默认参考.wav"
    txt = OUT_DIR / "小柚_默认参考.txt"

    if not synth_mp3(mp3):
        return 1
    if not to_wav(mp3, wav):
        return 1
    try:
        mp3.unlink(missing_ok=True)
    except Exception:
        pass

    txt.write_text(REF_TEXT, encoding="utf-8")
    dur = wav_seconds(wav)
    print(f"参考音频：{wav}")
    print(f"参考文本：{txt}")
    print(f"时长 {dur:.2f} 秒 / {wav.stat().st_size/1024:.0f} KB")
    if dur < 3:
        print("⚠️ 太短了，建议 3~10 秒（模型需要足够长的嗓子样本）")
    elif dur > 12:
        print("⚠️ 偏长，建议裁到 3~10 秒（太长会拖慢每次合成）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
