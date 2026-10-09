"""文档解析与条款切分模块
支持DOCX/PDF/TXT，自动识别条款编号并还原为条款树
"""
import re
from pathlib import Path

from .config import ALLOWED_EXT, MAX_UPLOAD_SIZE
from . import config


class ParseError(Exception):
    pass


def read_docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    lines = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            lines.append(t)
    # 简单表格文本也纳入
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append("；".join(cells))
    return "\n".join(lines)


def read_pdf(path: Path) -> str:
    import pdfplumber
    lines = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(l for l in text.splitlines() if l.strip())
    return "\n".join(lines)


def read_txt(path: Path) -> str:
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ParseError("无法识别文件编码")


def extract_text(path: Path) -> str:
    """按扩展名提取全文文本（兼容保留：内部走parse_document）"""
    return parse_document(path)["text"]


def parse_document(path: Path) -> dict:
    """F5统一解析入口：DOCX/TXT直读；PDF先检测文本层，
    无文本层（扫描件/图片PDF）自动走OCR通道（延迟导入，未安装时给出中文安装指引）。
    返回 {text, source: text_layer|ocr, ocr_info}
    """
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ParseError(f"不支持的文件类型: {ext}，仅支持 docx/pdf/txt")
    if path.stat().st_size > MAX_UPLOAD_SIZE:
        raise ParseError("文件超过50MB限制")
    if ext == ".docx":
        return {"text": read_docx(path), "source": "text_layer", "ocr_info": None}
    if ext == ".txt":
        return {"text": read_txt(path), "source": "text_layer", "ocr_info": None}
    if ext == ".pdf":
        # 延迟导入ocr模块，避免未安装OCR依赖时影响常规路径
        from . import ocr as ocr_mod
        if not ocr_mod.is_scanned_pdf(path):
            return {"text": read_pdf(path), "source": "text_layer", "ocr_info": None}
        # 扫描件→OCR
        if not config.OCR_ENABLED:
            raise ParseError("该PDF为扫描件/图片PDF，OCR功能已关闭（OCR_ENABLED=0），无法解析")
        try:
            info = ocr_mod.ocr_file_simple(path)
        except ocr_mod.OcrUnavailable as e:
            # 未安装paddle等依赖：转为ParseError，main.py返回400中文提示而非500
            raise ParseError(str(e))
        if not info["text"].strip():
            raise ParseError("OCR未识别到任何文字，请确认扫描件清晰度后重试")
        return {"text": info["text"], "source": "ocr", "ocr_info": info}
    if ext in (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"):
        # F5：图片型合同→OCR通道（与扫描件PDF同口径；延迟导入保证无OCR依赖时其他格式不受影响）
        from . import ocr as ocr_mod
        if not config.OCR_ENABLED:
            raise ParseError("图片型合同需要OCR功能，但OCR已关闭（OCR_ENABLED=0），无法解析")
        try:
            info = ocr_mod.ocr_file_simple(path)
        except ocr_mod.OcrUnavailable as e:
            raise ParseError(str(e))
        if not info["text"].strip():
            raise ParseError("OCR未识别到任何文字，请确认图片清晰度后重试")
        return {"text": info["text"], "source": "ocr", "ocr_info": info}
    raise ParseError(f"不支持的文件类型: {ext}")


# 条款编号识别：第X条 / 3.2 / （三） / 三、
_PATTERNS = [
    (1, re.compile(r"^第([一二三四五六七八九十百]+|\d+)条[、\s]?")),
    (1, re.compile(r"^(\d+(?:\.\d+)*)[、\s]?")),
    (2, re.compile(r"^[（(]([一二三四五六七八九十]|\d+)[）)]")),
    (2, re.compile(r"^([一二三四五六七八九十]+)、")),
]


def _detect_number(line: str):
    for level, pat in _PATTERNS:
        m = pat.match(line)
        if m:
            return m.group(1), level, line[m.end():].strip()
    return None, 0, line


def split_clauses(text: str) -> list:
    """将全文切分为条款列表 [{clause_id,title,content,level}]，保留原始编号"""
    clauses = []
    cur = None
    buf = []

    def flush():
        nonlocal cur, buf
        if cur is not None:
            cur["content"] = "\n".join(buf).strip()
            clauses.append(cur)
        buf = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        num, level, rest = _detect_number(line)
        if num:
            flush()
            cur = {
                "clause_id": num,
                "title": rest,
                "content": "",
                "level": level,
            }
            # 编号行剩余正文也要计入条款内容
            if rest:
                buf.append(rest)
        else:
            if cur is None:
                # 序言部分
                cur = {"clause_id": "0", "title": "序言", "content": "", "level": 0}
            buf.append(line)
    flush()
    # 至少给出一条
    if not clauses:
        clauses = [{"clause_id": "1", "title": "全文", "content": text.strip(), "level": 1}]
    return clauses
