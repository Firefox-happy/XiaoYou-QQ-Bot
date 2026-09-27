# -*- coding: utf-8 -*-
"""
模拟一个 NapCat / OneBot 11 服务端，用来在没有 QQ 的情况下测试 bot.py。

- HTTP 3000: 接收 bot 的 API 调用，把要发的消息打印出来
- WS   3001: 主动推事件给 bot

用法:
    python _test/mock_napcat.py            # 只起服务
    python _test/mock_napcat.py --demo     # 起服务并自动推几条测试消息
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

try:
    from websocket_server import WebsocketServer  # type: ignore
except ImportError:
    WebsocketServer = None

SENT = []
BOT_UIN = "10001"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            req = {}
        action = req.get("action", "")
        params = req.get("params", {})
        echo = req.get("echo")

        if action == "get_login_info":
            self._reply({"status": "ok", "retcode": 0, "data": {"user_id": int(BOT_UIN), "nickname": "小柚"}, "echo": echo})
            return

        if action in ("send_group_msg", "send_private_msg"):
            text = "".join(
                s.get("data", {}).get("text", "") or f"[{s.get('type')}]"
                for s in params.get("message", [])
            )
            SENT.append(text)
            print(f"\n  >>> 机器人发出 [{action}] {text}\n", flush=True)
            self._reply({"status": "ok", "retcode": 0, "data": {"message_id": len(SENT)}, "echo": echo})
            return

        self._reply({"status": "ok", "retcode": 0, "data": {}, "echo": echo})


def build_events():
    """构造几条测试事件：群@、群关键词、私聊、自己发的、无关消息。"""
    def msg_seg(t, **d):
        return {"type": t, "data": d}

    return [
        {  # 1. 群里 @ 机器人
            "post_type": "message", "message_type": "group",
            "self_id": int(BOT_UIN), "user_id": 20001, "group_id": 900001,
            "message_id": 1001,
            "sender": {"nickname": "阿浩", "card": "阿浩"},
            "message": [msg_seg("at", qq=BOT_UIN), msg_seg("text", text=" 在吗，今天好累")]
        },
        {  # 2. 群关键词（没 @）
            "post_type": "message", "message_type": "group",
            "self_id": int(BOT_UIN), "user_id": 20002, "group_id": 900001,
            "message_id": 1002,
            "sender": {"nickname": "路人甲", "card": "路人甲"},
            "message": [msg_seg("text", text="小柚你在干嘛呢")]
        },
        {  # 3. 私聊
            "post_type": "message", "message_type": "private",
            "self_id": int(BOT_UIN), "user_id": 20001,
            "message_id": 1003,
            "sender": {"nickname": "阿浩"},
            "message": [msg_seg("text", text="摸摸头")]
        },
        {  # 4. 自己在群里说话（应被忽略）
            "post_type": "message", "message_type": "group",
            "self_id": int(BOT_UIN), "user_id": int(BOT_UIN), "group_id": 900001,
            "message_id": 1004,
            "sender": {"nickname": "小柚"},
            "message": [msg_seg("text", text="刚才在数窗外的鸟")]
        },
        {  # 5. 群里无关消息（应被忽略）
            "post_type": "message", "message_type": "group",
            "self_id": int(BOT_UIN), "user_id": 20003, "group_id": 900001,
            "message_id": 1005,
            "sender": {"nickname": "路人乙"},
            "message": [msg_seg("text", text="今晚吃什么")]
        },
        {  # 6. 再次私聊（测上下文）
            "post_type": "message", "message_type": "private",
            "self_id": int(BOT_UIN), "user_id": 20001,
            "message_id": 1006,
            "sender": {"nickname": "阿浩"},
            "message": [msg_seg("text", text="我叫什么名字")]
        },
    ]


def main():
    demo = "--demo" in sys.argv

    httpd = HTTPServer(("127.0.0.1", 3000), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("[mock] HTTP API 已启动 http://127.0.0.1:3000")

    if WebsocketServer is None:
        print("[mock] 缺少 websocket-server 库，仅启动 HTTP")
        print("       安装: pip install websocket-server")
        while True:
            time.sleep(1)

    server = WebsocketServer(host="127.0.0.1", port=3001)
    clients = []

    def on_connect(client, _srv):
        clients.append(client)
        print(f"[mock] bot 已连接 (id={client['id']})，共 {len(clients)} 个")

    def on_disconnect(client, _srv):
        if client in clients:
            clients.remove(client)
        print(f"[mock] bot 断开 (id={client['id']})")

    server.set_fn_new_client(on_connect)
    server.set_fn_client_left(on_disconnect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("[mock] WS 事件通道已启动 ws://127.0.0.1:3001")

    if demo:
        def push():
            # 等 bot 先连上来，否则早期事件会丢
            waited = 0
            while not clients and waited < 90:
                time.sleep(1)
                waited += 1
            if not clients:
                print("[mock] 等不到 bot 连接，放弃推送")
                return
            print(f"\n[mock] bot 已就位（等了 {waited}s），2 秒后开始推事件")
            time.sleep(2)
            events = build_events()
            for i, ev in enumerate(events, 1):
                text = "".join(
                    s.get("data", {}).get("text", "") or "[at]"
                    for s in ev["message"]
                )
                print(f"\n[mock] --- 推送事件 {i}/{len(events)}: {ev['message_type']} / {text}")
                server.send_message_to_all(json.dumps(ev, ensure_ascii=False))
                time.sleep(12)
            print("\n[mock] 全部事件推送完毕，观察上方机器人回复。Ctrl+C 退出。")

        threading.Thread(target=push, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[mock] 退出")


if __name__ == "__main__":
    main()
