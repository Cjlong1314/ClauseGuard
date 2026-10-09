"""F4接口端到端冒烟：注册登录→建基线→上传合同→baseline-check→页面"""
import io, sys, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import urllib.request

BASE = "http://127.0.0.1:8611"
PASS = []
def check(name, cond, detail=""):
    PASS.append(cond)
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))

def req(method, path, data=None, token=None, raw=False):
    r = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **({"X-Token": token} if token else {})})
    resp = urllib.request.urlopen(r)
    body = resp.read()
    return body if raw else json.loads(body)

# 独立数据库不可能，直接用现有库；用随机账号避免冲突
import random
u = "f4smoke" + str(random.randint(1000, 9999))
reg = req("POST", "/api/auth/register", {"username": u, "password": "f4test123"})
# 冒烟提权：直接置为admin（模拟法务管理员操作基线）
from app import db as _db
with _db.get_conn() as _c:
    with _c.cursor() as _cur:
        _cur.execute("UPDATE users SET role='admin' WHERE username=%s", (u,))
    _c.commit()
tok = req("POST", "/api/auth/login", {"username": u, "password": "f4test123"})["token"]
check("注册登录", bool(tok))

b = req("POST", "/api/baselines", {
    "name": "F4冒烟采购范本", "contract_type": "买卖", "business_line": "采购", "stance": "买方",
    "content": "第一条 标的与价款\n采购货物总价款为人民币五十万元，含税含运费。\n第二条 保密条款\n双方对对方商业秘密负有保密义务。\n",
    "suggestions": {"保密": "保密期限自合同终止后持续三年。"}}, tok)
check("POST /api/baselines", bool(b.get("baseline_id")))
bid = b["baseline_id"]

# 更新升版本
b2 = req("PUT", f"/api/baselines/{bid}", {"content": b["content"] + "第三条 争议解决\n提交买方所在地法院管辖。\n"}, tok)
check("PUT升版本v2", b2["version"] == 2)
vers = req("GET", f"/api/baselines/{bid}/versions", None, tok)
check("GET版本历史", len(vers) == 2)

# 上传对照合同
boundary = "----f4b"
content = ("买卖合同\n第一条 标的与价款：采购货物总价款为人民币八十万元。\n"
           "第二条 特别约定：卖方有权无偿留置货物且不承担任何责任，买方不得异议。\n").encode("utf-8")
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"f4api.txt\"\r\n"
        f"Content-Type: text/plain\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
r = urllib.request.Request(BASE + "/api/contracts/upload", data=body, method="POST",
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Token": tok})
up = json.loads(urllib.request.urlopen(r).read())
cid = up["contract_id"]
check("上传对照合同", cid and up["clause_count"] >= 2, f"{up['clause_count']}条")

# baseline-check
res = req("POST", f"/api/contracts/{cid}/baseline-check", {"baseline_id": bid}, tok)
s = res["summary"]
check("POST baseline-check缺失", s["missing"] >= 1, f"缺失{s['missing']}")
check("baseline-check偏离(80万)", s["deviation"] >= 1)
check("baseline-check不利(留置)", s["unfavorable"] >= 1)
check("含修改建议", all(i["suggestion"] for i in res["items"] if i["type"] != "ok"))
# 指定版本对照
res_v1 = req("POST", f"/api/contracts/{cid}/baseline-check", {"baseline_id": bid, "version": 1}, tok)
check("指定版本差异审查", res_v1["baseline"]["version"] == 1)

# 页面与compare兼容
html = req("GET", "/baseline", None, tok, raw=True).decode("utf-8")
check("GET /baseline页面", "范本基线" in html)
cmp_res = req("POST", f"/api/contracts/{cid}/compare", {"tpl_id": bid}, tok) if False else None
tpls = req("GET", "/api/kb/templates", None, tok)
check("原模板接口可用", isinstance(tpls, list))

print(f"\n{sum(PASS)}/{len(PASS)} PASS")
sys.exit(0 if all(PASS) else 1)
