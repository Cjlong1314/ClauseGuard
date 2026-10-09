# -*- coding: utf-8 -*-
"""F5自测脚本：降级路径/依赖探测/文本层不回归/真实OCR全链路"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
PASS = []

def check(name, ok, detail=""):
    PASS.append(ok)
    print("[%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))

# 1. 依赖探测
try:
    import paddleocr
    HAS_PADDLE = True
except Exception:
    HAS_PADDLE = False
print("paddleocr可用:", HAS_PADDLE)

from app import ocr as ocr_mod
from app import parser as parser_mod
from app import config

# 2. 降级路径：paddle未安装时ocr_file应抛OcrUnavailable且含安装指引
if not HAS_PADDLE:
    try:
        ocr_mod.ocr_file(Path("nonexistent.png"))
        check("降级抛出OcrUnavailable", False)
    except ocr_mod.OcrUnavailable as e:
        check("降级抛出OcrUnavailable", "安装指引" in str(e) or "paddle" in str(e).lower())
    check("is_available=False", ocr_mod.is_available() is False)
else:
    check("paddleocr已安装", True)

# 3. 文本层PDF/DOCX不回归
from docx import Document
d = Document()
for line in ["技术服务合同", "第一条 服务内容", "乙方提供系统开发服务。", "第二条 金额", "总金额人民币50万元。"]:
    d.add_paragraph(line)
d.save("test_f5.docx")
r = parser_mod.parse_document(Path("test_f5.docx"))
check("DOCX解析不回归", r["source"] == "text_layer" and "50万元" in r["text"])
clauses = parser_mod.split_clauses(r["text"])
check("DOCX条款切分不回归", any(c["clause_id"] == "二" or c["clause_id"] == "2" for c in clauses),
      str([c["clause_id"] for c in clauses]))

# 4. 文本层PDF走原路径
try:
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    c = canvas.Canvas("test_f5_text.pdf")
    y = 800
    for line in ["技术服务合同", "第一条 服务内容 乙方提供系统开发服务，含需求分析与开发部署。", "第二条 合同金额 总金额人民币50万元，签约后支付。", "第三条 违约责任 违约方赔偿全部损失。", "第四条 争议解决 协商不成的提交仲裁。"]:
        c.setFont("STSong-Light", 12)
        c.drawString(72, y, line)
        y -= 40
    c.save()
    r = parser_mod.parse_document(Path("test_f5_text.pdf"))
    check("文本层PDF不回归", r["source"] == "text_layer" and "50万元" in r["text"])
    check("文本层PDF判定非扫描件", not ocr_mod.is_scanned_pdf(Path("test_f5_text.pdf")))
    HAS_RL = True
except ImportError:
    print("[SKIP] reportlab未安装，文本层PDF用例跳过")
    HAS_RL = False

# 5. 真实OCR全链路（仅paddle已装时）
if HAS_PADDLE:
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("simhei.ttf", 28)
    except Exception:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 28)
    img = Image.new("RGB", (900, 400), "white")
    dr = ImageDraw.Draw(img)
    lines = ["技术服务合同", "第一条 服务内容", "乙方向甲方提供系统开发服务。", "第二条 合同金额", "总金额人民币50万元。"]
    y = 20
    for line in lines:
        dr.text((40, y), line, fill="black", font=font)
        y += 60
    img.save("test_f5_scan.png")
    # 图片直接OCR
    r = ocr_mod.ocr_file(Path("test_f5_scan.png"))
    all_text = r["text"]
    check("图片OCR识别出合同文字", ("50万元" in all_text) or ("50" in all_text and "万元" in all_text),
          "识别片段数=%d" % len(r["pages"][0]["text"].split("\n")))
    check("OCR带置信度", all(p["avg_conf"] > 0 for p in r["pages"]))
    low = [s for p in r["pages"] for s in [{"conf": 0}] if p["avg_conf"] < config.OCR_MIN_CONF]
    check("低置信标记逻辑可用", r["need_review"] == bool(r["low_conf_pages"]))
    # 扫描件PDF全链路：图片嵌入PDF
    if HAS_RL:
        img.save("page_scan.jpg")
        c2 = canvas.Canvas("test_f5_scan.pdf")
        c2.drawImage("page_scan.jpg", 40, 400, width=500, height=280)
        c2.showPage(); c2.save()
        check("扫描件PDF判定", ocr_mod.is_scanned_pdf(Path("test_f5_scan.pdf")))
        doc = parser_mod.parse_document(Path("test_f5_scan.pdf"))
        check("parser自动走OCR通道", doc["source"] == "ocr")
        cls = parser_mod.split_clauses(doc["text"])
        check("OCR文本条款切分", len(cls) >= 1, "条款数=%d" % len(cls))

print("\n结果: %d/%d PASS" % (sum(PASS), len(PASS)))
sys.exit(0 if all(PASS) else 1)
