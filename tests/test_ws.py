"""WebSocket 最小客户端的回环测试：本地起一个 raw socket 服务端，
验证握手、大帧（64-bit 长度）、分片消息与 ping/pong。

用法（项目根目录）: python -I tests/test_ws.py
"""

import base64
import hashlib
import os
import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douban_wrapped.browser_cookie import _MiniWS  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        raise SystemExit(1)
    print(f"ok: {msg}")


def server_once(port_holder):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])
    conn, _ = srv.accept()
    # 1. 握手
    req = b""
    while b"\r\n\r\n" not in req:
        req += conn.recv(4096)
    key = ""
    for line in req.decode().split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    # 2. 发一条 >64KB 的完整文本帧（64-bit 长度路径）
    payload = b"x" * 200000
    frame = bytearray([0x81, 127]) + struct.pack(">Q", len(payload)) + payload
    conn.sendall(bytes(frame))
    # 3. 发 ping，客户端应回 pong
    conn.sendall(bytes([0x89, 0x02]) + b"hi")
    pong = conn.recv(4096)
    check(pong[:1] == b"\x8a" and pong[1] & 0x80, "收到掩码 pong 帧")
    if pong[1] & 0x80:
        n = pong[1] & 0x7F
        mask = pong[2:6]
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(pong[6 : 6 + n]))
        check(body == b"hi", "pong 内容正确")
    # 4. 发分片消息：第一帧 + 续帧
    part1 = b"hello "
    part2 = b"websocket"
    conn.sendall(bytes([0x01, len(part1)]) + part1)
    conn.sendall(bytes([0x80, len(part2)]) + part2)
    conn.close()
    srv.close()


def main():
    holder: list[int] = []
    t = threading.Thread(target=server_once, args=(holder,), daemon=True)
    t.start()
    while not holder:
        pass
    port = holder[0]

    ws = _MiniWS("127.0.0.1", port, "/devtools/browser/test", timeout=10)
    msg = ws.recv_message()
    check(len(msg) == 200000 and msg == "x" * 200000, "200KB 大帧完整接收")
    msg = ws.recv_message()
    check(msg == "hello websocket", f"分片消息拼接 (实际 {msg!r})")
    ws.close()
    t.join(timeout=5)
    print("\n全部通过 [OK]")


if __name__ == "__main__":
    main()
