"""Send fake PermissionRequest / Notification to the local hook server."""

import argparse
import json
import socket
import uuid


def send_hook(port: int, payload: dict) -> str:
    body = json.dumps(payload).encode()
    request = (
        f"POST /hook HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body

    sock = socket.create_connection(("127.0.0.1", port), timeout=310)
    sock.sendall(request)

    # Read full response
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
    sock.close()
    return b"".join(chunks).decode("utf-8", errors="replace")


def test_permission(port: int, cwd: str) -> None:
    payload = {
        "session_id": str(uuid.uuid4()),
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {
            "command": "rm -rf /tmp/test-dir",
            "description": "Delete test directory",
        },
        "cwd": cwd,
        "message": "Claude wants to run: rm -rf /tmp/test-dir",
        "permission_suggestions": [
            {
                "behavior": "allow",
                "rules": [
                    {"ruleContent": "Bash(rm -rf /tmp/*)"},
                ],
            }
        ],
    }
    print(f"Sending PermissionRequest to :{port} ...")
    print(f"  tool: {payload['tool_name']}")
    print(f"  cwd:  {payload['cwd']}")
    resp = send_hook(port, payload)
    print(f"Response:\n{resp}\n")


def test_notification(port: int, cwd: str) -> None:
    payload = {
        "session_id": str(uuid.uuid4()),
        "hook_event_name": "Notification",
        "cwd": cwd,
        "message": "Crunched for 4m 3s",
        "title": "Task Complete",
    }
    print(f"Sending Notification to :{port} ...")
    resp = send_hook(port, payload)
    print(f"Response:\n{resp}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test hook server")
    parser.add_argument("--port", type=int, default=19836)
    parser.add_argument("--cwd", default="/Users/hb37096/Documents/code/agent-tmux-notify")
    parser.add_argument("event", choices=["permission", "notification", "both"], default="both", nargs="?")
    args = parser.parse_args()

    if args.event in ("permission", "both"):
        test_permission(args.port, args.cwd)
    if args.event in ("notification", "both"):
        test_notification(args.port, args.cwd)


if __name__ == "__main__":
    main()
