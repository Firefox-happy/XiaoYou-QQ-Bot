# -*- coding: utf-8 -*-
"""
小柚 · 语音模块
================

让小柚听得见、也开得了口：

    hear            语音文件 → 中文文字（解码 + 离线识别一条龙）
    speak           文字 → 可发给 QQ 的语音文件

设计原则
--------
1. **听的离线，说的默认联网、可退离线。**

   - 识别（耳）：本机 vosk 中文模型，不要 Key、不要联网 —— 这台机器没有
     代理，任何"免费在线 ASR"实测都不可用，离线是唯一稳的路。
   - 合成（口）：**三级兜底**，保证她永远开得了口。首选本机 GPT-SoVITS
     （离线、可克隆音色、情绪最好，但要先把那个服务起起来）；不在就用
     Edge TTS（微软同一批神经网络音色，免 Key、免 GPU，实测 1.5 秒一条）；
     Edge 是**联网**的，失败再退回 Windows SAPI。

   （原文只有"全部离线"一条路，换成 SAPI 的 Huihui —— 那是十几年前的
   拼接式合成，念出来像导航播报。Edge 换上去是听感上最明显的一次提升。）

2. **不依赖 C 编译工具链。** 原本用 pilk 解 QQ 的 silk 格式，但它需要
   Microsoft Visual C++ 14.0 编译 `pilk._pilk` 扩展 —— 本机没有，
   `pip install pilk` 直接失败（`error: Microsoft Visual C++ 14.0 or
   greater is required`）。改用两条不编译的路：
     - **ffmpeg**（来自 imageio-ffmpeg 的预编译二进制，有 wheel）
     - **让协议层转码**：NapCat 本身必须内置 silk 编解码（否则它发不出
       语音），所以请它把 silk 转成 mp3 再给我们就行。

3. **采样率在源头对齐 16kHz。** vosk 只吃 16kHz 单声道；不管来的是什么
   格式，一律由 ffmpeg 统一转成 16k/mono/s16le。合成方向则让 SAPI 直接
   输出 16k wav。两头对齐，中间不需要退化成年久失修的 audioop
   （Python 3.13 已移除它）。

4. **失败一律返回 False / 空串，绝不抛异常。** 语音是锦上添花，
   它坏掉不该把聊天拖哑。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("xiaoyou.voice")

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "models" / "vosk-model-small-cn-0.22"
TMP_DIR = BASE_DIR / "tmp"

NO_WINDOW = 0x08000000              # 不弹黑窗口
SAMPLE_RATE = 16000
MAX_PCM_BYTES = SAMPLE_RATE * 2 * 90     # 最多听 90 秒，防止超长音频拖住

# 中文女声。这台机器实测有 Huihui Desktop（zh-CN）；Zira 是英文的，
# 选错会读出洋腔中文，所以只认中文名。
CN_VOICES = ("Microsoft Huihui Desktop", "Microsoft Yaoyao Desktop",
             "Microsoft Xiaoxiao", "Microsoft Xiaoyi")


# --------------------------------------------------------------------------
# 外部工具定位
# --------------------------------------------------------------------------

def ffmpeg_exe() -> str | None:
    """找 ffmpeg：优先 imageio-ffmpeg 自带的预编译二进制，其次 PATH。"""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _run_ffmpeg(args: list[str], timeout: int = 60) -> bool:
    exe = ffmpeg_exe()
    if not exe:
        logger.warning("没找到 ffmpeg，无法转换音频格式")
        return False
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, creationflags=NO_WINDOW)
        if r.returncode != 0:
            logger.warning("ffmpeg 失败: %s", (r.stderr or "")[:200])
            return False
        return True
    except Exception as e:
        logger.warning("调 ffmpeg 出错: %s", e)
        return False


# --------------------------------------------------------------------------
# 模型：懒加载 + 加锁
#
# 加载 42MB 模型要几百毫秒、占一百多 MB 内存。机器人可能一整天不碰语音，
# 所以第一次真正要用时才加载；加载动作不是线程安全的，加锁。
# --------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_model_failed = False


def model_ready() -> bool:
    """模型文件是否在位（不实际加载）。"""
    return (MODEL_DIR / "am" / "final.mdl").exists()


def _get_model():
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    with _model_lock:
        if _model is not None or _model_failed:
            return _model
        if not model_ready():
            logger.warning("没找到 vosk 中文模型 %s —— 听写不可用", MODEL_DIR)
            _model_failed = True
            return None
        try:
            from vosk import Model, SetLogLevel
            SetLogLevel(-1)          # 屏蔽 kaldi 的 LOG 噪音
            t0 = time.time()
            _model = Model(str(MODEL_DIR))
            logger.info("语音识别模型已加载（%.1fs）", time.time() - t0)
        except Exception:
            logger.exception("语音识别模型加载失败，本次运行不再重试")
            _model_failed = True
    return _model


def warmup() -> bool:
    """预热：启动时调一下，免得第一条语音卡两秒。失败不影响主流程。"""
    return _get_model() is not None


def ready() -> bool:
    """听写链路是否可用（模型在位 + 有 ffmpeg）。"""
    return model_ready() and bool(ffmpeg_exe())


# --------------------------------------------------------------------------
# 听：语音 → 文字
# --------------------------------------------------------------------------

def to_pcm16k(src: Path, dst: Path) -> bool:
    """任意音频 → 16kHz 单声道 16bit 裸 PCM。

    ffmpeg 认得的格式都能转（wav / mp3 / amr / ogg / m4a …）。
    **silk 不在其中** —— 那是腾讯私有格式，必须提前让协议层转成别的格式。
    """
    if not src.exists() or src.stat().st_size == 0:
        return False
    # 已经是目标格式的 wav 就不劳烦 ffmpeg 了
    try:
        if src.read_bytes()[:4] == b"RIFF" and _wav_is_pcm16k(src, dst):
            return True
    except Exception:
        pass
    return _run_ffmpeg([
        "-i", str(src),
        "-ar", str(SAMPLE_RATE), "-ac", "1",
        "-f", "s16le", "-acodec", "pcm_s16le",
        str(dst),
    ]) and dst.exists() and dst.stat().st_size > 0


def _wav_is_pcm16k(src: Path, dst: Path) -> bool:
    """已经是 16k/mono/16bit 的 wav 就直接剥壳。"""
    import wave
    try:
        with wave.open(str(src), "rb") as w:
            if (w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1
                    or w.getsampwidth() != 2):
                return False
            data = w.readframes(w.getnframes())
        if not data:
            return False
        dst.write_bytes(data)
        return True
    except Exception:
        return False


def transcribe(pcm_path: Path) -> str:
    """PCM 文件 → 中文文字。失败返回空串。"""
    model = _get_model()
    if model is None:
        return ""
    try:
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(model, SAMPLE_RATE)
        with open(pcm_path, "rb") as f:
            read = 0
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                read += len(chunk)
                rec.AcceptWaveform(chunk)
                if read >= MAX_PCM_BYTES:
                    logger.warning("语音过长，只识别前半段")
                    break
        text = json.loads(rec.FinalResult()).get("text", "") or ""
        return "".join(text.split())      # vosk 中文结果按词带空格，拼回去
    except Exception as e:
        logger.warning("语音识别失败: %s", e)
        return ""


def hear(src: Path) -> str:
    """一步到位：语音文件 → 文字（"" 表示听不清 / 不可用）。"""
    TMP_DIR.mkdir(exist_ok=True)
    pcm = TMP_DIR / f"in_{int(time.time()*1000)}_{threading.get_ident()}.pcm"
    try:
        if not to_pcm16k(src, pcm):
            return ""
        return transcribe(pcm)
    finally:
        try:
            pcm.unlink(missing_ok=True)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 说 · 方式一：Edge TTS（微软神经网络音色）
#
# 为什么加它：Windows 自带的 SAPI 在这台机器上只有 Huihui 一个中文女声，
# 而且是十几年前的拼接式合成，念出来一股"导航播报"味。Edge TTS 走的是
# 微软 Azure 同一批神经网络音色，免费、免 Key、不用 GPU，实测 1.5 秒出
# 一条 3 秒的语音。中文可选 8 个，还能自己调语速/音调（调高音调就萝莉）。
#
# 它是**联网**的，所以失败必须能自动退回 SAPI —— 断网时她照样能开口，
# 只是音色变回老样子。这个降级是本模块最重要的行为。
# --------------------------------------------------------------------------

# 短名给 edge_tts，中文名+说明给设置页和状态面板看。
# 只收录实测存在的 zh-CN 音色（列全了，多出来的在别的 locale）。
EDGE_VOICES: dict[str, tuple[str, str]] = {
    "zh-CN-XiaoyiNeural":           ("晓伊", "活泼少女 —— 最像小柚"),
    "zh-CN-XiaoxiaoNeural":         ("晓晓", "温暖亲切 —— 稳"),
    "zh-CN-liaoning-XiaobeiNeural": ("晓北", "东北口音 —— 逗"),
    "zh-CN-shaanxi-XiaoniNeural":   ("晓妮", "陕西口音 —— 亮"),
    "zh-CN-YunxiNeural":            ("云希", "青年男声 —— 阳光"),
    "zh-CN-YunjianNeural":          ("云健", "男声 —— 激昂"),
    "zh-CN-YunxiaNeural":           ("云夏", "男声 —— 软"),
}

DEFAULT_EDGE_VOICE = "zh-CN-XiaoyiNeural"

# 合成一条语音最多等多久。edge 正常 1~2 秒，给到 25 秒是留给网络抽风，
# 再久就该放弃走兜底了 —— 主人等 25 秒才收到回复已经很离谱。
EDGE_TIMEOUT = 25

_SAPI_HINTS = ("Huihui", "Yaoyao", "Xiaoxiao", "Xiaoyi")


def edge_available() -> bool:
    """装没装 edge-tts。只看模块在不在，不发网络请求。"""
    try:
        import importlib.util
        return importlib.util.find_spec("edge_tts") is not None
    except Exception:
        return False


def _edge_synth(text: str, out_mp3: Path, voice: str,
                rate: int = 8, pitch: int = 10, volume: int = 0,
                timeout: int = EDGE_TIMEOUT) -> bool:
    """文字 → mp3（Edge TTS）。失败返回 False，绝不抛异常。

    rate/pitch/volume 的取值都按"百分比/Hz 整数"收，这里再拼成 edge 要的
    "+8%" / "+10Hz" 字符串 —— 让上层配置保持好读的整数，翻译只在一处做。
    """
    text = (text or "").strip()
    if not text:
        return False

    try:
        import asyncio
        import edge_tts
    except Exception as e:
        logger.info("没装 edge-tts，用不了它：%s", e)
        return False

    voice = voice if voice in EDGE_VOICES else DEFAULT_EDGE_VOICE
    # 越界的值 edge-tts 会直接报错，先夹到它认的范围内。语速上限给到 +100%
    # 已经快得听不清了，再高没有意义。
    rate = max(-50, min(100, _safe_int(rate, 0)))
    pitch = max(-100, min(100, _safe_int(pitch, 0)))
    volume = max(-100, min(100, _safe_int(volume, 0)))
    rate_s = "%+d%%" % rate
    pitch_s = "%+dHz" % pitch
    vol_s = "%+d%%" % volume

    async def _go():
        c = edge_tts.Communicate(text, voice, rate=rate_s,
                                 pitch=pitch_s, volume=vol_s)
        await asyncio.wait_for(c.save(str(out_mp3)), timeout=timeout)

    try:
        # bot 的 worker 是普通线程、没有运行中的事件循环，run 是安全的；
        # 万一将来挪进 async 上下文，这里会报错而不是静默出乱码，反而好排查。
        asyncio.run(_go())
    except Exception as e:
        logger.warning("Edge TTS 合成失败（%s）：%s", voice, str(e)[:160])
        return False

    try:
        return out_mp3.exists() and out_mp3.stat().st_size > 1024
    except Exception:
        return False


def edge_voices() -> list[dict]:
    """给设置页/状态面板：可选音色清单。"""
    return [{"short": k, "name": v[0], "desc": v[1]} for k, v in EDGE_VOICES.items()]


# --------------------------------------------------------------------------
# 说 · 方式三：GPT-SoVITS（本机、离线、可克隆音色）—— 现在音色最好的那条路
#
# 为什么加它：Edge 的免费端点在 SSML 上被阉得很厉害（一次请求只认一个
# <prosody>，<break>、mstts:express-as 全不接），想让它"有情绪"就得在客户端
# 把一句话切成好几段分别合成 —— 代价是**句读被切碎，听着一顿一顿**。
#
# GPT-SoVITS 是本地跑的开源模型（MIT），不联网、不限量、不要钱，而且是
# **零样本克隆**：给它一段 3~10 秒的参考音频，它照着那个嗓子念新文本。
# 情绪同样来自参考音频 —— 换一段兴奋的底样，念出来就带兴奋。
#
# 它**不在本进程里跑**，而是一个独立的本机服务（api_v2.py，默认 9880）。
# 这样机器人不用装 torch（好几个 G），服务挂了也顶多"换个嗓子"，不拖累聊天。
#
# 和 Edge 的关系：它是**加分项**，不是**硬依赖**。服务没起来就走 Edge，
# Edge 也不行才退 SAPI —— 这条链任何一环断了，都不该让她变成哑巴。
# --------------------------------------------------------------------------

GPTSOVITS_DEFAULT_URL = "http://127.0.0.1:9880"

# 探活超时。别信「本机连接应该是毫秒级」这句话 —— 这台机器上往一个
# 没人监听的本地端口发 connect 不会立刻被拒，而是一路等到超时
# （实测稳定 1.5 秒）。所以这个值必须小，否则服务没起来时每条语音
# 都要白等这么久。
GPTSOVITS_PROBE_SEC = 0.35

# 判「不在」的结果缓存这么久。判「在」不缓存：服务随时可能挂，
# 缓存会让我们对着一个已经死掉的服务一直发请求。
GPTSOVITS_PROBE_TTL = 10.0
_probe_cache: dict = {}
_probe_lock = threading.Lock()

# 合成超时。第一次请求要加载参考音频，两三秒正常；给 60 秒是留给
# "刚开机、模型还在往显存里搬"的情形。
GPTSOVITS_TIMEOUT = 60

# 响度归一的目标（EBU R128 综合响度，单位 LUFS）。
#
# 为什么需要：GPT-SoVITS 的**原始输出很轻** —— 实测峰值只有 −15 ~ −17 dBFS、
# 综合响度约 −30 LUFS，而 Edge 那边是 −7 ~ −9 dBFS / 约 −22 LUFS。
# 不处理的话，用户从 Edge 换过来会觉得"她怎么没声音了"，得把手机音量拧到头。
#
# 为什么用 loudnorm 而不是直接拉峰值：loudnorm 归一的是**感知响度**，
# 不会因为句子里偶尔一个爆点就把整条压得很小；而且它自带真峰值限制（TP），
# 后面还要过一次 mp3 / silk 编码，留这点余量正好。
#
# −16 LUFS 是语音内容（播客 / 有声书）的常用目标，比 Edge 略响一点点。
# 想和 Edge 完全一样响就填 −21（实测 Edge ≈ −21.8）；填 0 ＝ 不处理。
GPTSOVITS_LOUDNESS_LUFS = -16.0

# 真峰值上限。留 1.5 dB 余量给后续编码，避免削波。
GPTSOVITS_TRUE_PEAK = -1.5

# 默认参考音频（项目根的 参考音色/ 目录，由 catbot/_make_ref_audio.py 生成）
DEFAULT_REF_DIR = BASE_DIR.parent / "参考音色"
DEFAULT_REF_AUDIO = DEFAULT_REF_DIR / "小柚_默认参考.wav"
DEFAULT_REF_TEXT_FILE = DEFAULT_REF_DIR / "小柚_默认参考.txt"


def _safe_float(value, default: float) -> float:
    """配置里读来的小数一律软着陆 —— 写错一个参数不该让她哑掉。"""
    try:
        if isinstance(value, bool):
            return default
        return float(str(value).strip())
    except Exception:
        return default


def gptsovits_url(profile: dict | None = None) -> str:
    """本机 TTS 服务的地址。"""
    return str((profile or {}).get("gptsovits_url")
               or GPTSOVITS_DEFAULT_URL).strip()


def gptsovits_alive(profile: dict | None = None) -> bool:
    """本机 TTS 服务在不在。**只探端口，不做合成** —— 探活必须快。

    服务是"先加载模型、再监听端口"的顺序（api_v2.py 在 import 时就把
    TTS 流水线建好了），所以端口能连上就代表它已经能干活。

    判「不在」的结果会缓存十秒：这台机器上摸一个没人监听的端口要等满超时，
    不复用的话服务没起来时每条语音都得白等一次。
    """
    import socket
    import urllib.parse

    url = gptsovits_url(profile)
    now = time.time()
    with _probe_lock:
        hit = _probe_cache.get(url)
        if hit is not None and now - hit < GPTSOVITS_PROBE_TTL:
            return False

    try:
        u = urllib.parse.urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 9880
        with socket.create_connection((host, port), timeout=GPTSOVITS_PROBE_SEC):
            ok = True
    except Exception:
        ok = False

    with _probe_lock:
        if ok:
            _probe_cache.pop(url, None)          # 活的就实时探，不吃缓存
        else:
            _probe_cache[url] = now
            # 顺手清掉早就过期的键，别让它一直长
            for k in [k for k, t in _probe_cache.items()
                      if now - t > GPTSOVITS_PROBE_TTL * 6]:
                _probe_cache.pop(k, None)
    return ok


def ref_text_for(ref_audio) -> str:
    """参考音频对应的文本：同名 .txt 有就用它，没有退回默认那份。

    prompt_text 与音频**必须逐字对应**，否则模型念出来会飘。所以宁可
    退回一份确定对得上的默认文本，也不拿一段不相干的字去糊。
    """
    try:
        side = Path(ref_audio).with_suffix(".txt")
        if side.exists():
            t = side.read_text(encoding="utf-8").strip()
            if t:
                return t
    except Exception:
        pass
    try:
        if DEFAULT_REF_TEXT_FILE.exists():
            return DEFAULT_REF_TEXT_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _gptsovits_synth(text: str, out_wav: Path, *, url: str, ref_audio: str = "",
                     ref_text: str = "", ref_lang: str = "zh",
                     speed: float = 1.0, temperature: float = 1.0,
                     split_method: str = "cut0",
                     timeout: int = GPTSOVITS_TIMEOUT) -> bool:
    """文字 → wav（本机 GPT-SoVITS）。失败返回 False，绝不抛异常。

    text_split_method 默认 **cut0（不切句）**：她的回复上限才 55 字，
    整句交给模型，语气最连贯 —— 这正是它比"Edge 客户端分段"强的地方。
    """
    text = (text or "").strip()
    if not text:
        return False

    ref = Path(ref_audio) if ref_audio else DEFAULT_REF_AUDIO
    if not ref.exists():
        logger.warning("GPT-SoVITS 的参考音频不存在：%s", ref)
        return False

    try:
        import requests
    except Exception as e:
        logger.info("没装 requests，用不了 GPT-SoVITS：%s", e)
        return False

    payload = {
        "text": text,
        "text_lang": ref_lang,
        "ref_audio_path": str(ref),
        "prompt_text": ref_text or ref_text_for(ref),
        "prompt_lang": ref_lang,
        "text_split_method": split_method,
        "media_type": "wav",
        "speed_factor": max(0.5, min(2.0, speed)),
        "temperature": max(0.1, min(1.5, temperature)),
        "streaming_mode": False,
    }
    try:
        r = requests.post(url.rstrip("/") + "/tts", json=payload, timeout=timeout)
    except Exception as e:
        logger.warning("GPT-SoVITS 请求失败（%s）：%s", url, str(e)[:160])
        return False

    if r.status_code != 200:
        logger.warning("GPT-SoVITS 返回 %s：%s", r.status_code, (r.text or "")[:200])
        return False

    # 出错时它回的是 JSON 而非音频。状态码之外再兜一层"内容是不是真 wav"，
    # 免得把一段报错文字当成语音发出去。
    body = r.content or b""
    if len(body) < 1024 or body[:4] != b"RIFF":
        logger.warning("GPT-SoVITS 返回的不是有效 wav（%d 字节）", len(body))
        return False

    try:
        out_wav.write_bytes(body)
    except Exception as e:
        logger.warning("写 GPT-SoVITS 结果失败：%s", e)
        return False
    return out_wav.exists() and out_wav.stat().st_size > 1024


# --------------------------------------------------------------------------
# 说 · 方式二：SAPI（离线兜底）
# --------------------------------------------------------------------------

def text_to_wav(text: str, out_wav: Path, rate: int = 1) -> bool:
    """用 Windows 自带的 SAPI 合成语音。

    直接把输出格式钉成 16kHz/16bit/单声道，与后续编码格式一致，省一次重采样。
    文本经 UTF-8 文件交给 PowerShell —— 中文走命令行参数会变乱码，这是
    最容易踩的坑（写进文件再用 .NET 的 UTF8 读，绕开参数编码）。
    """
    text = (text or "").strip()
    if not text:
        return False

    TMP_DIR.mkdir(exist_ok=True)
    txt_file = out_wav.with_suffix(".txt")
    try:
        txt_file.write_text(text, encoding="utf-8")
    except Exception as e:
        logger.warning("写 TTS 文本失败: %s", e)
        return False

    names = ",".join("'%s'" % n for n in CN_VOICES)
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"foreach($n in @({names})){{ try{{ $s.SelectVoice($n); break }}catch{{}} }};"
        f"$s.Rate = {int(rate)};"
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        " [System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{out_wav}', $fmt);"
        f"$t = [System.IO.File]::ReadAllText('{txt_file}', "
        "[System.Text.Encoding]::UTF8);"
        "$s.Speak($t);$s.Dispose();"
    )

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW,
        )
        if r.returncode != 0:
            logger.warning("SAPI 合成失败: %s", (r.stderr or "")[:200])
            return False
        ok = out_wav.exists() and out_wav.stat().st_size > 1024
        if not ok:
            logger.warning("SAPI 没产出有效 wav（可能没装中文语音包）")
        return ok
    except Exception as e:
        logger.warning("调 SAPI 出错: %s", e)
        return False
    finally:
        try:
            txt_file.unlink(missing_ok=True)
        except Exception:
            pass


def _transcode(src: Path, out: Path, fmt: str) -> bool:
    """换容器/编码。src 是 wav 或 mp3，out 按 fmt 命名。"""
    args = ["-i", str(src)]
    if fmt == "mp3":
        args += ["-acodec", "libmp3lame", "-b:a", "48k", "-ar", "24000", "-ac", "1"]
    elif fmt == "amr":
        args += ["-ar", "8000", "-ac", "1", "-acodec", "libopencore_amrnb",
                 "-b:a", "12.2k"]
    args += [str(out)]
    try:
        return _run_ffmpeg(args) and out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def _normalize_loudness(src: Path, out: Path,
                        lufs: float = GPTSOVITS_LOUDNESS_LUFS) -> bool:
    """把音量拉到"正常说话"的响度（EBU R128）。失败返回 False，由调用方兜底。

    为什么不能只是"拉峰值"：峰值归一遇到句子里一个爆点，整条就被压小了；
    反过来如果没爆点又会放得过大。**感知响度**才是耳朵听到的音量。

    输出直接定成 24kHz/单声道 —— 这正是后面 mp3 交付要的格式，
    免得先归一到 32k 再被 `_transcode` 重采样一次（多一道损失）。

    loudnorm 是单遍模式：对 1~10 秒的短语音足够准，也不像两遍模式那样
    要先跑一次再跑一次（那得合成两遍，不值当）。
    """
    if not src.exists() or src.stat().st_size <= 1024:
        return False
    try:
        lufs = float(lufs)
    except Exception:
        lufs = GPTSOVITS_LOUDNESS_LUFS
    if lufs >= 0:                      # 0 或正数＝用户要求关掉归一
        return False
    return _run_ffmpeg([
        "-i", str(src),
        "-af", "loudnorm=I=%.1f:TP=%.1f:LRA=11" % (lufs, GPTSOVITS_TRUE_PEAK),
        "-ar", "24000", "-ac", "1", "-acodec", "pcm_s16le",
        str(out),
    ]) and out.exists() and out.stat().st_size > 1024


def _safe_int(value, default: int) -> int:
    """把配置里读来的值转成 int；转不了就用默认值。

    配置文件是手写的，填成 "快一点" 或者漏个引号都很正常。语音是锦上添花，
    绝不能因为一个参数写错就把整条消息拖哑 —— 所以这里一律软着陆。
    """
    try:
        if isinstance(value, bool):
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def _pick_provider(profile: dict) -> str:
    p = str((profile or {}).get("tts_provider") or "auto").strip().lower()
    return p if p in ("auto", "gptsovits", "edge", "sapi") else "auto"


def speak(text: str, rate: int = 1, fmt: str = "mp3",
          profile: dict | None = None) -> Path | None:
    """文字 → 可直接发给 QQ 的语音文件路径。失败返回 None。

    profile 就是 config.json 里的 `voice` 段，按它的 `tts_provider` 选引擎：

      auto （默认）本机 GPT-SoVITS → Edge TTS → 系统语音，逐级兜底
      gptsovits     优先用本机 GPT-SoVITS（离线、克隆音色）；没起来就往下退
      edge          优先用 Edge TTS（联网、音色好）
      sapi          只用 Windows 自带语音（完全离线）

    交付格式默认 mp3：体积小、协议层必定认。协议层在发送时会自己把它
    转成 QQ 要的 silk —— 那是它必须会的事，我们不重复实现一遍。
    """
    text = (text or "").strip()
    if not text:
        return None
    profile = profile or {}
    provider = _pick_provider(profile)
    TMP_DIR.mkdir(exist_ok=True)
    stamp = f"{int(time.time()*1000)}_{threading.get_ident()}"

    # --- 路线一：GPT-SoVITS（本机、离线、音色最好）---
    if provider in ("auto", "gptsovits"):
        if gptsovits_alive(profile):
            wav = TMP_DIR / f"say_{stamp}.gsv.wav"
            if _gptsovits_synth(
                text, wav,
                url=gptsovits_url(profile),
                ref_audio=str(profile.get("gptsovits_ref_audio") or ""),
                ref_text=str(profile.get("gptsovits_ref_text") or ""),
                ref_lang=str(profile.get("gptsovits_ref_lang") or "zh"),
                speed=_safe_float(profile.get("gptsovits_speed"), 1.0),
                temperature=_safe_float(profile.get("gptsovits_temperature"), 1.0),
                split_method=str(profile.get("gptsovits_split") or "cut0"),
            ):
                # 归一响度：它的原始输出比 Edge 轻 8~10 dB（实测 −30 vs −22 LUFS），
                # 不处理听着就像"她没在说话"。归一失败就把原样交出去，别因此哑掉。
                norm = TMP_DIR / f"say_{stamp}.gsv.norm.wav"
                if _normalize_loudness(
                    wav, norm,
                    _safe_float(profile.get("gptsovits_loudness"),
                                GPTSOVITS_LOUDNESS_LUFS),
                ):
                    try:
                        wav.unlink(missing_ok=True)
                    except Exception:
                        pass
                    wav = norm
                else:
                    logger.info("响度归一没做成，按原始音量交付（会偏轻）")
                if fmt == "wav":
                    return wav
                out = TMP_DIR / f"say_{stamp}.{fmt}"
                if _transcode(wav, out, fmt):
                    try:
                        wav.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return out
                return wav          # 转码失败也把 wav 交出去，总比没有强
            logger.info("GPT-SoVITS 合成没成功，往下退一级")
        elif provider == "gptsovits":
            logger.info("本机 GPT-SoVITS 没起来（%s），先退到 Edge",
                        gptsovits_url(profile))

    # --- 路线二：Edge TTS ---
    if provider in ("auto", "gptsovits", "edge") and edge_available():
        mp3 = TMP_DIR / f"say_{stamp}.mp3"
        if _edge_synth(
            text, mp3,
            voice=str(profile.get("edge_voice") or DEFAULT_EDGE_VOICE),
            rate=_safe_int(profile.get("edge_rate"), 8),
            pitch=_safe_int(profile.get("edge_pitch"), 10),
            volume=_safe_int(profile.get("edge_volume"), 0),
        ):
            if fmt == "mp3":
                return mp3
            out = TMP_DIR / f"say_{stamp}.{fmt}"
            if _transcode(mp3, out, fmt):
                try:
                    mp3.unlink(missing_ok=True)
                except Exception:
                    pass
                return out
            return mp3          # 转码失败也把 mp3 交出去，总比没有强
        logger.info("Edge TTS 没成功，改用系统语音顶一下")

    # --- 路线三：SAPI（离线兜底）---
    wav = TMP_DIR / f"say_{stamp}.wav"
    if not text_to_wav(text, wav, _safe_int(rate, 1)):
        return None

    if fmt == "wav":
        return wav

    out = TMP_DIR / f"say_{stamp}.{fmt}"
    if _transcode(wav, out, fmt):
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass
        return out

    # 转码失败就退回去交 wav —— 让协议层自己想办法，总比什么都不发好
    logger.warning("转 %s 失败，退回 wav 交付", fmt)
    return wav


def voices_installed() -> list[str]:
    """状态面板用：这台机器装了哪些语音。"""
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          "($s.GetInstalledVoices()|ForEach-Object{$_.VoiceInfo.Name}) -join ','")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           creationflags=NO_WINDOW)
        return [v for v in (r.stdout or "").strip().split(",") if v]
    except Exception:
        return []


def whisper_supported() -> bool:
    """现在能不能让她开口说中文。任一条路可用即可。

    Edge TTS 装了就算能用（它是联网的，真发的时候可能失败，那时会自动
    退回 SAPI —— 所以这里不因为"可能断网"就返回 False）。
    """
    if gptsovits_alive({}) or edge_available():
        return True
    return any(any(cn in v for cn in _SAPI_HINTS) for v in voices_installed())


def tts_engine(profile: dict | None = None) -> str:
    """当前实际会用的合成引擎名，给状态面板显示用。"""
    p = _pick_provider(profile or {})
    if p in ("auto", "gptsovits") and gptsovits_alive(profile or {}):
        return "gptsovits"
    if p in ("auto", "gptsovits", "edge") and edge_available():
        return "edge"
    if any(any(cn in v for cn in _SAPI_HINTS) for v in voices_installed()):
        return "sapi"
    return "none"


def describe_engine(profile: dict | None = None) -> str:
    """一句人话，直接往状态面板上贴。"""
    profile = profile or {}
    eng = tts_engine(profile)
    if eng == "gptsovits":
        return "GPT-SoVITS（本机克隆音色）"
    if eng == "edge":
        short = str(profile.get("edge_voice") or DEFAULT_EDGE_VOICE)
        label = EDGE_VOICES.get(short, (short, ""))[0]
        return f"Edge TTS（{label}）"
    if eng == "sapi":
        return "系统语音（离线）"
    return "不可用"


def sweep_tmp(keep_seconds: int = 600) -> None:
    """清掉临时语音文件，避免 tmp 目录越堆越多。"""
    try:
        now = time.time()
        for p in TMP_DIR.glob("*"):
            if p.is_file() and now - p.stat().st_mtime > keep_seconds:
                p.unlink(missing_ok=True)
    except Exception:
        pass
