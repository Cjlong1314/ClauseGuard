import json
import urllib.request

BASE = "http://127.0.0.1:8600"


def call(path, method="GET", data=None, token=None):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Token", token)
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}


print("register:", call("/api/auth/register", "POST", {"username": "kb_tester2", "password": "kbtest123"}))
print("login:", call("/api/auth/login", "POST", {"username": "kb_tester2", "password": "kbtest123"}))
