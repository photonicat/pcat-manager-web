#!/usr/bin/env python3
"""Test charge-threshold get/set via pcat-manager Unix socket.

Usage:
  python3 test_charge_limit.py          # Read current value
  python3 test_charge_limit.py 85       # Set value to 85
"""

import json
import socket
import sys

SOCK_PATH = "/tmp/pcat-manager.sock"

def send_and_recv(cmd):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(SOCK_PATH)
    except Exception as e:
        print(f"Failed to connect to {SOCK_PATH}: {e}")
        sys.exit(1)

    payload = json.dumps(cmd).encode() + b'\0'
    print(f">>> Sending: {json.dumps(cmd)}")
    sock.sendall(payload)

    # Read response until null byte
    buf = b''
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b'\0' in buf:
                break
    except socket.timeout:
        print("Timeout waiting for response")
        sock.close()
        return None

    sock.close()

    # Parse response(s) - may receive multiple JSON messages
    messages = buf.split(b'\0')
    for msg in messages:
        msg = msg.strip()
        if not msg:
            continue
        try:
            data = json.loads(msg)
            print(f"<<< Received: {json.dumps(data, indent=2)}")
            if data.get("command") in ("charge-threshold-get", "charge-threshold-set"):
                return data
        except json.JSONDecodeError:
            print(f"<<< Raw: {msg}")
    return None

if __name__ == "__main__":
    if len(sys.argv) > 1:
        value = int(sys.argv[1])
        if value < 80 or value > 100:
            print("Error: value must be between 80 and 100")
            sys.exit(1)
        print(f"--- Setting charge threshold to {value}% ---")
        resp = send_and_recv({"command": "charge-threshold-set", "value": value})
    else:
        print("--- Reading charge threshold ---")
        resp = send_and_recv({"command": "charge-threshold-get"})

    if resp:
        code = resp.get("code")
        val = resp.get("value")
        print(f"\nResult: code={code}, value={val}")
    else:
        print("\nNo matching response received")
