#!/usr/bin/env python3
"""On-device call control for scripted tests.

Flask's csrf_guard needs a signed session cookie whose _csrf_token matches the
X-CSRF-Token header, even from localhost (only api_auth_required has the
localhost bypass), so mint one from /etc/pcat-secret-key rather than logging in.

  callctl.py make 13800138003 | answer | hangup | status
"""
import hashlib
import http.client
import json
import ssl
import sys

SECRET_PATH = "/etc/pcat-secret-key"
CSRF = "cli-test-token"


def cookie():
    from itsdangerous import URLSafeTimedSerializer
    from flask.sessions import TaggedJSONSerializer
    with open(SECRET_PATH) as f:
        secret = f.read().strip()
    s = URLSafeTimedSerializer(
        secret, salt="cookie-session", serializer=TaggedJSONSerializer(),
        signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1})
    return s.dumps({"username": "root", "_csrf_token": CSRF})


def call(method, path, body=None):
    c = http.client.HTTPSConnection("127.0.0.1", 9443,
                                    context=ssl._create_unverified_context(),
                                    timeout=20)
    headers = {"Cookie": "session=" + cookie(), "X-CSRF-Token": CSRF}
    if body is not None:
        body = json.dumps(body)
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    out = r.read().decode(errors="replace")
    c.close()
    return r.status, out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "make":
        st, out = call("POST", "/api/v1/voice_call/make", {"number": sys.argv[2]})
    elif cmd == "answer":
        st, out = call("POST", "/api/v1/voice_call/answer", {})
    elif cmd == "hangup":
        st, out = call("POST", "/api/v1/voice_call/hangup", {})
    elif cmd == "status":
        st, out = call("GET", "/api/v1/voice_call/status")
    else:
        print("unknown command", cmd)
        return 2
    print(st, out)
    return 0 if st == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
