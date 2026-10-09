# -*- coding: utf-8 -*-
"""F5端到端冒烟：登录→上传扫描件PDF→异步状态查询→上传DOCX不回归"""
import io, json, time, requests
from docx import Document as Dx
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from PIL import Image, ImageDraw, ImageFont

BASE = "http://127.0.0.1:8600"
results = []

def check(name, ok, detail=""):
    results.append(ok)
    print("[%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))

s = requests.Session()
r = s.post(BASE + "/api/auth/login", json={"username": "admin", "password": "admin123"})
token = r.json().get("token") or r.json().get("access_token")
H = {"X-Token": token}
check("登录", r.status_code == 200)

# 生成扫描件PDF：白底图嵌合同文字（无文本层）
try:
    font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 26)
except Exception:
    font = ImageFont.load_default()
img = Image.new("RGB", (900, 400), "white")
dr = ImageDraw.Draw(img)
y = 20
for line in ["技术服务合同", "第一条 服务内容", "乙方向甲方提供系统开发服务。", "第二条 合同金额", "总金额人民币50万元。"]:
    dr.text((40, y), line, fill="black", font=font)
    y += 60
img.save("f5_e2e_page.jpg")
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
c = canvas.Canvas("f5_e2e_scan.pdf")
c.drawImage("f5_e2e_page.jpg", 40, 400, width=520, height=300)
c.showPage(); c.save()

# 上传扫描件→应走异步OCR通道
t0 = time.time()
r = s.post(BASE + "/api/contracts/upload", headers=H,
           files={"file": ("f5_scan.pdf", open("f5_e2e_scan.pdf", "rb"), "application/pdf")})
check("上传扫描件(应返回processing)", r.status_code == 200 and r.json().get("parse_status") == "processing",
      "耗时%.1fs resp=%s" % (time.time() - t0, {k: v for k, v in r.json().items() if k != "extracted"}))
cid = r.json()["contract_id"]

# 轮询解析状态（OCR模型已缓存，应在60s内完成）
status = ""
for i in range(30):
    time.sleep(2)
    r = s.get(BASE + "/api/contracts/%s/parse-status" % cid, headers=H)
    status = r.json().get("parse_status")
    if status in ("done", "failed"):
        break
check("OCR异步解析完成", status == "done", "状态=%s ocr=%s" % (status, json.dumps(r.json().get("ocr"), ensure_ascii=False)))
detail = r.json()
if status == "done":
    check("解析出条款", detail.get("clause_count", 0) >= 1, "条款数=%d" % detail.get("clause_count"))
    check("带置信度与复核标记", "ocr" in detail and "avg_conf" in detail["ocr"],
          "avg_conf=%s need_review=%s" % (detail["ocr"].get("avg_conf"), detail["ocr"].get("need_review")))

# 上传DOCX不回归（同步路径）
d = Dx()
for line in ["技术服务合同", "第一条 服务内容", "乙方提供开发服务。"]:
    d.add_paragraph(line)
b = io.BytesIO(); d.save(b)
r = s.post(BASE + "/api/contracts/upload", headers=H,
           files={"file": ("f5_docx.docx", b.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
check("DOCX同步上传不回归", r.status_code == 200 and r.json().get("source") == "text_layer",
      "clause_count=%s" % r.json().get("clause_count"))

# 清理
for c2 in (cid,):
    s.delete(BASE + "/api/contracts/%s" % c2, headers=H)
print("\n结果: %d/%d PASS" % (sum(results), len(results)))
