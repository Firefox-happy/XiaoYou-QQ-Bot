# -*- coding: utf-8 -*-
"""端到端实测：喂真实事件给小柚，看她的实际反应。

覆盖：闲聊 / 看图 / 听语音 / 语音回复 / 早安生成。
OneBot 换成假的（只记录调用了什么），避免依赖被风控的 QQ 账号。
"""
import io
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import bot as botmod
import voice

OUT = []
def log(*a):
    OUT.append(" ".join(str(x) for x in a))


class FakeAPI:
    """假装是 NapCat：把要发的消息记下来。"""
    def __init__(self):
        self.sent = []
        self.self_id = "1234567890"

    def _rec(self, kind, target, msg):
        self.sent.append({"kind": kind, "to": target, "msg": msg})
        for seg in msg:
            t = seg.get("type")
            d = seg.get("data") or {}
            if t == "text":
                log(f"      → [文本] {d.get('text','')[:100]}")
            elif t == "record":
                log(f"      → [语音] {d.get('file','')[:80]}")
            elif t == "at":
                log(f"      → [@] {d.get('qq')}")
        return {"status": "ok", "retcode": 0}

    def send_private(self, uid, msg):
        return self._rec("private", uid, msg)

    def send_group(self, gid, msg):
        return self._rec("group", gid, msg)

    def call(self, action, **params):
        log(f"      → [API] {action} {params}")
        return {"status": "ok", "retcode": 0, "data": {}}

    def get_login_info(self):
        return {"data": {"user_id": "1234567890", "nickname": "小柚"}}


def mk_ev(msg, mt="private", uid="10001", gid=None, mid=None):
    ev = {
        "post_type": "message", "message_type": mt,
        "user_id": uid, "self_id": "1234567890",
        "message_id": mid or int(time.time() * 1000 % 1000000),
        "sender": {"nickname": "主人", "card": ""},
        "message": msg,
    }
    if gid:
        ev["group_id"] = gid
    return ev


def mk_image(text):
    """直接引用预生成的图片。

    catbot venv 里没有 PIL（只有 requests/websocket/vosk），所以造图这一步
    交给 Anaconda 环境做（make_pic.py），这里只负责用。
    """
    out = BASE / "tmp" / "e2e_pic.png"
    if not out.exists():
        return None
    return out


log("=== 准备 ===")
b = botmod.CatBot()
b.api = FakeAPI()

# 隔离记忆：e2e 直接调 _handle，测试消息会像真消息一样落盘。
# 不隔离的话，跑完测试小柚就"记得"一堆从没发生过的对话。
b.memory.dir = BASE / "tmp" / "_mem_test"
b.memory.dir.mkdir(parents=True, exist_ok=True)
b.memory._cache.clear()

log(f"  模型      : {b.llm.model} @ {b.llm.api_base}")
log(f"  语音就绪  : 听={voice.ready()} 说={voice.whisper_supported()}")

# 造"主人发来的语音"（真实 QQ 是 silk，这里用等价的 wav 走同一条识别链）
SAID = "小柚你好呀，今天过得怎么样"
wav = BASE / "tmp" / "e2e_in.wav"
voice.text_to_wav(SAID, wav)
log(f"  测试语音  : {SAID!r} → {wav.name}")

img = mk_image("")
log(f"  测试图片  : 报销单 合计1280元 → {img.name if img else '缺失，跳过看图用例'}")


def run(title, ev, note=""):
    log("")
    log(f"=== {title} ===")
    if note:
        log(f"  {note}")
    b.limiter._last.clear()          # 绕开 3 秒冷却，让用例连着跑
    b.limiter._global.clear()
    t0 = time.time()
    b._handle(ev)
    log(f"  （耗时 {time.time()-t0:.1f}s）")
    return b.api.sent[:]


# ---- 1. 纯文本闲聊 ----
b.api.sent = []
run("1. 纯文本闲聊", mk_ev([{"type": "text", "data": {"text": "在吗"}}]),
    "期望：短句、不调工具、不打回'不懂'")

# ---- 2. 看图 ----
if img:
    b.api.sent = []
    run("2. 看图", mk_ev([{"type": "text", "data": {"text": "看看这个"}},
                        {"type": "image", "data": {"file": str(img)}}]),
        "期望：读出图里的金额 1280")
else:
    log("")
    log("=== 2. 看图 ===")
    log("  跳过：测试图片不存在")

# ---- 3. 听语音 ----
b.api.sent = []
run("3. 听语音", mk_ev([{"type": "record", "data": {"file": str(wav)}}]),
    f"期望：先识别出 {SAID!r} 再正常回话")

# ---- 4. 语音回复（晚安触发）----
b.api.sent = []
run("4. 语音回复", mk_ev([{"type": "text", "data": {"text": "晚安，我去睡了"}}]),
    "期望：短句 → 合成语音条发出")
sent = b.api.sent
has_record = any(s.get("type") == "record"
                 for item in sent for s in item["msg"])
log(f"  实际发出语音条: {has_record}")

# ---- 5. 群聊里不该发语音 ----
b.api.sent = []
run("5. 群聊晚安（应仍打字）",
    mk_ev([{"type": "at", "data": {"qq": "1234567890"}},
           {"type": "text", "data": {"text": " 晚安"}}], mt="group", gid="610086723"),
    "期望：群里默认不发语音（group_enabled=false）")
g = b.api.sent
rec_in_group = any(s.get("type") == "record" for item in g for s in item["msg"])
log(f"  群里发语音了没: {rec_in_group}（应为 False）")

# ---- 6. 早安生成 ----
log("")
log("=== 6. 主动早安（只生成文案，不真发）===")
try:
    t0 = time.time()
    greet = b._compose_greeting("10001")
    log(f"  （耗时 {time.time()-t0:.1f}s）")
    log(f"  文案: {greet!r}")
except Exception as e:
    log(f"  FAIL: {type(e).__name__}: {e}")

# ---- 7. 坏语音不该崩 ----
b.api.sent = []
bad = BASE / "tmp" / "e2e_bad.silk"
bad.write_bytes(b"#!SILK_V3" + b"\x00" * 40)
run("7. 听不清的语音", mk_ev([{"type": "record", "data": {"file": str(bad)}}]),
    "期望：不崩，老实说没听清")

(BASE / "_test" / "e2e_voice_out.txt").write_text("\n".join(OUT), encoding="utf-8")
print("done")
