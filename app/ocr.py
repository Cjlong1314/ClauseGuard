"""F5 OCR/PDF版式解析模块（PaddleOCR，可选依赖）
扫描件/图片PDF全量可审：文本层缺失时自动走OCR通道，
输出带置信度的识别结果，低置信段落标记需人工复核。
依赖采用延迟导入——未安装paddle时不影响其他格式。
"""
import re
from pathlib import Path

from . import config


class OcrUnavailable(Exception):
    """paddleocr未安装或不可用时抛出，message含中文安装指引"""
    pass


INSTALL_GUIDE = (
    "OCR功能依赖PaddleOCR（可选组件）尚未安装。安装指引（CPU版，Windows/Linux通用）：\n"
    "  1) pip install paddlepaddle==2.6.2 -i https://pypi.tuna.tsinghua.edu.cn/simple\n"
    "  2) pip install paddleocr==2.7.3\n"
    "  GPU版参考 https://www.paddlepaddle.org.cn/install/quick 选择对应CUDA版本\n"
    "  安装后重启服务即可支持扫描件/图片PDF识别。"
)

_engine = None


def is_available() -> bool:
    """延迟检测paddleocr是否可导入（不初始化引擎，开销小）"""
    if not config.OCR_ENABLED:
        return False
    try:
        import paddleocr  # noqa: F401
        return True
    except Exception:
        return False


def _get_engine():
    """单例初始化PaddleOCR引擎（首次调用时下载/加载模型，耗时较长）"""
    global _engine
    if _engine is not None:
        return _engine
    if not config.OCR_ENABLED:
        raise OcrUnavailable("OCR功能已在配置中关闭（OCR_ENABLED=0）")
    try:
        from paddleocr import PaddleOCR
    except Exception as e:
        raise OcrUnavailable(INSTALL_GUIDE + f"\n（导入失败原因：{e}）")
    # 兼容paddleocr 2.x/3.x：3.x参数名变更且不支持show_log
    try:
        _engine = PaddleOCR(
            lang=config.OCR_LANG,
            ocr_version="PP-OCRv4",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            enable_mkldnn=False,  # paddle3.3+onednn执行器存在PIR属性转换bug，禁用mkldnn
        )
    except (TypeError, ValueError):
        # 2.x降级：use_angle_cls+无额外参数
        _engine = PaddleOCR(use_angle_cls=True, lang=config.OCR_LANG)
    return _engine


def pdf_text_layer_chars(path: Path) -> int:
    """快速统计PDF文本层字符总数（不渲染，开销小）"""
    import pdfplumber
    total = 0
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            total += len(t.strip())
            # 命中阈值即提前返回，避免大文件全量扫描
            if total >= config.OCR_SCAN_MIN_CHARS:
                break
    return total


def is_scanned_pdf(path: Path) -> bool:
    """判定是否扫描件/图片PDF（文本层字符数低于阈值）"""
    try:
        return pdf_text_layer_chars(path) < config.OCR_SCAN_MIN_CHARS
    except Exception:
        # 打不开文本层（损坏或纯图片）也按扫描件处理
        return True


def _render_pdf_pages(path: Path):
    """将PDF逐页渲染为PIL图片（pdfplumber内置pdfminer+Pillow，零新增依赖）"""
    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            im = page.to_image(resolution=config.OCR_DPI)
            yield im.original  # PIL.Image


def _detect_sign_areas(text: str) -> list:
    """签署栏区域识别：按签署相关关键词所在行返回上下文片段"""
    areas = []
    parties = ("甲方", "乙方", "丙方", "委托方", "受托方", "承包人", "发包人")
    signs = ("签字", "签章", "盖章", "签署", "签 字", "盖 章", "签订日期", "签署日期")
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(p in s for p in signs) or any(p in s and len(s) <= 30 for p in parties):
            areas.append(s[:60])
    return areas[:10]


