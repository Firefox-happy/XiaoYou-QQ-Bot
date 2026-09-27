# -*- coding: utf-8 -*-
"""GPT-SoVITS provider 的单元测试（不需要真的装 GPT-SoVITS）。

怎么做到"不装也能测"
--------------------
`voice.py` 只通过 **HTTP** 跟 GPT-SoVITS 说话，所以这里起一个**本机假服务**
（`http.server`，端口 0 让系统随便给），把 `/tts` 的响应换成一坨真 wav。
于是整条链——探活 → 组请求 → 收字节 → 换容器 → 交付——都能被真正跑一遍，
而不用往显存里搬 2G 模型，也不会因为测试去烧 GPU。

覆盖的坑（都是这类"外挂服务"最容易出事的地方）
    1. 服务不在时**必须**快速判死并往下退，不能把主人晾在那儿
    2. 它出错时回的是 JSON 而不是音频 —— 不能把报错文字当语音发出去
    3. 语速/发挥度越界要夹住（它是拿去做数值计算的，塞个 "快一点" 会崩）
    4. 参考音频丢了要**明确拒绝**，而不是硬合成一段不成调的东西
    5. 探活必须是"问一句就走"，不能顺手做个合成
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 测试进程不要往生产日志里写东西（项目铁律）
os.environ["XIAOYOU_NO_FILE_LOG"] = "1"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import voice  # noqa: E402

OK, BAD = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (OK if cond else BAD).append(name)
    print("  [%s] %s%s" % ("OK" if cond else "!!", name,
                           ("  —— " + extra) if extra else ""))


# ---------------------------------------------------------------- 假服务

def make_wav_bytes(seconds: float = 0.6, rate: int = 32000) -> bytes:
    """造一段合法的 wav（静音）。ffmpeg 只认 RIFF 头，内容无所谓。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


GOOD_WAV = make_wav_bytes()


def make_tone_bytes(seconds: float = 2.0, freq: float = 220.0,
                    amp: float = 0.02, rate: int = 32000) -> bytes:
    """造一段**很轻的正弦波**，用来模拟 GPT-SoVITS 那种偏轻的原始输出。

    这里必须用真实波形、不能用静音：loudnorm 对静音基本等于没事干，
    量出来永远是"已经达标"，那样的测试是假的。
    `amp=0.02` 约合 −34 dBFS 峰值，比它真实的输出（−15 上下）还保守一点。
    """
    import math
    import struct
    buf = io.BytesIO()
    n = int(rate * seconds)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h",
                        int(32767 * amp * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)))
    return buf.getvalue()


QUIET_WAV = make_tone_bytes()


