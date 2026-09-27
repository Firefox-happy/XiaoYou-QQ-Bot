# -*- coding: utf-8 -*-
"""
事件探针 —— 验证 NapCat 推给机器人的事件格式是否正确。

用途：机器人不回复时，先用它确认「协议层到底有没有把事件推出来」。
它只监听、不改任何数据。

用法：
    python _test/probe_events.py            # 只听 35 秒，打印收到的原始事件
    python _test/probe_events.py --send     # 额外给自己发一条私聊，触发一次真实事件
"""
import json
import sys
import threading
import time
import urllib.request

WS_URL = "ws://127.0.0.1:3001"
HTTP_URL = "http://127.0.0.1:3000"

LISTEN_SECONDS = 35
SELF_UIN = None  # 自动从 /get_login_info 取

try:
    sys.stdout.reconfigure(encoding="gbk", errors="replace", line_buffering=True)
except Exception:
    pass

captured = []


def on_message(ws, raw):
    captured.append(raw)
    try:
        ev = json.loads(raw)
    except Exception:
        print("[RAW-非JSON] " + raw[:300])
        return
    kind = ev.get("post_type", "?")
    if kind == "meta_event":
        print("[事件] meta_event / %s  (心跳，正常)" % ev.get("meta_event_type"))
        return
    print("[事件] " + json.dumps(ev, ensure_ascii=False)[:900])
    print("       -> message_type=%s user_id=%s group_id=%s self_id=%s"
          % (ev.get("message_type"), ev.get("user_id"),
             ev.get("group_id"), ev.get("self_id")))
    print("       -> message=%s" % json.dumps(ev.get("message"), ensure_ascii=False)[:300])


def on_open(ws):
    print("[探针] 已连上 " + WS_URL)


def on_error(ws, err):
    print("[探针] 连接出错: %s" % err)


def api(path, payload=None):
    url = HTTP_URL + path
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    import websocket

    do_send = "--send" in sys.argv

    try:
        info = api("/get_login_info")
        uin = str((info.get("data") or {}).get("user_id", ""))
        print("[探针] 当前登录账号: %s (%s)"
              % (uin, (info.get("data") or {}).get("nickname")))
    except Exception as e:
        print("[探针] 读不到登录信息，协议层可能没起来: %s" % e)
        return 1

    ws = websocket.WebSocketApp(
        WS_URL, on_message=on_message, on_open=on_open, on_error=on_error)
    threading.Thread(target=ws.run_forever, daemon=True).start()
    time.sleep(2)

    if do_send:
        print("[探针] 给自己发一条私聊，触发真实消息事件...")
        try:
            r = api("/send_private_msg",
                    {"user_id": int(uin), "message": "格式探针（可忽略）"})
            print("[探针] 发送结果: " + json.dumps(r, ensure_ascii=False))
        except Exception as e:
            print("[探针] 发送失败（有些 QQ 号不支持发给自己）: %s" % e)

    print("[探针] 监听 %d 秒..." % LISTEN_SECONDS)
    time.sleep(LISTEN_SECONDS)

    msg_events = [c for c in captured
                  if '"post_type":"message"' in c.replace(" ", "")]
    print()
    print("=" * 56)
    print("  收到事件总数: %d" % len(captured))
    print("  其中消息事件: %d" % len(msg_events))
    if captured:
        print("  结论: 协议层推送正常（机器人连不上就不是协议层的问题）")
    else:
        print("  结论: 一个事件都没收到 —— 协议层没登录成功，或端口不对")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
