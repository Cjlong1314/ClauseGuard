# -*- coding: utf-8 -*-
"""P2期集成自测：F5扫描件全链路 / F9质检闭环 / F5-F9互不干扰 / P0-P1模块不回归"""
import io, json, time
import requests
from docx import Document

BASE = "http://127.0.0.1:8600"
RESULTS = []

def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("[%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))

def docx_bytes(text):
    doc = Document()
    for line in text.split("\n"):
        if line.strip():
            doc.add_paragraph(line.strip())
    b = io.BytesIO(); doc.save(b)
    return b.getvalue()

CONTRACT = """技术服务合同

甲方：北京示例科技有限公司
乙方：上海研发有限公司

第一条 服务内容
乙方向甲方提供系统开发服务。

第二条 合同金额
总金额人民币50万元。

第三条 违约责任
违约方赔偿对方全部损失。

第四条 保密条款
双方对商业秘密负有保密义务。

第五条 知识产权
开发成果知识产权归乙方所有，甲方仅有使用权。
"""

def main():
    s = requests.Session()
    r = s.post(BASE + "/api/auth/login", json={"username": "admin", "password": "admin123"})
    token = r.json().get("token") or r.json().get("access_token")
    H = {"X-Token": token}
    check("登录", r.status_code == 200 and token)

    # ===== F5：扫描件上传→parse-status→条款切分→pipeline审查全链路 =====
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 700), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
        small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
    except Exception:
        font = ImageFont.load_default(); small = font
    lines = ["技术服务合同", "第一条 服务内容", "乙方向甲方提供系统开发服务。",
             "第二条 合同金额", "总金额人民币50万元。", "第三条 知识产权", "开发成果知识产权归乙方所有。"]
    y = 40
    for i, ln in enumerate(lines):
        d.text((60, y), ln, fill="black", font=font if i == 0 else small)
        y += 70 if i == 0 else 60
    ib = io.BytesIO(); img.save(ib, format="PNG")
    # 图片包进PDF（pdfplumber可读渲染的简单PDF用reportlab可能未装；直接传图片PDF不可行时传纯图片文件由OCR处理）
    try:
        from PIL import Image as _I
        pdfb = _make_image_pdf(ib.getvalue())
        fname, ctype = ("scan.pdf", "application/pdf")
        payload = pdfb
    except Exception:
        payload = ib.getvalue(); fname, ctype = ("scan.png", "image/png")

    t0 = time.time()
    r = s.post(BASE + "/api/contracts/upload", headers=H,
               files={"file": (fname, payload, ctype)})
    up = r.json()
    check("F5扫描件上传(异步)", r.status_code == 200 and up.get("parse_status") == "processing",
          "resp=%s 耗时%.1fs" % (up.get("parse_status"), time.time() - t0))
    cid = up["contract_id"]
    # 轮询parse-status
    status, ocr, clauses = None, {}, 0
    for i in range(60):
        try:
            r = s.get(BASE + "/api/contracts/%s/parse-status" % cid, headers=H, timeout=30)
            j = r.json()
        except Exception:
            time.sleep(2 if i < 10 else 4)
            continue
        status = j.get("parse_status") or j.get("status")
        if status == "done":
            ocr = j.get("ocr") or {}
            break
        time.sleep(1)
    check("F5 parse-status至done", status == "done", "ocr.avg_conf=%s" % ocr.get("avg_conf"))
    r = s.get(BASE + "/api/contracts/%s" % cid, headers=H)
    clauses = len(r.json().get("clauses") or [])
    check("F5 OCR条款切分", clauses >= 2, "条款数=%d" % clauses)
    r = s.post(BASE + "/api/contracts/%s/review/pipeline" % cid, headers=H, json={})
    j = r.json()
    summary = j.get("summary") or {}
    check("F5扫描件→pipeline审查", r.status_code == 200 and j.get("findings") is not None,
          "mode=%s 发现=%d" % (summary.get("pipeline_mode"), len(j.get("findings") or [])))

    # ===== F9：抽样→复核→qc/overview统计 =====
    r = s.post(BASE + "/api/qc/assign", headers=H, json={})
    check("F9抽样assign", r.status_code == 200, str(r.json())[:80])
    r = s.get(BASE + "/api/qc/overview", headers=H)
    ov = r.json()
    pending = ov.get("pending_list") or ov.get("pending") or []
    check("F9 qc/overview统计", r.status_code == 200 and "total" in json.dumps(ov), "total=%s reviewed=%s" % (ov.get("total"), ov.get("reviewed")))
    # 复核一条
    reviewed_ok = False
    qid = None
    if pending:
        first = pending[0]
        qid = first.get("qc_id") or first.get("id")
    if qid is None:
        # 从overview其他字段找qc_id
        for k in ("items", "list", "reviews"):
            for it in (ov.get(k) or []):
                if not it.get("verdict") and (it.get("qc_id") or it.get("id")):
                    qid = it.get("qc_id") or it.get("id"); break
            if qid: break
    if qid:
        r = s.post(BASE + "/api/qc/review", headers=H, json={"qc_id": qid, "verdict": "correct", "comment": "p2集成自测"})
        reviewed_ok = r.status_code == 200
    check("F9提交复核", reviewed_ok, "qc_id=%s" % qid)
    r = s.get(BASE + "/api/qc/overview", headers=H)
    prog = (r.json().get("progress") or {})
    check("F9复核后统计更新", r.status_code == 200 and (prog.get("reviewed") or 0) >= 1,
          "reviewed=%s" % prog.get("reviewed"))

    # ===== F5/F9互不干扰：F5合同存在期间qc接口与upload接口均正常 =====
    r = s.get(BASE + "/api/contracts/%s/parse-status" % cid, headers=H)
    check("互不干扰: F5状态查询正常", r.status_code == 200)
    r = s.get(BASE + "/api/qc/overview", headers=H)
    check("互不干扰: F9接口正常", r.status_code == 200)
    r = s.post(BASE + "/api/contracts/upload", headers=H,
               files={"file": ("p2_docx.docx", docx_bytes(CONTRACT), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    cid2 = r.json().get("contract_id")
    check("互不干扰: DOCX上传不受OCR影响", r.status_code == 200 and cid2, "clause_count=%s" % r.json().get("clause_count"))
    r = s.post(BASE + "/api/contracts/%s/review" % cid2, headers=H, json={})
    check("互不干扰: DOCX规则审查", r.status_code == 200)

    # ===== P0/P1关键模块导入不回归 =====
    import importlib
    for mod in ("app.pipeline", "app.annotator", "app.diff", "app.baseline", "app.obligations", "app.integrations.base", "app.ocr", "app.qc", "app.weekly"):
        try:
            importlib.import_module(mod)
            check("导入.%s" % mod, True)
        except Exception as e:
            check("导入.%s" % mod, False, str(e)[:60])

    # ===== 清理 =====
    for c in (cid, cid2):
        s.delete(BASE + "/api/contracts/%s" % c, headers=H)
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print("\n结果: %d/%d PASS" % (len(RESULTS) - n_fail, len(RESULTS)))
    raise SystemExit(1 if n_fail else 0)

def _make_image_pdf(png_bytes):
    """把PNG包成一个页面图片型PDF（纯手工构造，无新增依赖）：用pdfplumber不可写，改用简单嵌入方案——
    若reportlab可用则用它，否则抛异常走降级。"""
    import reportlab
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader
    b = io.BytesIO()
    c = rl_canvas.Canvas(b, pagesize=(1400, 700))
    c.drawImage(ImageReader(io.BytesIO(png_bytes)), 0, 0, 1400, 700)
    c.save()
    return b.getvalue()

if __name__ == "__main__":
    main()
