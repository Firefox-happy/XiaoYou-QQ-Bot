# -*- coding: utf-8 -*-
"""
bot.py 逻辑单元测试 —— 不依赖 QQ、不依赖模型，纯函数级验证。

运行:
    python _test/test_unit.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402

PASS = FAIL = 0


def check(name, got, expect):
    global PASS, FAIL
    ok = got == expect
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望: {expect!r}\n         实际: {got!r}")


def check_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("\n=== 1. 消息段解析 ===")
seg = lambda t, **d: {"type": t, "data": d}
group_msg = [seg("at", qq="10001"), seg("text", text=" 在吗")]
check("extract_text 组合", bot.extract_text(group_msg), "@10001 在吗")
check("extract_text 纯文本", bot.extract_text("你好呀"), "你好呀")
check("extract_text 图片", bot.extract_text([seg("text", text="看这个"), seg("image", file="a.jpg")]), "看这个[图片]")
check("extract_text 混合", bot.extract_text([seg("face", id="1"), seg("text", text="哈哈")]), "[表情:撇嘴]哈哈")
check("内置表情名称", bot.extract_text([seg("face", id="14")]), "[表情:微笑]")
check("未知表情 ID 回退", bot.extract_text([seg("face", id="99999")]), "[表情(id=99999)]")
check("商城表情摘要", bot.extract_text([seg("mface", summary=" /菜汪 ")]), "[表情包:菜汪]")
check("商城空摘要回退", bot.extract_text([seg("mface", summary=" / ")]), "[表情包]")
check("商城缺失摘要回退", bot.extract_text([seg("mface")]), "[表情包]")
check("商城异常摘要回退", bot.extract_text([seg("mface", summary=123)]), "[表情包]")
check("文字加表情", bot.extract_text([seg("text", text="好耶"), seg("face", id="14")]), "好耶[表情:微笑]")
check("表情加图片保留占位", bot.extract_text([seg("face", id="179"), seg("image", file="a.jpg")]), "[表情:doge][图片]")
check("整数表情 ID", bot.extract_text([seg("face", id=270)]), "[表情:emm]")
check("异常消息段忽略", bot.extract_text([{"type": "mface", "data": 123}]), "")
check("触发用文本排除表情", bot.extract_text([seg("text", text="你好"), seg("face", id="14"), seg("mface", summary="/小柚")], include_faces=False), "你好")
for bad_map in ("not json", "[]"):
    with patch.object(bot, "_FACE_MAP", None), patch.object(Path, "read_text", return_value=bad_map):
        check("损坏映射回退 " + bad_map, bot.extract_text([seg("face", id="14")]), "[表情(id=14)]")
with patch.object(bot, "_FACE_MAP", None), patch.object(Path, "read_text", side_effect=FileNotFoundError):
    check("缺失映射回退", bot.extract_text([seg("face", id="14")]), "[表情(id=14)]")

print("\n=== 2. @ 检测与剥离 ===")
check_true("is_at_me 命中", bot.is_at_me(group_msg, "10001"))
check_true("is_at_me 不命中", not bot.is_at_me(group_msg, "99999"))
check_true("is_at_me 字符串消息", not bot.is_at_me("hello", "10001"))
check("strip_at_me 去掉@", bot.strip_at_me(group_msg, "10001").strip(), "在吗")
check("strip_at_me 保留他人@", bot.strip_at_me([seg("at", qq="888"), seg("text", text=" hi")], "10001").strip(), "@888 hi")
check("strip_at_me 别人的@不会被误删", bot.strip_at_me([seg("at", qq="777"), seg("at", qq="10001")], "10001").strip(), "@777")

print("\n=== 3. 回复清洗（去 markdown）===")
check("去粗体", bot.clean_reply("**喵呜**主人好", 200), "喵呜主人好")
check("去斜体", bot.clean_reply("*歪头*看你", 200), "歪头看你")
check("去标题", bot.clean_reply("## 小柚的回复\n在的喵", 200), "小柚的回复\n在的喵")
check("去列表", bot.clean_reply("- 第一件事\n- 第二件事", 200), "第一件事\n第二件事")
check("去行内代码", bot.clean_reply("`code` 是这样", 200), "code 是这样")
check("去自报名前缀", bot.clean_reply("小柚：在的喵~", 200), "在的喵~")
check("去包裹引号", bot.clean_reply("\"在的喵\"", 200), "在的喵")
check("去链接保留文字", bot.clean_reply("看[这里](http://a.com)喵", 200), "看这里喵")
check("压缩多余空行", bot.clean_reply("喵\n\n\n喵", 200), "喵\n喵")

print("\n=== 4. 长度控制 ===")
long_text = "第一句话在这里呀。第二句话也在这里。第三句话还是在这里。第四句继续。"
r = bot.clean_reply(long_text, 20)
check_true("超长被截断", len(r) <= 20, f"len={len(r)}")
check_true("截断点在标点处", r.endswith(("。", "！", "？", "…")), repr(r))
check("短文本不变", bot.clean_reply("在的喵", 200), "在的喵")
check("空文本", bot.clean_reply("", 200), "")

print("\n=== 5. 长消息分段 ===")
check("短消息不分段", len(bot.split_message("在的喵", 120)), 1)
multi = bot.split_message("第一句话够长了。第二句话也够长了。第三句话继续接上。", 20)
check_true("长消息被分段", len(multi) > 1, f"得到 {len(multi)} 段")
check_true("分段内容不丢失", "".join(multi).replace(" ", "") == "第一句话够长了。第二句话也够长了。第三句话继续接上。".replace(" ", ""),
           f"{multi}")

print("\n=== 6. 触发判断 ===")
cfg = bot.Config(ROOT / "config.json")
bot_cfg = cfg

class FakeBot:
    """只带 `_should_reply` 需要的那两段配置，而且是**写死**的。

    2026-09-25 踩过：这里原来直接用了 `config.json` 的实名配置，
    而 `trigger.group_at` 是**用户在设置页随时能改的偏好**。用户把它关掉之后，
    「群@触发」这条测试就红了 —— 看起来像代码坏了，其实只是设置变了。
    **测试要测的是逻辑，不是用户的偏好**，所以这里自己钉一份固定配置。
    （"config.json 里确实有这段配置"由下面第 9 节单独守着。）
    """
    cfg = {
        "trigger": {"group_at": True, "group_keywords": ["小柚", "猫娘"],
                    "private": True, "ignore_self": True},
        "whitelist": {"groups": [], "users": []},
    }

    def _should_reply(self, ev):
        return bot.CatBot._should_reply(self, ev)


fb = FakeBot()
BASE = {"self_id": 10001, "message_type": "group", "group_id": 900001, "user_id": 20001}

ev_at = dict(BASE, message=[seg("at", qq="10001"), seg("text", text=" 你好")], message_id=1)
check("群@触发", fb._should_reply(ev_at), (True, "at"))

ev_kw = dict(BASE, message=[seg("text", text="小柚在吗")], message_id=2)
check("群关键词触发", fb._should_reply(ev_kw), (True, "keyword"))

ev_plain = dict(BASE, message=[seg("text", text="今晚吃什么")], message_id=3)
check("群普通消息不触发", fb._should_reply(ev_plain), (False, "no_trigger"))

ev_self = dict(BASE, user_id=10001, message=[seg("text", text="小柚在吗")], message_id=4)
check("自己发的消息忽略", fb._should_reply(ev_self), (False, "self"))

ev_pv = {"self_id": 10001, "message_type": "private", "user_id": 20001,
         "message": [seg("text", text="在吗")], "message_id": 5}
check("私聊触发", fb._should_reply(ev_pv), (True, "private"))

ev_other = {"self_id": 10001, "message_type": "notice", "message_id": 6}
check("非消息事件不触发", fb._should_reply(ev_other), (False, "unknown_type"))

emoji_msg = [seg("mface", summary="/小柚"), seg("face", id="14")]
check("群表情摘要不误触发", fb._should_reply(dict(BASE, message=emoji_msg)), (False, "no_trigger"))
check("私聊纯表情仍触发", fb._should_reply(dict(ev_pv, message=emoji_msg)), (True, "private"))
check("群@加表情仍触发", fb._should_reply(dict(BASE, message=[seg("at", qq="10001")] + emoji_msg)), (True, "at"))
check("群文字关键词加表情仍触发", fb._should_reply(dict(BASE, message=[seg("text", text="小柚在吗")] + emoji_msg)), (True, "keyword"))

print("\n=== 7. 限流 ===")
class FakeLim:
    pass
lim = bot.RateLimiter(bot_cfg)
ok1, _ = lim.allow("g1")
ok2, why2 = lim.allow("g1")
ok3, _ = lim.allow("g2")
check_true("首次放行", ok1)
check_true("冷却期内拦截", not ok2 and why2 == "session_cooldown", why2)
check_true("不同会话独立", ok3)

print("\n=== 8. 人设加载 ===")
p = bot.Persona(ROOT / cfg["persona_file"])
check_true("人设非空", len(p.text) > 100, f"len={len(p.text)}")
check_true("人设含关键约束", "短" in p.text and "markdown" in p.text.lower())
p.reload(force=True)
check_true("重复载入稳定", len(p.text) > 100)

print("\n=== 9. 配置 ===")
check_true("配置有 llm.model", bool(cfg["llm"]["model"]), cfg["llm"]["model"])
# 这里只要求「这段配置存在、类型对」——**不钉具体值**。
# group_at / private 都是用户在设置页能改的偏好，钉死会让「用户改设置」
# 表现成「测试失败」。要守的是配置结构没丢、没被写坏。
_trig = cfg["trigger"]
check_true("配置有触发设置（键在、类型对）",
           isinstance(_trig.get("group_at"), bool)
           and isinstance(_trig.get("group_keywords"), list)
           and isinstance(_trig.get("private"), bool),
           dict(_trig) if hasattr(_trig, "get") else _trig)
check_true("max_turns 合理性", 0 < cfg["memory"]["max_turns"] <= 50, cfg["memory"]["max_turns"])

print("\n=== 10. NapCat HTTP 请求体格式 ===")
# 回归测试：HTTP 服务端要【裸参数】json 体，即 {"user_id":..,"message":..}。
# 若包成 {"action":..,"params":{..}}（那是反向 HTTP 客户端的格式），
# NapCat 取不到 message，会抛
#   TypeError: Cannot read properties of undefined (reading 'type')
# 结果就是消息一直发不出去。
captured = {}


class _FakeResp:
    status_code = 200

    def json(self):
        return {"status": "ok", "retcode": 0}


class _FakeSession:
    headers = {}

    def post(self, url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _FakeResp()


api = bot.OneBot(bot_cfg)
api._session = _FakeSession()
api.send_private(20001, [seg("text", text="你好")])

body = captured.get("body") or {}
check_true("请求体不含 action/params 包装",
           "action" not in body and "params" not in body, f"body={body}")
check("请求体直接带 user_id", body.get("user_id"), 20001)
check("请求体直接带 message", body.get("message"), [seg("text", text="你好")])
check_true("URL 指向动作路径", captured.get("url", "").endswith("/send_private_msg"),
           captured.get("url"))

print(f"\n{'=' * 50}")
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 50)
sys.exit(1 if FAIL else 0)
