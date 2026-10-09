"""合同要素抽取（正则规则版）
抽取合同类型、主体、标的金额、履行期限、签署日期
F6升级：新增LLM结构化抽取通道（JSON Schema约束+失败重试），
正则结果与LLM结果交叉校验，冲突项标记"需人工确认"；LLM未配置自动降级为纯正则
"""
import json
import re
from .models import ExtractedInfo, Party, ExtractionConflict


def _norm(s) -> str:
    """归一化用于比较：去空白、全角转半角、大写、去尾部标点"""
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    table = str.maketrans("，。；：（）ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９",
                          "，。；：（）ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
    return s.translate(table).replace(" ", "").replace("\u3000", "").rstrip("。；;，,").upper()

TYPE_KEYWORDS = [
    ("买卖合同", ["买卖", "购销", "销售", "采购"]),
    ("租赁合同", ["租赁", "出租", "承租"]),
    ("服务合同", ["服务", "委托", "外包", "咨询"]),
    ("借款合同", ["借款", "借贷", "贷款"]),
    ("劳动合同", ["劳动合同", "聘用", "雇佣"]),
    ("保密协议", ["保密"]),
    ("知识产权合同", ["知识产权", "许可", "著作权", "专利"]),
]

AMOUNT_RE = re.compile(r"[（(]?(?:大写[:：]?\s*)?[人民币RMB]*\s*([壹贰叁肆伍陆柒捌玖拾佰仟万亿元角分]+元[整正]?)")
AMOUNT_NUM_RE = re.compile(r"([¥￥$]?\s*\d[\d,，]*(?:\.\d+)?)\s*(?:元|万元)")

PARTY_RE = re.compile(r"(甲方|乙方|丙方|发包方|承包方|出租方|承租方)[:：]?\s*([^\n，。;；]{2,40})")
CREDIT_RE = re.compile(r"统一社会信用代码[:：]?\s*([0-9A-Z]{18})")

TERM_RE = re.compile(r"(?:履行期限|合同期限|期限|工期|租赁期)[:：]?\s*([^\n，。;；]{2,40})")
DATE_RE = re.compile(r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)")


def detect_type(text: str) -> str:
    counts = []
    for name, kws in TYPE_KEYWORDS:
        c = sum(text.count(k) for k in kws)
        if c:
            counts.append((c, name))
    counts.sort(reverse=True)
    return counts[0][1] if counts else "未知"


def extract(text: str) -> ExtractedInfo:
    parties = []
    for role, name in PARTY_RE.findall(text):
        if not any(p.role == role for p in parties):
            parties.append(Party(role=role, name=name.strip()))
    credits = CREDIT_RE.findall(text)
    for i, p in enumerate(parties):
        if i < len(credits):
            p.identifier = credits[i]

    amount = ""
    m = AMOUNT_NUM_RE.search(text)
    if m:
        amount = m.group(0).strip()
    m2 = AMOUNT_RE.search(text)
    if m2:
        amount = f"{amount}（{m2.group(1)}）" if amount else m2.group(1)

    term_m = TERM_RE.search(text)
    date_m = DATE_RE.search(text)
    return ExtractedInfo(
        contract_type=detect_type(text),
        parties=parties,
        amount=amount,
        term=term_m.group(1).strip() if term_m else "",
        sign_date=date_m.group(1).replace(" ", "") if date_m else "",
    )


# ---------- F6：LLM结构化抽取通道 ----------

# JSON Schema约束LLM输出（OpenAI兼容接口以提示词方式约束，兼顾本地小参数模型）
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "contract_type": {"type": "string",
                          "enum": ["买卖合同", "租赁合同", "服务合同", "借款合同",
                                   "劳动合同", "保密协议", "知识产权合同", "其他", "未知"]},
        "parties": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "identifier": {"type": "string",
                               "description": "统一社会信用代码，18位，没有则为空字符串"},
            },
            "required": ["role", "name"],
        }},
        "amount": {"type": "string"},
        "term": {"type": "string"},
        "sign_date": {"type": "string"},
        "jurisdiction": {"type": "string"},
        "dispute_resolution": {"type": "string", "enum": ["诉讼", "仲裁", "", "未知"]},
    },
    "required": ["contract_type", "parties", "amount", "term", "sign_date",
                 "jurisdiction", "dispute_resolution"],
}

_LLM_SYSTEM = (
    "你是合同要素抽取助手。从合同文本中抽取结构化要素，"
    "严格按以下JSON Schema输出一个JSON对象，不要输出任何其他文字：\n"
    + json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False)
    + "\n要求：只依据合同原文，不得编造；找不到的字段填空字符串；"
      "parties的identifier填统一社会信用代码；dispute_resolution只能取诉讼/仲裁/未知/空。"
)

_LLM_RETRIES = 2  # 失败重试次数