def _ocr_image(engine, pil_image) -> dict:
    """对单张PIL图片执行OCR，返回{text,segments:[{text,conf}],avg_conf}
    兼容paddleocr 2.x(engine.ocr)与3.x(engine.predict)返回结构
    """
    import numpy as np
    segments = []
    arr = np.array(pil_image)
    if hasattr(engine, "predict"):
        results = engine.predict(arr)
    else:
        results = engine.ocr(arr, cls=True)
    for res in results or []:
        if res is None:
            continue
        if isinstance(res, dict) or hasattr(res, "get"):
            # paddleocr 3.x：predict结果dict，rec_texts/rec_scores
            texts = res.get("rec_texts") or []
            scores = res.get("rec_scores") or []
            for t, c in zip(texts, scores):
                segments.append({"text": str(t), "conf": round(float(c), 4)})
        else:
            # 2.x：[box, (text, conf)]列表
            for item in res:
                if not item:
                    continue
                segments.append({"text": str(item[1][0]), "conf": round(float(item[1][1]), 4)})
    return {
        "text": "\n".join(s["text"] for s in segments),
        "segments": segments,
        "avg_conf": round(sum(s["conf"] for s in segments) / len(segments), 4) if segments else 0.0,
    }


def _ppstructure_tables(engine_pp, pil_image) -> list:
    """PP-Structure表格还原（可选：仅当ppstructure可导入时启用）"""
    try:
        from paddleocr import PPStructure  # noqa: F401
        table_engine = PPStructure(show_log=False)
        import numpy as np
        result = table_engine(np.array(pil_image))
        tables = []
        for region in result or []:
            if region.get("type") == "table" and region.get("res"):
                html = region["res"].get("html", "")
                if html:
                    tables.append({"html": html, "bbox": region.get("bbox", [])})
        return tables
    except Exception:
        # 表格还原组件不可用时降级：返回空列表，不影响文本OCR主流程
        return []


def ocr_file(path: Path, with_tables: bool = False) -> dict:
    """统一OCR入口：PDF逐页渲染识别 / 图片直接识别
    返回 {source, pages:[{page_no,text,avg_conf,low_conf,tables}], text, sign_areas,
          need_review, low_conf_pages}
    标题层级/条款树由parser.split_clauses基于OCR文本还原，此处不做重复切分。
    """
    engine = _get_engine()
    ext = path.suffix.lower()
    pages = []
    if ext == ".pdf":
        units = [(i + 1, im) for i, im in enumerate(_render_pdf_pages(path))]
    elif ext in (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"):
        from PIL import Image
        units = [(1, Image.open(str(path)))]
    else:
        raise OcrUnavailable(f"OCR不支持的文件类型: {ext}")

    for page_no, im in units:
        r = _ocr_image(engine, im)
        tables = _ppstructure_tables(_get_engine(), im) if with_tables else []
        low = r["avg_conf"] < config.OCR_MIN_CONF
        pages.append({
            "page_no": page_no,
            "text": r["text"],
            "avg_conf": r["avg_conf"],
            "low_conf": low,
            "tables": tables,
        })

    full_text = "\n".join(p["text"] for p in pages)
    low_conf_pages = [p["page_no"] for p in pages if p["low_conf"]]
    return {
        "source": "ocr",
        "lang": config.OCR_LANG,
        "pages": pages,
        "text": full_text,
        "sign_areas": _detect_sign_areas(full_text),
        "need_review": bool(low_conf_pages),
        "low_conf_pages": low_conf_pages,
    }


def ocr_file_simple(path: Path) -> dict:
    """parser集成的简化入口：仅返回{text, need_review, low_conf_pages, avg_conf}"""
    r = ocr_file(path)
    confs = [p["avg_conf"] for p in r["pages"] if p["avg_conf"]]
    return {
        "text": r["text"],
        "need_review": r["need_review"],
        "low_conf_pages": r["low_conf_pages"],
        "avg_conf": round(sum(confs) / len(confs), 4) if confs else 0.0,
        "sign_areas": r["sign_areas"],
    }
