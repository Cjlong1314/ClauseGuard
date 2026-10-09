"""批注式审查报告回写Word模块（F1）
用python-docx读入合同原文，按条款定位段落锚点，将审查发现以OOXML批注
（word/comments.xml）方式写入对应条款位置，支持导出"带批注版"与"干净版"。
批注内容含：风险等级、规则名、法条依据、修改建议。
无法定位锚点时降级为在文档末尾追加"未定位批注清单"。
"""
from pathlib import Path
import uuid

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt

from .models import Contract

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ---------- OOXML批注底层写入 ----------

def _ensure_comments_part(doc: Document):
    """确保文档包含word/comments.xml部件，返回(部件, root元素)"""
    from docx.opc.part import Part
    from docx.opc.packuri import PackURI
    from lxml import etree

    class _CommentsPart(Part):
        """覆盖blob序列化，保存时从_element（lxml root）输出XML"""
        _element = None

        @property
        def blob(self):
            if self._element is None:
                return self._blob_bytes
            return etree.tostring(self._element, xml_declaration=True,
                                  encoding="UTF-8", standalone=True)

    COMMENTS_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    # 已存在则复用
    for rel in doc.part.rels.values():
        if rel.reltype == COMMENTS_RT:
            return rel.target_part, rel.target_part._element

    comments_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:comments xmlns:w="%s"/>' % W_NS
    )
    part = _CommentsPart(
        PackURI("/word/comments.xml"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        comments_xml.encode("utf-8"),
        doc.part.package,
    )
    part._blob_bytes = comments_xml.encode("utf-8")
    doc.part.relate_to(part, COMMENTS_RT)
    root = etree.fromstring(part._blob_bytes)
    part._element = root
    return part, root


def _make_comment(root, comment_id: int, author: str, text: str):
    """在comments.xml中追加一条批注定义"""
    from lxml import etree
    ns = {"w": W_NS}
    comment = etree.SubElement(root, qn("w:comment"))
    comment.set(qn("w:id"), str(comment_id))
    comment.set(qn("w:author"), author)
    comment.set(qn("w:date"), "2026-01-01T00:00:00Z")
    p = etree.SubElement(comment, qn("w:p"))
    for line in text.split("\n"):
        r = etree.SubElement(p, qn("w:r"))
        t = etree.SubElement(r, qn("w:t"))
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line
        if line != text.split("\n")[-1]:
            # 多行用换行run分隔
            br = etree.SubElement(r, qn("w:br"))
    return comment


def _wrap_paragraph_with_comment(paragraph, comment_id: int):
    """在段落XML中插入commentRangeStart/End与commentReference"""
    from lxml import etree
    pid = paragraph._p
    start = etree.SubElement(pid, qn("w:commentRangeStart"))
    start.set(qn("w:id"), str(comment_id))
    # 将start移动到段落最前（在pPr之后）
    pid.remove(start)
    ppr = pid.find(qn("w:pPr"))
    if ppr is not None:
        ppr.addnext(start)
    else:
        pid.insert(0, start)

    end = etree.SubElement(pid, qn("w:commentRangeEnd"))
    end.set(qn("w:id"), str(comment_id))

    ref_run = etree.SubElement(pid, qn("w:r"))
    ref = etree.SubElement(ref_run, qn("w:commentReference"))
    ref.set(qn("w:id"), str(comment_id))


# ---------- 条款锚点定位 ----------

def _norm(s: str) -> str:
    return "".join(s.split())


def _find_anchor_paragraph(doc: Document, clause) -> object:
    """按clause_id+文本相似度在docx段落中定位条款锚点段落"""
    import difflib
    clauses_text = [
        f"第{clause.clause_id}条 {clause.title}".strip(),
        clause.title or "",
        (clause.content or "")[:60],
    ]
    targets = [_norm(t) for t in clauses_text if _norm(t)]

    paras = [p for p in doc.paragraphs if _norm(p.text)]
    texts = [_norm(p.text) for p in paras]
    best, best_score = None, 0.0
    for i, p in enumerate(paras):
        t = texts[i]
        score = max(difflib.SequenceMatcher(None, t, tg[:80]).ratio()
                    for tg in targets)
        # 前缀快速命中（标题行）
        for tg in targets:
            if tg and (t.startswith(tg[:10]) or tg.startswith(t[:10])):
                score = max(score, 0.9)
        if score > best_score:
            best, best_score = p, score
    return best if best_score >= 0.55 else None


def _finding_text(f) -> str:
    """将finding格式化为批注文本（风险等级、规则名、法条依据、修改建议）"""
    lines = [f"【{f.risk_level}风险｜{f.dimension}】{f.rule_name}"]
    if f.legal_basis:
        lines.append(f"法条依据：{f.legal_basis}")
    if f.suggestion:
        lines.append(f"修改建议：{f.suggestion}")
    return "\n".join(lines)


def _append_unanchored_list(doc: Document, findings):
    """降级方案：无法定位锚点的发现，追加到文档末尾清单"""
    doc.add_paragraph("—— 以下风险未能定位到原文条款，请人工核对 ——")
    for f in findings:
        p = doc.add_paragraph()
        run = p.add_run(_finding_text(f))
        run.font.size = Pt(10)
        if f.risk_level == "高":
            run.font.color.rgb = __import__("docx.shared", fromlist=["RGBColor"]).RGBColor(0xC0, 0, 0)
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT


# ---------- 对外主入口 ----------

def generate_annotated(contract: Contract, out_path: Path,
                       source_path: Path = None, author: str = "ClauseGuard") -> Path:
    """生成带批注版docx：把审查发现以OOXML批注写入原文条款位置。

    - 优先复用上传的原始docx（source_path），否则用空文档+降级清单
    - 锚点定位失败的finding统一附加到文档末尾清单
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    findings = list(contract.review.findings) if contract.review else []
    if not findings:
        raise ValueError("该合同尚未审查或无审查发现，无法生成批注版")

    if source_path and Path(source_path).exists() and Path(source_path).suffix.lower() == ".docx":
        doc = Document(str(source_path))
    else:
        doc = Document()
        doc.add_heading(contract.filename or "合同原文", level=1)
        for line in (contract.raw_text or "").splitlines():
            if line.strip():
                doc.add_paragraph(line.strip())

    # clause_id -> Clause索引，供锚点定位
    clause_map = {c.clause_id: c for c in contract.clauses}

    comments_part, comments_root = _ensure_comments_part(doc)
    anchored, unanchored = [], []
    cid = 1
    for f in findings:
        clause = clause_map.get(f.clause_id or "")
        anchor = _find_anchor_paragraph(doc, clause) if clause else None
        if anchor is None and f.excerpt:
            # 退化：用excerpt片段直接匹配段落
            ex = _norm(f.excerpt)[:40]
            if ex:
                for p in doc.paragraphs:
                    if ex and ex in _norm(p.text):
                        anchor = p
                        break
        if anchor is None:
            unanchored.append(f)
            continue
        _make_comment(comments_root, cid, author, _finding_text(f))
        _wrap_paragraph_with_comment(anchor, cid)
        anchored.append(f)
        cid += 1

    if unanchored:
        _append_unanchored_list(doc, unanchored)

    doc.save(str(out_path))
    return out_path


def generate_clean(contract: Contract, out_path: Path,
                   source_path: Path = None) -> Path:
    """生成干净版docx：原文内容，不携带批注（重新打开原docx另存即剥离历史批注）"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path and Path(source_path).exists() and Path(source_path).suffix.lower() == ".docx":
        doc = Document(str(source_path))
    else:
        doc = Document()
        for line in (contract.raw_text or "").splitlines():
            if line.strip():
                doc.add_paragraph(line.strip())
    # 剥离任何已有批注range与引用
    body = doc.element.body
    for tag in ("w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"):
        for el in body.iter(qn(tag)):
            el.getparent().remove(el)
    doc.save(str(out_path))
    return out_path
