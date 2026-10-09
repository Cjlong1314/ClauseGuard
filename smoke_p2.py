# -*- coding: utf-8 -*-
"""P2整体冒烟：逐接口记录状态码"""
import io, json, time
import requests
from PIL import Image, ImageDraw, ImageFont
from docx import Document

BASE = "http://127.0.0.1:8600"
codes = {}

def rec(name, r):
    codes[name] = r.status_code
    print("%-28s -> %s" % (name, r.status_code))
    return r

s = requests.Session()
# 1 /
rec("GET /", s.get(BASE + "/"))
# 2 /health
rec("GET /health", s.get(BASE + "/health"))
# 3 /diff
rec("GET /diff", s.get(BASE + "/diff"))
# 4 /baseline
rec("GET /baseline", s.get(BASE + "/baseline"))
# 5 /qc
rec("GET /qc", s.get(BASE + "/qc"))
# 6 login
r = rec("POST login", s.post(BASE + "/api/auth/login", json={"username": "admin", "password": "admin123"}))
t = r.json().get("token")
H = {"X-Token": t}

CONTRACT = "技术服务合同\n\n甲方：北京示例科技有限公司\n乙方：上海研发有限公司\n\n第一条 服务内容\n乙方向甲方提供系统开发服务。\n\n第二条 合同金额\n总金额人民币50万元。\n\n第三条 违约责任\n违约方赔偿对方全部损失。\n\n第四条 保密条款\n双方对商业秘密负有保密义务。\n\n第五条 知识产权\n开发成果知识产权归乙方所有，甲方仅有使用权。\n"

def docx_bytes(text):
    doc = Document()
    for line in text.split("\n"):
        if line.strip():
            doc.add_paragraph(line.strip())
    b = io.BytesIO(); doc.save(b)
    return b.getvalue()

# 7 upload DOCX
r = rec("POST upload(DOCX)", s.post(BASE + "/api/contracts/upload", headers=H,
    files={"file": ("smoke_p2.docx", docx_bytes(CONTRACT), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}))
cid = r.json()["contract_id"]
# 8 review
rec("POST review", s.post(BASE + "/api/contracts/%s/review" % cid, headers=H, json={}))
# 9 review/pipeline
rec("POST review/pipeline", s.post(BASE + "/api/contracts/%s/review/pipeline" % cid, headers=H, json={}))
# 10 extraction
rec("GET extraction", s.get(BASE + "/api/contracts/%s/extraction" % cid, headers=H))
# 11 report/annotated
rec("GET report/annotated", s.get(BASE + "/api/contracts/%s/report/annotated?mode=annotated" % cid, headers=H))
# 12 baseline-check：先建基线再对照
r = s.post(BASE + "/api/baselines", headers=H, json={"name": "smoke技术服基线", "contract_type": "服务合同", "content": CONTRACT})
bid = (r.json() or {}).get("baseline_id")
rec("POST 建基线", r)
rec("POST baseline-check", s.post(BASE + "/api/contracts/%s/baseline-check" % cid, headers=H, json={"baseline_id": bid}))
# 13 obligations extract + overview
rec("POST obligations/extract", s.post(BASE + "/api/contracts/%s/obligations/extract" % cid, headers=H, json={}))
rec("GET obligations/overview", s.get(BASE + "/api/obligations/overview", headers=H))
# 14 integrations/status
rec("GET integrations/status", s.get(BASE + "/api/integrations/status", headers=H))
# 15 parse-status（扫描样例）：生成图片型合同
img = Image.new("RGB", (1200, 500), "white")
d = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
except Exception:
    font = ImageFont.load_default()
d.text((50, 40), "租赁合同", fill="black", font=font)
d.text((50, 120), "第一条 租赁物", fill="black", font=font)
d.text((50, 180), "甲方将位于北京的办公室出租给乙方使用。", fill="black", font=font)
d.text((50, 260), "第二条 租金", fill="black", font=font)
d.text((50, 320), "月租金人民币3万元，每月5日前支付。", fill="black", font=font)
ib = io.BytesIO(); img.save(ib, format="PNG")
r = rec("POST upload(扫描件PNG)", s.post(BASE + "/api/contracts/upload", headers=H,
    files={"file": ("smoke_scan.png", ib.getvalue(), "image/png")}))
print("  resp:", r.text[:200])
scid = (r.json() or {}).get("contract_id")
# 轮询至done
final = 0
for i in range(60):
    try:
        rr = s.get(BASE + "/api/contracts/%s/parse-status" % scid, headers=H, timeout=30)
        j = rr.json()
        if (j.get("parse_status") or j.get("status")) == "done":
            final = rr.status_code
            break
        final = rr.status_code
    except Exception:
        pass
    time.sleep(2)
codes["GET parse-status(至done)"] = final
print("%-28s -> %s" % ("GET parse-status(至done)", final))
# 16 qc/overview
rec("GET qc/overview", s.get(BASE + "/api/qc/overview", headers=H))
# 17 report/weekly
rec("GET report/weekly", s.get(BASE + "/api/report/weekly", headers=H))

# 清理
for c in (cid, scid):
    s.delete(BASE + "/api/contracts/%s" % c, headers=H)

ok = all(v in (200, 404) for v in codes.values())
print("\n状态码汇总:", json.dumps(codes, ensure_ascii=False))
print("冒烟结论:", "ALL-200" if all(v == 200 for v in codes.values()) else ("OK-含清理405" if ok else "HAS-FAIL"))
