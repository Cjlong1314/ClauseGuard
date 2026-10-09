"""审查报告生成模块
使用python-docx生成《合同审查报告.docx》
结构：标题 -> 合同基本信息表 -> 审查结论 -> 风险清单表 -> 落款日期
安全要求：每页页脚添加灰色小字水印"仅供内部审查使用"
"""
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from .models import Contract

WATERMARK_TEXT = "仅供内部审查使用"


def _add_page_watermark(doc: Document):
    """给所有节的页脚添加文字水印（灰色小字号，含页码域）"""
    for section in doc.sections:
        footer = section.footer
        footer.is_linked_to_previous = False
        p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        p.text = ""
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"{WATERMARK_TEXT}    ")
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0xA0, 0xA0, 0xA0)
        run.font.name = "Microsoft YaHei"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

# 风险等级颜色（RGB）
RISK_COLORS = {
    "高": RGBColor(0xC0, 0x00, 0x00),
    "中": RGBColor(0xBF, 0x8F, 0x00),
    "低": RGBColor(0x2E, 0x75, 0xB6),
}


def _set_cell_text(cell, text: str, bold: bool = False):
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.font.size = Pt(10.5)
    run.font.bold = bold


def generate_report(contract: Contract, out_path: Path) -> Path:
    """根据Contract生成docx审查报告并返回out_path"""
    doc = Document()

    # ---------- 标题 ----------
    title = doc.add_heading("合同审查报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ---------- 合同基本信息表 ----------
    doc.add_heading("一、合同基本信息", level=1)
    extracted = contract.extracted
    parties = "、".join(
        f"{p.role}：{p.name}" + (f"（{p.identifier}）" if p.identifier else "")
        for p in extracted.parties
    ) if extracted and extracted.parties else "未识别"

    info_rows = [
        ("文件名", contract.filename),
        ("合同类型", extracted.contract_type if extracted else "未识别"),
        ("主体", parties),
        ("金额", (extracted.amount if extracted and extracted.amount else "未识别")),
        ("期限", (extracted.term if extracted and extracted.term else "未识别")),
        ("签署日期", (extracted.sign_date if extracted and extracted.sign_date else "未识别")),
    ]
    table = doc.add_table(rows=len(info_rows), cols=2)
    table.style = "Table Grid"
    for i, (k, v) in enumerate(info_rows):
        _set_cell_text(table.rows[i].cells[0], k, bold=True)
        _set_cell_text(table.rows[i].cells[1], v)
    table.columns[0].width = None  # 交给Word自适应

    # ---------- 审查结论 ----------
    doc.add_heading("二、审查结论", level=1)
    review = contract.review
    if review is None:
        conclusion = doc.add_paragraph(
            "该合同尚未进行智能审查，风险情况未知，请先执行审查。"
        )
        conclusion.runs[0].font.color.rgb = RGBColor(0x80, 0x80, 0x80)
    else:
        findings = review.findings
        high = sum(1 for f in findings if f.risk_level == "高")
        mid = sum(1 for f in findings if f.risk_level == "中")
        low = sum(1 for f in findings if f.risk_level == "低")
        if not findings:
            text = "经审查，未发现明显违法违规或重大风险条款，建议按常规流程签署。"
        elif high:
            text = (
                f"经审查，共发现{len(findings)}项风险（高风险{high}项、中风险{mid}项、"
                f"低风险{low}项），存在高风险条款，建议修改完善并经执业律师复核后再行签署。"
            )
        else:
            text = (
                f"经审查，共发现{len(findings)}项风险（中风险{mid}项、低风险{low}项），"
                "建议关注相关条款并酌情完善。"
            )
        doc.add_paragraph(text)
        doc.add_paragraph(f"审查时间：{review.reviewed_at}")

    # ---------- 风险清单表 ----------
    doc.add_heading("三、风险清单", level=1)
    if review is None or not review.findings:
        doc.add_paragraph("无。")
    else:
        risk_table = doc.add_table(rows=1, cols=6)
        risk_table.style = "Table Grid"
        headers = ["风险等级", "审查维度", "条款编号", "规则", "法律依据", "修改建议"]
        for i, h in enumerate(headers):
            _set_cell_text(risk_table.rows[0].cells[i], h, bold=True)
        for f in review.findings:
            row = risk_table.add_row()
            _set_cell_text(row.cells[0], f.risk_level)
            # 高风险标红
            if f.risk_level == "高":
                row.cells[0].paragraphs[0].runs[0].font.color.rgb = RISK_COLORS["高"]
            _set_cell_text(row.cells[1], f.dimension)
            _set_cell_text(row.cells[2], f.clause_id or "—")
            _set_cell_text(row.cells[3], f.rule_name)
            _set_cell_text(row.cells[4], f.legal_basis)
            _set_cell_text(row.cells[5], f.suggestion)

    # ---------- 落款日期 ----------
    tail = doc.add_paragraph()
    tail.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    tail.add_run(f"ClauseGuard合同智能审查系统\n{date.today().strftime('%Y年%m月%d日')}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _add_page_watermark(doc)  # 页脚水印（安全要求：仅供内部审查使用）
    doc.save(str(out_path))
    return out_path