def _ffmpeg_stderr(path: Path, filt: str) -> str:
    """跑一次 ffmpeg 把测量输出捞回来（没装 ffmpeg 就返回空串，测试会跳过）。"""
    exe = voice.ffmpeg_exe()
    if not exe:
        return ""
    try:
        r = subprocess.run(
            [exe, "-hide_banner", "-i", str(path), "-af", filt, "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000)          # 别弹黑窗
        return (r.stderr or "") + (r.stdout or "")
    except Exception:
        return ""


def measure_lufs(path: Path):
    """综合响度（EBU R128，LUFS）。量不出来返回 None。"""
    m = re.findall(r"I:\s*(-?\d+\.\d+)\s*LUFS", _ffmpeg_stderr(path, "ebur128"))
    return float(m[-1]) if m else None


def measure_peak(path: Path):
    """最大采样峰值（dBFS）。量不出来返回 None。"""
    m = re.search(r"max_volume:\s*(-?\d+\.\d+) dB",
                  _ffmpeg_stderr(path, "volumedetect"))
    return float(m.group(1)) if m else None


def wav_info(path: Path) -> tuple:
    """(采样率, 声道数)；读不了返回 (0, 0)。"""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getframerate(), w.getnchannels()
    except Exception:
        return (0, 0)


class FakeHandler(BaseHTTPRequestHandler):
    received: list = []          # 收到的请求体
    mode: str = "ok"             # ok / err400 / json200 / short / quiet

    def log_message(self, *a):   # 别把访问日志刷到测试输出里
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_unparsable": raw[:80].decode("utf-8", "replace")}

        if self.path != "/tts":
            self.send_response(404)
            self.end_headers()
            return

        FakeHandler.received.append(body)
        mode = FakeHandler.mode

        if mode == "err400":
            payload = json.dumps({"message": "text_lang is required"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if mode == "json200":            # 有些错它就是用 200 回 JSON
            payload = json.dumps({"message": "boom"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if mode == "quiet":              # 回一段很轻的真实波形（测响度归一）
            payload = QUIET_WAV
        else:
            payload = GOOD_WAV if mode != "short" else b"RIFFxxxx"
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_fake() -> tuple:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def free_port() -> int:
    """要一个此刻没人监听的端口（用来验证"服务不在"这条路）。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- 开始

print("=" * 62)
print("GPT-SoVITS provider 测试")
print("=" * 62)

tmp = Path(tempfile.mkdtemp(prefix="gsv_test_"))
# 铁律：测试不许往生产目录写东西。`speak()` 会把中间产物写进 `voice.TMP_DIR`，
# 这里把它整体指到本次测试的临时目录，跑完随 tmp 一起清掉。
voice.TMP_DIR = tmp
print("\n[1] 引擎选择 _pick_provider")
for val, want in (("gptsovits", "gptsovits"), ("GPTSOVITS", "gptsovits"),
                  ("auto", "auto"), ("edge", "edge"), ("sapi", "sapi"),
                  ("???", "auto"), ("", "auto"), (None, "auto")):
    got = voice._pick_provider({"tts_provider": val})
    check("tts_provider=%r -> %s" % (val, want), got == want, "得到 " + got)

print("\n[2] 探活：端口没人听时必须判死")
p = free_port()
dead_url = "http://127.0.0.1:%d" % p
t0 = time.time()
alive = voice.gptsovits_alive({"gptsovits_url": dead_url})
dt = time.time() - t0
check("空端口判定为不在", alive is False)
check("判定要快（<1s）", dt < 1.0, "%.3fs" % dt)

print("\n[3] 探活：端口有人听时必须判活")
srv, url = start_fake()
try:
    check("假服务被判定为在", voice.gptsovits_alive({"gptsovits_url": url}) is True)
    check("默认地址可被覆盖", voice.gptsovits_url({"gptsovits_url": url}) == url)
    check("没配置时用默认端口",
          voice.gptsovits_url({}) == voice.GPTSOVITS_DEFAULT_URL)

    print("\n[4] 请求体：字段、语言、切句方式")
    FakeHandler.received.clear()
    FakeHandler.mode = "ok"
    out = tmp / "a.wav"
    good_ref = tmp / "ref.wav"
    good_ref.write_bytes(GOOD_WAV)
    ok = voice._gptsovits_synth("今天好开心呀！", out, url=url,
                                ref_audio=str(good_ref), ref_text="你好呀",
                                ref_lang="zh")
    check("合成返回 True", ok is True)
    check("产物是可用的 wav", out.exists() and out.stat().st_size > 1024)
    check("确实发了一次请求", len(FakeHandler.received) == 1)
    body = FakeHandler.received[0] if FakeHandler.received else {}
    check("text 原样传过去", body.get("text") == "今天好开心呀！")
    check("text_lang=zh", body.get("text_lang") == "zh")
    check("prompt_lang=zh", body.get("prompt_lang") == "zh")
    check("prompt_text 用调用方给的", body.get("prompt_text") == "你好呀")
    check("ref_audio_path 是绝对路径",
          str(body.get("ref_audio_path", "")).endswith("ref.wav"))
    check("默认不切句（cut0）", body.get("text_split_method") == "cut0")
    check("非流式", body.get("streaming_mode") is False)
    check("媒体类型 wav", body.get("media_type") == "wav")

    print("\n[5] 参数夹取：越界的语速/发挥度必须被夹住")
    FakeHandler.received.clear()
    voice._gptsovits_synth("测试", tmp / "b.wav", url=url,
                           ref_audio=str(good_ref), speed=99.0,
                           temperature=-5.0)
    b = FakeHandler.received[0]
    check("语速夹到上限 2.0", b.get("speed_factor") == 2.0, str(b.get("speed_factor")))
    check("发挥度夹到下限 0.1", b.get("temperature") == 0.1, str(b.get("temperature")))

    FakeHandler.received.clear()
    voice._gptsovits_synth("测试", tmp / "c.wav", url=url,
                           ref_audio=str(good_ref), speed=0.01,
                           temperature=99.0)
    b = FakeHandler.received[0]
    check("语速夹到下限 0.5", b.get("speed_factor") == 0.5, str(b.get("speed_factor")))
    check("发挥度夹到上限 1.5", b.get("temperature") == 1.5, str(b.get("temperature")))

    print("\n[6] 出错路径：都得软着陆，不许抛异常")
    FakeHandler.mode = "err400"
    check("HTTP 400 返回 False",
          voice._gptsovits_synth("测试", tmp / "d.wav", url=url,
                                 ref_audio=str(good_ref)) is False)
    FakeHandler.mode = "json200"
    check("200 但内容是 JSON 也返回 False",
          voice._gptsovits_synth("测试", tmp / "e.wav", url=url,
                                 ref_audio=str(good_ref)) is False)
    check("坏的 200 不会留下垃圾文件", not (tmp / "e.wav").exists()
          or (tmp / "e.wav").stat().st_size == 0)
    FakeHandler.mode = "short"
    check("太短的内容视为无效",
          voice._gptsovits_synth("测试", tmp / "f.wav", url=url,
                                 ref_audio=str(good_ref)) is False)
    FakeHandler.mode = "ok"

    check("连不上就返回 False（不抛）",
          voice._gptsovits_synth("测试", tmp / "g.wav", url=dead_url,
                                 ref_audio=str(good_ref)) is False)
    check("空文本直接拒绝",
          voice._gptsovits_synth("   ", tmp / "h.wav", url=url,
                                 ref_audio=str(good_ref)) is False)
    check("参考音频不存在时明确拒绝",
          voice._gptsovits_synth("测试", tmp / "i.wav", url=url,
                                 ref_audio=str(tmp / "没有这个.wav")) is False)

    print("\n[7] 参考文本的查找顺序")
    side = tmp / "有文本.wav"
    side.write_bytes(GOOD_WAV)
    side.with_suffix(".txt").write_text("同名文本", encoding="utf-8")
    check("优先用同名 .txt", voice.ref_text_for(side) == "同名文本")
    no_side = tmp / "没文本.wav"
    no_side.write_bytes(GOOD_WAV)
    if voice.DEFAULT_REF_TEXT_FILE.exists():
        default_txt = voice.DEFAULT_REF_TEXT_FILE.read_text(encoding="utf-8").strip()
        check("没有 .txt 时退回默认那份",
              voice.ref_text_for(no_side) == default_txt, repr(default_txt[:20]))
        check("默认参考音频确实存在", voice.DEFAULT_REF_AUDIO.exists())
    else:
        # 「参考音色/」是**用户自己的**录音样本，不进仓库（见 .gitignore）。
        # 别人刚克隆下来没有这份，跳过这两条即可，不是失败。
        print("  [--] 跳过默认参考音频检查（参考音色/ 还没生成，属正常）")

    print("\n[8] speak()：整条链跑通 + 交付格式")
    FakeHandler.received.clear()
    prof = {"tts_provider": "gptsovits", "gptsovits_url": url,
            "gptsovits_ref_audio": str(good_ref), "gptsovits_ref_text": "你好呀"}
    pth = voice.speak("她今天心情很好喵", profile=prof)
    check("拿到交付文件", pth is not None and Path(pth).exists(),
          str(pth))
    check("默认交付 mp3", str(pth).lower().endswith(".mp3"), str(pth))
    check("mp3 有内容", Path(pth).stat().st_size > 1024 if pth else False)
    check("走的是 GPT-SoVITS 而不是 Edge", len(FakeHandler.received) == 1)

    pth_wav = voice.speak("给我一条 wav", fmt="wav", profile=prof)
    check("要 wav 就给 wav",
          pth_wav is not None and str(pth_wav).lower().endswith(".wav"),
          str(pth_wav))

    print("\n[9] speak()：服务不在时退回 Edge，绝不哑掉")
    bad_prof = dict(prof, gptsovits_url=dead_url)
    edge_called = {"n": 0}
    real_edge = voice._edge_synth

    def fake_edge(*a, **kw):
        edge_called["n"] += 1
        return real_edge(*a, **kw)

    voice._edge_synth = fake_edge
    try:
        pth2 = voice.speak("退回测试", profile=bad_prof)
        check("服务不在仍拿到文件", pth2 is not None and Path(pth2).exists())
        check("确实退到了 Edge", edge_called["n"] == 1)
    finally:
        voice._edge_synth = real_edge

    print("\n[10] 状态面板的说法")
    live_prof = {"tts_provider": "auto", "gptsovits_url": url}
    check("auto 且服务在 → 报 gptsovits",
          voice.tts_engine(live_prof) == "gptsovits")
    check("描述里点名 GPT-SoVITS",
          "GPT-SoVITS" in voice.describe_engine(live_prof),
          voice.describe_engine(live_prof))
    check("auto 且服务不在 → 报 edge（或 sapi）",
          voice.tts_engine({"tts_provider": "auto",
                            "gptsovits_url": dead_url}) in ("edge", "sapi"))
    check("明确选 sapi 时不报 gptsovits",
          voice.tts_engine({"tts_provider": "sapi"}) != "gptsovits")
    check("服务在时也算「能开口」", voice.whisper_supported() is True)

    print("\n[11] 软着陆 _safe_float")
    for val, want in ((1.2, 1.2), ("0.9", 0.9), ("快一点", 1.0), (None, 1.0),
                      (True, 1.0), ("", 1.0), (2, 2.0)):
        got = voice._safe_float(val, 1.0)
        check("_safe_float(%r) -> %r" % (val, want), got == want, repr(got))

    print("\n[12] 离谱配置不该把它弄崩")
    for weird in ({}, None, {"tts_provider": "gptsovits"},
                  {"tts_provider": "gptsovits", "gptsovits_url": "http://"},
                  {"tts_provider": "gptsovits", "gptsovits_url": "不是网址"},
                  {"tts_provider": "gptsovits", "gptsovits_speed": "飞快"}):
        try:
            voice.gptsovits_alive(weird or {})
            voice.gptsovits_url(weird or {})
            check("配置 %r 不崩" % (weird,), True)
        except Exception as e:
            check("配置 %r 不崩" % (weird,), False, repr(e))

    # ------------------------------------------------------------------
    # 响度归一
    #
    # 这是"改用本机克隆"后**最容易漏掉、但用户第一下就能听出来**的问题：
    # 它的原始输出比 Edge 轻 8~10 dB（实测约 −30 vs −22 LUFS），
    # 不做归一，用户会以为"换了引擎她怎么没声音了"。
    # ------------------------------------------------------------------
    print("\n[13] 响度归一：把偏轻的原始输出拉到正常音量")
    quiet = tmp / "quiet.wav"
    quiet.write_bytes(QUIET_WAV)

    raw_lufs = measure_lufs(quiet)
    check("素材本身确实很轻（< −24 LUFS）",
          raw_lufs is not None and raw_lufs < -24.0,
          ("%.1f LUFS" % raw_lufs) if raw_lufs is not None else "量不了（缺 ffmpeg）")

    norm = tmp / "norm.wav"
    ok_norm = voice._normalize_loudness(quiet, norm, -16.0)
    check("归一返回 True", ok_norm is True)
    check("产出非空", norm.exists() and norm.stat().st_size > 1024)

    got_lufs = measure_lufs(norm)
    check("归一后接近 −16 LUFS（±3 LU）",
          got_lufs is not None and abs(got_lufs + 16.0) <= 3.0,
          ("%.1f LUFS" % got_lufs) if got_lufs is not None else "量不了（缺 ffmpeg）")

    rate, ch = wav_info(norm)
    check("输出直接就是 24kHz 单声道（省一次重采样）",
          (rate, ch) == (24000, 1), "%dHz %d声道" % (rate, ch))

    peak = measure_peak(norm)
    check("真峰值留着余量、不削波（≤ −1 dBFS）",
          peak is not None and peak <= -1.0,
          ("%.1f dB" % peak) if peak is not None else "量不了（缺 ffmpeg）")

    check("比原始素材响得多（至少 +8 dB 感知响度）",
          raw_lufs is not None and got_lufs is not None
          and got_lufs - raw_lufs >= 8.0,
          ("%.1f → %.1f LUFS" % (raw_lufs, got_lufs))
          if (raw_lufs is not None and got_lufs is not None) else "量不了")

    print("\n[14] 归一可以关掉；坏输入只能软着陆")
    check("loudness=0 ＝ 关掉，返回 False",
          voice._normalize_loudness(quiet, tmp / "z0.wav", 0) is False)
    check("loudness 为正数也当关掉",
          voice._normalize_loudness(quiet, tmp / "z1.wav", 5) is False)
    check("loudness 写成中文不崩（退回默认值）",
          isinstance(voice._normalize_loudness(quiet, tmp / "z2.wav", "响一点"),
                     bool))
    check("源文件不存在返回 False",
          voice._normalize_loudness(tmp / "nope.wav",
                                    tmp / "z3.wav", -16) is False)
    broken = tmp / "broken.wav"
    broken.write_bytes(b"RIFF" + b"\x00" * 4000)      # 连头都不完整
    check("损坏的 wav 返回 False 且不抛异常",
          voice._normalize_loudness(broken, tmp / "z4.wav", -16) is False)

    print("\n[15] speak() 交付时真的过了一遍归一")
    FakeHandler.received.clear()
    FakeHandler.mode = "quiet"          # 假服务回一段偏轻的波形
    prof_norm = dict(prof, gptsovits_loudness=-16.0)
    p_norm = voice.speak("归一路径", fmt="wav", profile=prof_norm)
    check("交付文件拿到", p_norm is not None and Path(p_norm).exists(), str(p_norm))
    if p_norm and Path(p_norm).exists():
        r2, c2 = wav_info(Path(p_norm))
        check("交付的 wav 已被归一（成了 24kHz 单声道）",
              (r2, c2) == (24000, 1), "%dHz %d声道" % (r2, c2))
        l2 = measure_lufs(Path(p_norm))
        check("交付的 wav 确实被拉响了（≥ −22 LUFS）",
              l2 is not None and l2 >= -22.0,
              ("%.1f LUFS" % l2) if l2 is not None else "量不了（缺 ffmpeg）")

    FakeHandler.received.clear()
    prof_off = dict(prof, gptsovits_loudness=0)
    p_off = voice.speak("不归一路径", fmt="wav", profile=prof_off)
    check("loudness=0 时交付文件仍然拿到",
          p_off is not None and Path(p_off).exists(), str(p_off))
    if p_off and Path(p_off).exists():
        r3, c3 = wav_info(Path(p_off))
        check("关掉归一后交付的是服务原样的 32kHz",
              r3 == 32000, "%dHz" % r3)

    # ------------------------------------------------------------------
    # 状态面板
    #
    # 这一节是补**真实踩过的坑**：`service_ctl.py` 里有个
    # `tts_engine_name()`，当初为了"不 import voice 省时间"自己判断了一遍 ——
    # 只看 `edge_tts` 能不能 import，于是加了本机 GPT-SoVITS 之后，
    # 面板**照旧报「Edge TTS」**，服务明明在跑、显示却是错的。
    # 教训：**同一个判断在两处各写一遍，就一定有一处会过期。**
    # ------------------------------------------------------------------
    print("\n[16] 状态面板的引擎名必须跟着真实引擎走")
    import service_ctl

    # ① 它得**委托**给 voice，而不是自己再判断一遍
    real_desc = voice.describe_engine
    voice.describe_engine = lambda prof=None: "哨兵-只可能来自 voice"
    try:
        got = service_ctl.tts_engine_name()
        check("面板的引擎名取自 voice.describe_engine",
              got == "哨兵-只可能来自 voice", got)
    finally:
        voice.describe_engine = real_desc

    # ② 真读一份配置：服务在 → 报 GPT-SoVITS；选 sapi → 不报；服务不在 → 退下去
    panel_cfg = tmp / "panel_config.json"
    real_cfg_file = service_ctl.CONFIG_FILE
    service_ctl.CONFIG_FILE = panel_cfg
    try:
        panel_cfg.write_text(json.dumps(
            {"voice": {"tts_provider": "auto", "gptsovits_url": url}}),
            encoding="utf-8")
        n1 = service_ctl.tts_engine_name()
        check("服务在 + auto → 面板报 GPT-SoVITS", "GPT-SoVITS" in n1, n1)

        panel_cfg.write_text(json.dumps({"voice": {"tts_provider": "sapi"}}),
                             encoding="utf-8")
        n2 = service_ctl.tts_engine_name()
        check("明确选 sapi → 面板不报 GPT-SoVITS", "GPT-SoVITS" not in n2, n2)

        panel_cfg.write_text(json.dumps(
            {"voice": {"tts_provider": "auto", "gptsovits_url": dead_url}}),
            encoding="utf-8")
        n3 = service_ctl.tts_engine_name()
        check("服务不在 + auto → 面板往下退（Edge / 系统语音）",
              "Edge" in n3 or n3 == "系统语音", n3)

        panel_cfg.write_text("{ 这不是 json", encoding="utf-8")
        check("配置坏了也不能崩，返回一个字符串",
              isinstance(service_ctl.tts_engine_name(), str))
    finally:
        service_ctl.CONFIG_FILE = real_cfg_file

    # ③ import 它本身不该有副作用
    #    真实案例：它的 `_setup_console()` 原本写在**模块顶层**，
    #    于是任何 import 它的进程 stdout 都会被改成 GBK ——
    #    测试脚本 import 完，自己整份输出全变乱码。
    import importlib
    enc_before = sys.stdout.encoding
    importlib.reload(service_ctl)
    check("import service_ctl 不改动调用方的 stdout 编码",
          sys.stdout.encoding == enc_before,
          "%r → %r" % (enc_before, sys.stdout.encoding))

finally:
    srv.shutdown()
    srv.server_close()

# 收拾临时目录（显式清单，不用通配符 —— 项目铁律）
for f in tmp.iterdir():
    try:
        f.unlink()
    except Exception:
        pass
try:
    tmp.rmdir()
except Exception:
    pass

print("\n" + "=" * 62)
print("通过 %d 项，失败 %d 项" % (len(OK), len(BAD)))
if BAD:
    print("失败的项：")
    for b in BAD:
        print("  -", b)
print("=" * 62)
sys.exit(1 if BAD else 0)
