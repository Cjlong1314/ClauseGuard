# -*- coding: utf-8 -*-
"""P1冒烟：注册登录→上传→review→pipeline→extraction→annotated→diff→baseline-check→obligations→status，全记录状态码"""
import json
import urllib.request

BASE = "http://127.0.0.1:8600"


def req(method, path, token=None, data=None, raw=None, ctype=None):
    r = urllib.request.Request(BASE + path, method=method)
    if token:
        r.add_header("X-Token", token)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        r.add_header("Content-Type", "application/json")
    elif raw is not None:
        body = raw
        r.add_header("Content-Type", ctype or "application/octet-stream")
    try:
        resp = urllib.request.urlopen(r, body, timeout=60)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


results = []


def mark(name, code, expect=(200,)):
    results.append((name, code, "OK" if code in expect else "FAIL"))
    print(name, "->", code)


mark("GET /", req("GET", "/")[0])
mark("GET /health", req("GET", "/health")[0])
mark("GET /baseline页", req("GET", "/baseline")[0])
mark("GET /diff页", req("GET", "/diff")[0])

st, b = req("POST", "/api/auth/register", data={"username": "smoke_p1", "password": "Smoke@2026", "role": "admin"})
st, b = req("POST", "/api/auth/login", data={"username": "smoke_p1", "password": "Smoke@2026"})
tok = json.loads(b).get("token", "")
mark("POST login", st)

text = "服务合同\n第一条 服务内容：乙方向甲方提供系统运维服务。\n第二条 服务费：每年50万元，合同签订后30日内支付50%。\n第三条 交付：2026年11月30日前完成部署。\n第四条 保密：双方对合作信息保密。\n第五条 违约：逾期每日按合同金额千分之五支付违约金。".encode("utf-8")
boundary = "----smoke"
part = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"p1smoke.txt\"\r\n"
        f"Content-Type: text/plain\r\n\r\n").encode("utf-8") + text + f"\r\n--{boundary}--\r\n".encode("utf-8")
st, b = req("POST", "/api/contracts/upload", tok, raw=part,
            ctype=f"multipart/form-data; boundary={boundary}")
cid = json.loads(b).get("contract_id", "")
mark("POST upload", st)

mark("POST review", req("POST", f"/api/contracts/{cid}/review", tok)[0])
mark("POST review/pipeline", req("POST", f"/api/contracts/{cid}/review/pipeline", tok)[0])
mark("GET extraction", req("GET", f"/api/contracts/{cid}/extraction", tok)[0])
mark("GET report/annotated", req("GET", f"/api/contracts/{cid}/report/annotated?mode=annotated", tok)[0])

st, b = req("POST", "/api/baselines", tok, data={"name": "服务类基线", "contract_type": "服务",
            "business_line": "运维", "stance": "甲方", "content": "第一条 服务内容\n第二条 付款方式\n第三条 保密条款\n第六条 数据安全"})
bid = json.loads(b).get("baseline_id", "")
mark("POST 建基线", st)
st, b = req("POST", f"/api/contracts/{cid}/baseline-check", tok, data={"baseline_id": bid})
mark("POST baseline-check", st)

mark("POST obligations/extract", req("POST", f"/api/contracts/{cid}/obligations/extract", tok)[0])
mark("GET obligations/overview", req("GET", "/api/obligations/overview", tok)[0])
mark("GET integrations/status", req("GET", "/api/integrations/status", tok)[0])

print("\n汇总：")
for n, c, r in results:
    print(f"{r}  {c}  {n}")
bad = [r for r in results if r[2] == "FAIL"]
print(f"\n{len(results)-len(bad)}/{len(results)} OK", "失败项:" + str(bad) if bad else "")
with open("smoke_p1_result.json", "w", encoding="utf-8") as f:
    json.dump({"cid": cid, "bid": bid, "results": results}, f, ensure_ascii=False)