def _parse_llm_json(reply: str) -> dict:
    """从LLM回复中解析JSON对象（容忍markdown代码块包裹）"""
    reply = reply.strip()
    if reply.startswith("```"):
        # 去掉```json ... ```包裹
        reply = re.sub(r"^```[a-zA-Z]*\s*", "", reply)
        reply = re.sub(r"\s*```$", "", reply).strip()
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回复中未找到JSON对象")
    obj = json.loads(reply[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON不是对象")
    return obj


def _validate_llm(obj: dict) -> dict:
    """按Schema校验并规整LLM输出，不符合抛ValueError"""
    if not isinstance(obj.get("contract_type"), str):
        raise ValueError("contract_type缺失或非字符串")
    allowed = EXTRACTION_SCHEMA["properties"]["contract_type"]["enum"]
    if obj["contract_type"] not in allowed:
        raise ValueError(f"contract_type非法: {obj['contract_type']}")
    parties = obj.get("parties", [])
    if not isinstance(parties, list):
        raise ValueError("parties必须是数组")
    clean_parties = []
    for p in parties:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        clean_parties.append({
            "role": str(p.get("role", "")).strip() or "未知",
            "name": p["name"].strip(),
            "identifier": str(p.get("identifier", "")).strip(),
        })
    dr = obj.get("dispute_resolution", "")
    if dr not in ("诉讼", "仲裁", "未知", ""):
        dr = "未知"
    return {
        "contract_type": obj["contract_type"],
        "parties": clean_parties,
        "amount": str(obj.get("amount", "")).strip(),
        "term": str(obj.get("term", "")).strip(),
        "sign_date": str(obj.get("sign_date", "")).strip(),
        "jurisdiction": str(obj.get("jurisdiction", "")).strip(),
        "dispute_resolution": dr,
    }


def llm_extract(text: str) -> dict:
    """LLM结构化抽取：JSON Schema约束+失败重试，返回规整后的dict"""
    from . import llm
    if not llm.is_configured():
        raise llm.LLMNotConfigured("LLM未配置")
    # 超长文本截断（抽取要素多在前部与尾部：签署栏在尾部）
    snippet = text if len(text) <= 12000 else text[:9000] + "\n...\n" + text[-2500:]
    last_err = None
    for i in range(_LLM_RETRIES + 1):
        try:
            reply = llm.chat(_LLM_SYSTEM, f"合同文本：\n{snippet}\n请输出JSON。")
            return _validate_llm(_parse_llm_json(reply))
        except (llm.LLError, ValueError) as e:
            last_err = e
    raise llm.LLError(f"LLM抽取重试{_LLM_RETRIES}次仍失败: {last_err}")


_COMPARE_FIELDS = ["contract_type", "amount", "term", "sign_date",
                   "jurisdiction", "dispute_resolution"]


def _cross_check(regex_e: ExtractedInfo, llm_e: ExtractedInfo):
    """交叉校验：正则与LLM结果对比，冲突项标记需人工确认"""
    conflicts = []
    for f in _COMPARE_FIELDS:
        rv, lv = getattr(regex_e, f), getattr(llm_e, f)
        if lv and rv and _norm(rv) != _norm(lv):
            conflicts.append(ExtractionConflict(
                field=f, regex_value=rv, llm_value=lv))
    # 主体按角色对齐比较
    rmap = {_norm(p.role): p for p in regex_e.parties if p.role}
    lmap = {_norm(p.role): p for p in llm_e.parties if p.role}
    for role in sorted(set(rmap) | set(lmap)):
        rp, lp = rmap.get(role), lmap.get(role)
        rname = rp.name if rp else ""
        lname = lp.name if lp else ""
        if rname and lname and _norm(rname) != _norm(lname):
            conflicts.append(ExtractionConflict(
                field=f"parties.{rp.role if rp else lp.role}.name",
                regex_value=rname, llm_value=lname))
        rid = rp.identifier if rp else ""
        lid = lp.identifier if lp else ""
        if rid and lid and _norm(rid) != _norm(lid):
            conflicts.append(ExtractionConflict(
                field=f"parties.{(rp or lp).role}.identifier",
                regex_value=rid, llm_value=lid))
    return conflicts


def extract_with_llm(text: str, contract_id: str = "") -> dict:
    """F6总入口：正则基线+LLM通道+交叉校验。
    返回ExtractionResult可序列化dict；LLM未配置/失败自动降级为纯正则结果。
    """
    from .models import ExtractionResult
    from . import llm as llm_mod
    regex_e = extract(text)
    if not llm_mod.is_configured():
        result = ExtractionResult(
            contract_id=contract_id, extracted=regex_e,
            regex_extracted=regex_e, llm_extracted=None,
            conflicts=[], source="regex", llm_status="disabled")
        return result.model_dump()
    try:
        data = llm_extract(text)
        llm_e = ExtractedInfo(**data)
    except llm_mod.LLMNotConfigured:
        result = ExtractionResult(
            contract_id=contract_id, extracted=regex_e,
            regex_extracted=regex_e, llm_extracted=None,
            conflicts=[], source="regex", llm_status="disabled")
        return result.model_dump()
    except llm_mod.LLError as e:
        result = ExtractionResult(
            contract_id=contract_id, extracted=regex_e,
            regex_extracted=regex_e, llm_extracted=None,
            conflicts=[], source="regex", llm_status="failed",
            llm_error=str(e))
        return result.model_dump()

    # 合并策略：以LLM为主（召回更高），正则补充LLM缺失字段
    merged = llm_e.model_copy(deep=True)
    for f in _COMPARE_FIELDS:
        if not getattr(merged, f):
            setattr(merged, f, getattr(regex_e, f))
    if not merged.parties:
        merged.parties = regex_e.parties
    else:
        # 补齐正则识别到而LLM遗漏的信用代码
        rmap = {_norm(p.role): p for p in regex_e.parties}
        for p in merged.parties:
            if not p.identifier and _norm(p.role) in rmap:
                p.identifier = rmap[_norm(p.role)].identifier

    conflicts = _cross_check(regex_e, llm_e)
    result = ExtractionResult(
        contract_id=contract_id, extracted=merged,
        regex_extracted=regex_e, llm_extracted=llm_e,
        conflicts=conflicts, source="llm_merged", llm_status="ok")
    return result.model_dump()

