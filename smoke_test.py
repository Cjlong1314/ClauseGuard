# -*- coding: utf-8 -*-
"""P0整体冒烟验收脚本：逐接口验证"""
import json
import sys
import urllib.request
import uuid

BASE = "http://127.0.0.1:8600"
R = []


def req(name, method, path, data=None, files=None, raw=False, token=""):
    url = BASE + path
    try:
        if files:
            boundary = uuid.uuid4().hex
            body = b""
            for k, (fn, content, ctype) in files.items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                         f"filename=\"{fn}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + content + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            r = urllib.request.Request(url, data=body, method="POST",
                                       headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Token": token})
        elif data is not None:
            r = urllib.request.Request(url, data=json.dumps(data).encode(), method=method,
                                       headers={"Content-Type": "application/json", "X-Token": token})
        else:
            r = urllib.request.Request(url, method=method, headers={"X-Token": token})
        with urllib.request.urlopen(r, timeout=120) as resp:
            content = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
        content = e.read()
    except Exception as e:
        R.append((name, "ERR", str(e)))
        print(f"[ERR ] {name}: {e}")
        return None
    try:
        out = json.loads(content)
    except Exception:
        out = content[:100] if not raw else content
    R.append((name, code, ""))
    print(f"[{code}] {name} {str(out)[:200]}")
    return code, out


print("=" * 40, "\nP0整体冒烟验收\n", "=" * 40)

# 1 首页
code, _ = req("GET / 首页", "GET", "/")
home_ok = code == 200

# 1.5 注册+登录拿Token
user = f"smoke{int(__import__('time').time())}"
req("POST /api/auth/register", "POST", "/api/auth/register", data={"username": user, "password": "Smoke@2026"})
_, lg = req("POST /api/auth/login", "POST", "/api/auth/login", data={"username": user, "password": "Smoke@2026"})
token = (lg or {}).get("token", "") if isinstance(lg, dict) else ""
print(f"    token={token[:12]}...")

# 2 上传
docx = open("smoke_contract_v1.docx", "rb").read()
docx2 = open("smoke_contract_v2.docx", "rb").read()
code, up = req("POST /api/contracts/upload (v1)", "POST", "/api/contracts/upload", token=token,
               files={"file": ("smoke_contract_v1.docx", docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
cid = (up or {}).get("contract_id") or (up or {}).get("id") if isinstance(up, dict) else None
code2, up2 = req("POST /api/contracts/upload (v2)", "POST", "/api/contracts/upload", token=token,
                 files={"file": ("smoke_contract_v2.docx", docx2, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
cid2 = (up2 or {}).get("contract_id") or (up2 or {}).get("id") if isinstance(up2, dict) else None
print(f"    cid={cid} cid2={cid2}")

# 3 原 POST /review
code, _ = req("POST /api/contracts/{cid}/review (原接口)", "POST", f"/api/contracts/{cid}/review", token=token) if cid else (None, None)

# 4 pipeline
code, _ = req("POST /api/contracts/{cid}/review/pipeline", "POST", f"/api/contracts/{cid}/review/pipeline", token=token) if cid else (None, None)

# 5 extraction
code, _ = req("GET /api/contracts/{cid}/extraction", "GET", f"/api/contracts/{cid}/extraction", token=token) if cid else (None, None)

# 6 annotated report
code, _ = req("GET /api/contracts/{cid}/report/annotated", "GET", f"/api/contracts/{cid}/report/annotated?mode=annotated", token=token, raw=True) if cid else (None, None)

# 7 diff
code, _ = req("POST /api/contracts/{cid}/diff", "POST", f"/api/contracts/{cid}/diff", token=token, data={"target_id": cid2}) if cid and cid2 else (None, None)

print("-" * 40)
n_err = sum(1 for _, s, _ in R if s == "ERR")
print(f"冒烟项: {len(R)}, 请求异常: {n_err}")
