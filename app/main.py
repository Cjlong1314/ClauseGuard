"""ClauseGuard FastAPI 入口"""
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import config, extractor, parser, reviewer
from . import settings as settings_mod
from . import auth as auth_mod
from . import audit as audit_mod
from . import kb as kb_mod
from . import compare as compare_mod
from .models import Contract, Clause, ExtractedInfo, ReviewResult, ExtractionResult, ExtractionCorrection

app = FastAPI(title="ClauseGuard 合同智能审查系统", version="1.0.0")

# 静态资源（小灵通头像等）
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ---------- 认证辅助 ----------

def _get_user(request: Request, perm: str = None):
    """从请求头X-Token解析当前用户，可校验权限"""
    token = request.headers.get("X-Token", "")
    user = auth_mod.current_user(token)
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    if perm and not auth_mod.has_perm(user["role"], perm):
        raise HTTPException(403, "当前角色无此权限")
    return user

from . import db as db_mod

# PostgreSQL存储（合同主数据持久化，重启不丢失）
db_mod.init_db()

# 加载持久化的系统设置（LLM/Embedding在线配置），不重启即生效
settings_mod.load()

# ---------- F7：集成适配层（IntegrationHub） ----------
from .integrations import base as connectors_mod
from .integrations import queue as outbox_mod
from .integrations.routes import router as integrations_router

app.include_router(integrations_router)      # /api/integrations/*回调与健康状态
connectors_mod.load_connectors()             # 按INTEGRATIONS_ENABLED加载Connector
outbox_mod.start_worker()                    # 失败重试队列后台线程

# 健康检查探针（/health：db/llm/embedding三探针，降级可报）
from . import health as health_mod
app.include_router(health_mod.router)


def _get_contract(cid: str):
    """从数据库加载合同对象，不存在返回None"""
    row = db_mod.get_contract_row(cid)
    if not row:
        return None
    return Contract(
        contract_id=row["contract_id"], filename=row["filename"],
        upload_time=row["upload_time"], raw_text=row["raw_text"] or "",
        clauses=[Clause(**c) for c in (row["clauses"] or [])],
        extracted=ExtractedInfo(**row["extracted"]) if row.get("extracted") else None,
        review=ReviewResult(**row["review"]) if row.get("review") else None,
    )


def _all_contracts() -> dict:
    """加载全部合同（供统计），{cid: Contract}"""
    out = {}
    for row in db_mod.list_contract_rows():
        out[row["contract_id"]] = _get_contract(row["contract_id"])
    return out


def _save_contract_meta(cid: str):
    pass  # 预留持久化扩展点


@app.get("/", response_class=HTMLResponse)
def index():
    """主页；禁止缓存，避免浏览器使用旧版HTML导致登录态判断异常"""
    tpl = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=tpl.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.post("/api/contracts/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    _get_user(request, "upload")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in config.ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型: {suffix}")
    cid = uuid.uuid4().hex[:12]
    dest = config.UPLOAD_DIR / f"{cid}_{file.filename}"
    content = await file.read()
    if len(content) > config.MAX_UPLOAD_SIZE:
        raise HTTPException(400, "文件超过50MB限制")
    dest.write_bytes(content)

    # F5：扫描件判定与OCR异步通道
    needs_ocr = False
    ocr_ok = True
    if suffix == ".pdf":
        from . import ocr as ocr_mod
        try:
            needs_ocr = ocr_mod.is_scanned_pdf(dest)
        except Exception:
            needs_ocr = True
        ocr_ok = ocr_mod.is_available()
        if needs_ocr and not ocr_ok:
            raise HTTPException(400, ocr_mod.INSTALL_GUIDE)

    if needs_ocr and config.OCR_ASYNC and ocr_ok:
        # 异步解析：先落"解析中"占位合同，daemon线程完成后覆盖
        placeholder = Contract(
            contract_id=cid,
            filename=file.filename,
            upload_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            raw_text="",
            clauses=[],
            extracted=None,
        )
        db_mod.save_contract(placeholder.model_dump())
        audit_mod.log(_get_user(request)["username"], "upload_ocr_async", cid,
                      f"上传扫描件{file.filename}，后台OCR解析中")
        threading.Thread(target=_ocr_background_parse, args=(cid, dest, file.filename),
                         daemon=True).start()
        return {"contract_id": cid, "clause_count": 0, "extracted": None,
                "parse_status": "processing", "source": "ocr",
                "status_url": f"/api/contracts/{cid}/parse-status"}

    try:
        doc = parser.parse_document(dest)
        text = doc["text"]
        clauses = parser.split_clauses(text)
        extracted = extractor.extract(text)
    except parser.ParseError as e:
        raise HTTPException(400, str(e))

    contract = Contract(
        contract_id=cid,
        filename=file.filename,
        upload_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        raw_text=text,
        clauses=[Clause(**c) for c in clauses],
        extracted=extracted,
    )
    db_mod.save_contract(contract.model_dump())
    audit_mod.log(_get_user(request)["username"], "upload", cid, f"上传合同{file.filename}")
    resp = {"contract_id": cid, "clause_count": len(contract.clauses),
            "extracted": extracted.model_dump(), "source": doc["source"]}
    if doc["source"] == "ocr" and doc.get("ocr_info"):
        oi = doc["ocr_info"]
        resp["ocr"] = {"avg_conf": oi.get("avg_conf"), "need_review": oi.get("need_review"),
                       "low_conf_pages": oi.get("low_conf_pages"),
                       "sign_areas": oi.get("sign_areas", [])}
    return resp


# ---------- F5 OCR异步解析（daemon线程，零新增依赖） ----------
_parse_jobs = {}  # cid -> {status: processing|done|failed, error, clause_count}


def _ocr_background_parse(cid: str, dest: Path, filename: str):
    """后台线程执行OCR解析并落库（参考F8 daemon线程模式）"""
    _parse_jobs[cid] = {"status": "processing", "error": "", "clause_count": 0}
    try:
        from . import ocr as ocr_mod
        doc = ocr_mod.ocr_file_simple(dest)
        text = doc["text"]
        clauses = parser.split_clauses(text)
        extracted = extractor.extract(text)
        contract = Contract(
            contract_id=cid,
            filename=filename,
            upload_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            raw_text=text,
            clauses=[Clause(**c) for c in clauses],
            extracted=extracted,
        )
        db_mod.save_contract(contract.model_dump())
        audit_mod.log("system", "ocr_parse_done", cid,
                      f"扫描件OCR解析完成，{len(clauses)}条款，"
                      f"平均置信度{doc.get('avg_conf', 0)}，"
                      f"需复核页{doc.get('low_conf_pages', [])}")
        _parse_jobs[cid] = {"status": "done", "error": "",
                            "clause_count": len(clauses),
                            "ocr": {k: doc.get(k) for k in
                                    ("avg_conf", "need_review", "low_conf_pages", "sign_areas")}}
    except Exception as e:
        _parse_jobs[cid] = {"status": "failed", "error": str(e), "clause_count": 0}
        try:
            audit_mod.log("system", "ocr_parse_failed", cid, f"扫描件OCR解析失败：{e}")
        except Exception:
            pass


@app.get("/api/contracts/{cid}/parse-status")
def parse_status(cid: str, request: Request):
    """F5：查询扫描件OCR解析状态（processing/done/failed/not_ocr）"""
    _get_user(request)
    job = _parse_jobs.get(cid)
    if job:
        return {"contract_id": cid, "parse_status": job["status"],
                "error": job.get("error", ""), "clause_count": job.get("clause_count", 0),
                "ocr": job.get("ocr")}
    row = db_mod.get_contract_row(cid)
    if not row:
        raise HTTPException(404, "合同不存在")
    # 非扫描件或服务重启前的已完成合同：按raw_text判别
    if row.get("raw_text"):
        return {"contract_id": cid, "parse_status": "done", "error": "",
                "clause_count": len(row.get("clauses") or [])}
    return {"contract_id": cid, "parse_status": "processing", "error": "", "clause_count": 0}


@app.get("/api/contracts")
def list_contracts(request: Request):
    _get_user(request)
    return [{"contract_id": c.contract_id, "filename": c.filename,
             "upload_time": c.upload_time,
             "reviewed": c.review is not None}
            for c in _all_contracts().values()]


@app.get("/api/contracts/{cid}")
def get_contract(cid: str, request: Request):
    _get_user(request)
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    return c.model_dump(exclude={"raw_text"})


@app.post("/api/contracts/{cid}/review")
def review_contract(cid: str, request: Request):
    _get_user(request, "review")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    findings = reviewer.review(c.raw_text, [cl.model_dump() for cl in c.clauses], c.extracted)
    result = ReviewResult(
        contract_id=cid,
        reviewed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        findings=findings,
        summary=reviewer.build_summary(findings),
    )
    db_mod.update_contract_review(cid, result.model_dump())
    audit_mod.log(_get_user(request)["username"], "review", cid, f"执行审查，发现{len(findings)}项")
    # F7：发布审查完成事件（异步推送外部系统，失败落重试队列）
    from .integrations import hub
    hub.publish("review.completed",
                {"contract_id": cid, "contract_filename": c.filename,
                 "finding_count": len(findings), "summary": result.summary,
                 "notify_users": [_get_user(request)["username"]]},
                actor=_get_user(request)["username"], contract_id=cid)
    return result.model_dump()


# ---------- F6：LLM要素抽取（正则+LLM交叉校验+人工修正） ----------


@app.get("/api/contracts/{cid}/extraction")
def get_extraction(cid: str, request: Request):
    """查询合同要素抽取结果；无缓存时现场执行抽取（LLM未配置自动降级纯正则）"""
    _get_user(request)
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    row = db_mod.get_extraction(cid)
    if row:
        return {"from_cache": True, **row}
    result = extractor.extract_with_llm(c.raw_text, cid)
    result["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_mod.upsert_extraction(result)
    audit_mod.log(_get_user(request)["username"], "extraction", cid,
                  f"要素抽取完成，通道{result['llm_status']}，冲突{len(result['conflicts'])}项")
    return {"from_cache": False, **result}


@app.post("/api/contracts/{cid}/extraction")
def run_extraction(cid: str, request: Request):
    """强制重新执行要素抽取（覆盖缓存结果），body可选{force:bool}"""
    _get_user(request, "review")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    result = extractor.extract_with_llm(c.raw_text, cid)
    result["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_mod.upsert_extraction(result)
    audit_mod.log(_get_user(request)["username"], "extraction", cid,
                  f"重新执行要素抽取，通道{result['llm_status']}，冲突{len(result['conflicts'])}项")
    return {"from_cache": False, **result}


@app.post("/api/contracts/{cid}/extraction/correct")
def correct_extraction(cid: str, request: Request, data: dict):
    """人工修正抽取结果：body={corrected:{...要素}, comment:str}
    修正记录落extraction_corrections表，并同步更新最终结果，供评测与提示词迭代
    """
    user = _get_user(request, "review")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    row = db_mod.get_extraction(cid)
    origin = row["extracted"] if row else (c.extracted.model_dump() if c.extracted else None)
    corrected_raw = (data or {}).get("corrected")
    if not isinstance(corrected_raw, dict):
        raise HTTPException(400, "缺少corrected要素对象")
    try:
        corrected = ExtractedInfo(**corrected_raw)
    except Exception as e:
        raise HTTPException(400, f"corrected格式非法: {e}")
    record = ExtractionCorrection(
        contract_id=cid, corrected=corrected,
        origin=ExtractedInfo(**origin) if origin else None,
        operator=user["username"], comment=(data or {}).get("comment", ""),
        time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    db_mod.save_extraction_correction(record.model_dump())
    # 同步合同主表的extracted字段，保持下游审查引用一致
    db_mod.save_contract({
        "contract_id": c.contract_id, "filename": c.filename,
        "upload_time": c.upload_time, "raw_text": c.raw_text,
        "clauses": [cl.model_dump() for cl in c.clauses],
        "extracted": corrected.model_dump(),
        "review": c.review.model_dump() if c.review else None,
    })
    audit_mod.log(user["username"], "extraction_correct", cid,
                  f"人工修正要素抽取结果：{data.get('comment', '')}")
    return {"ok": True, "corrected": corrected.model_dump(),
            "correction_id": None}


@app.get("/api/contracts/{cid}/extraction/corrections")
def list_extraction_corrections(cid: str, request: Request):
    """查看某合同的人工修正历史"""
    _get_user(request)
    if not db_mod.contract_exists(cid):
        raise HTTPException(404, "合同不存在")
    with db_mod.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, contract_id, corrected, origin, operator, comment, time
                   FROM extraction_corrections WHERE contract_id=%s ORDER BY id DESC""",
                (cid,))
            rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "contract_id": r[1],
            "corrected": r[2] if isinstance(r[2], dict) else json.loads(r[2] or "{}"),
            "origin": (r[3] if isinstance(r[3], dict) else json.loads(r[3])) if r[3] else None,
            "operator": r[4], "comment": r[5], "time": r[6],
        })
    return out


@app.get("/api/contracts/{cid}/report")
def export_report(cid: str, request: Request):
    _get_user(request, "report")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    from .report import generate_report
    out = config.REPORT_DIR / f"审查报告_{cid}.docx"
    generate_report(c, out)
    audit_mod.log(_get_user(request)["username"], "export_report", cid, f"导出审查报告{out.name}")
    return FileResponse(str(out), filename=f"合同审查报告_{c.filename}.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


# ---------- F2：Agent审查流水线 ----------

from . import pipeline as pipeline_mod


@app.post("/api/contracts/{cid}/review/pipeline")
def review_pipeline(cid: str, request: Request):
    """Agent流水线审查：意图识别→召回→LLM研判（置信度）→交叉验证→发现+法条原文+置信度

    LLM未配置时自动降级为规则引擎基线（pipeline_mode=rule_baseline）。
    原POST /review保持不变，两者互不影响。
    """
    user = _get_user(request, "review")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    out = pipeline_mod.run_pipeline(
        c.raw_text, [cl.model_dump() for cl in c.clauses], c.extracted, cid)
    result = ReviewResult(**out["result"])
    db_mod.update_contract_review(cid, result.model_dump())
    ext = out["audit_ext"]
    mode = ext.get("trace") and result.summary.get("pipeline_mode", "")
    audit_mod.log(user["username"], "review_pipeline", cid,
                  f"流水线审查（{mode}），发现{len(result.findings)}项，"
                  f"溯源丢弃{ext.get('dropped_unsourced', 0)}条",
                  ext=ext)
    return {**result.model_dump(), "audit_ext": ext}


@app.get("/api/rules")
def list_rules(request: Request):
    _get_user(request, "rules_view")
    rules_file = config.RULES_DIR / "rules.json"
    if not rules_file.exists():
        return []
    return json.loads(rules_file.read_text(encoding="utf-8"))


# ---------- 用户认证与管理 ----------

@app.post("/api/auth/register")
def api_register(request: Request, data: dict):
    # 注册无需登录；首位注册用户自动成为系统管理员，其余默认律师助理
    try:
        return auth_mod.register(data.get("username", ""), data.get("password", ""),
                                 data.get("role", "assistant"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/login")
def api_login(data: dict):
    try:
        return auth_mod.login(data.get("username", ""), data.get("password", ""))
    except ValueError as e:
        raise HTTPException(401, str(e))


@app.post("/api/auth/logout")
def api_logout(request: Request):
    auth_mod.logout(request.headers.get("X-Token", ""))
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(request: Request):
    return _get_user(request)


@app.post("/api/auth/change_password")
def api_change_password(request: Request, data: dict):
    """登录态修改本人密码：校验旧密码，成功后需重新登录"""
    user = _get_user(request)
    try:
        auth_mod.change_password(user["username"], data.get("old_password", ""),
                                 data.get("new_password", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(user["username"], "change_password", "", "用户修改密码")
    return {"ok": True, "message": "密码修改成功，请重新登录"}


# ---------- 三期：统计看板 ----------

from . import stats as stats_mod


@app.get("/api/stats")
def api_stats(request: Request):
    _get_user(request)
    return stats_mod.stats_overview(_all_contracts())


@app.get("/api/users")
def api_users(request: Request):
    _get_user(request, "users_manage")
    return auth_mod.list_users()


@app.post("/api/users/{username}/role")
def api_set_role(username: str, request: Request, data: dict):
    _get_user(request, "users_manage")
    try:
        auth_mod.set_role(username, data.get("role", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/users/{username}/reset_password")
def api_reset_password(username: str, request: Request, data: dict):
    _get_user(request, "users_manage")
    try:
        auth_mod.reset_password(username, data.get("password", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/users/{username}")
def api_delete_user(username: str, request: Request):
    me = _get_user(request, "users_manage")
    if me["username"] == username:
        raise HTTPException(400, "不能删除自己")
    try:
        auth_mod.delete_user(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/roles")
def api_roles():
    return [{"key": k, "name": v, "perms": sorted(auth_mod.ROLE_PERMS[k])}
            for k, v in auth_mod.ROLE_NAMES.items()]


# ---------- 二期：大模型RAG研判 ----------

from . import llm as llm_mod
from . import rag as rag_mod


@app.post("/api/contracts/{cid}/ai_review")
def ai_review(cid: str, request: Request):
    """对每个风险finding做RAG召回+LLM研判，附ai_conclusion或recalled_laws"""
    _get_user(request, "review")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    if not c.review:
        raise HTTPException(400, "请先执行基础审查再进行AI研判")

    rag_mod.build_index()
    ai_enabled = llm_mod.is_configured()
    findings = [f.model_dump() for f in c.review.findings]
    for f in findings:
        query = f"{f.get('rule_name', '')} {f.get('dimension', '')}"
        recalled = rag_mod.search(query, top_k=5)
        # 输出给前端的法条不带内部打分
        f["recalled_laws"] = [{k: l[k] for k in
                               ("law_id", "law_name", "article_no", "content", "valid_from")}
                              for l in recalled]
        f["ai_conclusion"] = None
        if ai_enabled:
            clause_text = f.get("excerpt", "")
            result = rag_mod.analyze_with_llm(clause_text, query, recalled)
            f["ai_conclusion"] = result  # LLM失败时为None，不影响主流程

    if not ai_enabled:
        return {"ai_enabled": False,
                "message": "未配置大模型服务，仅完成法条召回",
                "findings": findings}
    return {"ai_enabled": True, "findings": findings}


@app.get("/api/laws/search")
def laws_search(request: Request, q: str = "", top_k: int = 5):
    """法条关键词召回接口"""
    _get_user(request)
    rag_mod.build_index()
    top_k = max(1, min(top_k, 20))
    return {"total": rag_mod.build_index(), "results": rag_mod.search(q, top_k=top_k)}


# ---------- 审查结果处理（二期：确认/驳回/忽略） ----------

_HANDLE_ACTIONS = {"confirm": "confirmed", "reject": "rejected", "ignore": "ignored"}


@app.post("/api/contracts/{cid}/findings/{finding_id}/handle")
def handle_finding(cid: str, finding_id: str, request: Request, data: dict):
    user = _get_user(request, "review")
    c = _get_contract(cid)
    if not c or not c.review:
        raise HTTPException(404, "合同不存在或未审查")
    action = data.get("action", "")
    if action not in _HANDLE_ACTIONS:
        raise HTTPException(400, "action必须是confirm/reject/ignore")
    for f in c.review.findings:
        if f.finding_id == finding_id:
            f.status = _HANDLE_ACTIONS[action]
            f.comment = data.get("comment", "")
            db_mod.update_contract_review(cid, c.review.model_dump())
            audit_mod.log(user["username"], f"finding_{action}", cid,
                          f"审查项{finding_id}处理为{f.status}，意见：{f.comment}")
            return {"ok": True, "finding": f.model_dump()}
    raise HTTPException(404, "审查项不存在")


# ---------- 审计留痕查询 ----------

@app.get("/api/audit")
def api_audit(request: Request, contract_id: str = None, limit: int = 200):
    user = _get_user(request)
    return audit_mod.query(contract_id=contract_id, limit=limit)


# ---------- 知识库管理（二期） ----------

@app.get("/api/kb/laws")
def api_kb_laws(request: Request, q: str = "", valid: str = ""):
    _get_user(request, "rules_view")
    return kb_mod.list_laws(q=q, valid=valid)


@app.post("/api/kb/laws")
def api_kb_create_law(request: Request, data: dict):
    _get_user(request, "rules_edit")
    try:
        law = kb_mod.create_law(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(_get_user(request)["username"], "kb_law_create", law["law_id"],
                  f"新增法条《{law['law_name']}》{law['article_no']}")
    return law


@app.put("/api/kb/laws/{law_id}")
def api_kb_update_law(law_id: str, request: Request, data: dict):
    user = _get_user(request, "rules_edit")
    try:
        law = kb_mod.update_law(law_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(user["username"], "kb_law_update", law_id,
                  f"更新法条《{law['law_name']}》{law['article_no']}，版本升至v{law['version']}")
    return law


@app.delete("/api/kb/laws/{law_id}")
def api_kb_delete_law(law_id: str, request: Request):
    user = _get_user(request, "rules_edit")
    try:
        kb_mod.delete_law(law_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    audit_mod.log(user["username"], "kb_law_delete", law_id, f"删除法条{law_id}")
    return {"ok": True}


@app.get("/api/kb/laws/{law_id}/check")
def api_kb_check_law(law_id: str, request: Request):
    _get_user(request, "rules_view")
    law = kb_mod.get_law(law_id)
    if not law:
        raise HTTPException(404, f"法条不存在: {law_id}")
    return kb_mod.check_effective(law)


@app.get("/api/kb/templates")
def api_kb_templates(request: Request, q: str = ""):
    _get_user(request, "rules_view")
    return kb_mod.list_templates(q=q)


@app.post("/api/kb/templates")
def api_kb_create_template(request: Request, data: dict):
    user = _get_user(request, "rules_edit")
    try:
        tpl = kb_mod.create_template(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(user["username"], "kb_tpl_create", tpl["tpl_id"], f"新增合同模板《{tpl['name']}》")
    return tpl


@app.put("/api/kb/templates/{tpl_id}")
def api_kb_update_template(tpl_id: str, request: Request, data: dict):
    user = _get_user(request, "rules_edit")
    try:
        tpl = kb_mod.update_template(tpl_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(user["username"], "kb_tpl_update", tpl_id, f"更新合同模板《{tpl['name']}》")
    return tpl


@app.delete("/api/kb/templates/{tpl_id}")
def api_kb_delete_template(tpl_id: str, request: Request, data: dict = None):
    user = _get_user(request, "rules_edit")
    try:
        kb_mod.delete_template(tpl_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    audit_mod.log(user["username"], "kb_tpl_delete", tpl_id, f"删除合同模板{tpl_id}")
    return {"ok": True}


# ---------- 三期：协同任务与通知 ----------

from . import collab as collab_mod


@app.get("/api/tasks")
def api_tasks(request: Request):
    user = _get_user(request)
    return collab_mod.list_tasks()


@app.post("/api/tasks")
def api_create_task(request: Request, data: dict):
    user = _get_user(request)
    fname = ""
    c = _get_contract(data.get("contract_id", ""))
    if c:
        fname = c.filename
    try:
        return collab_mod.create_task(user["username"], data, fname)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{task_id}/status")
def api_task_status(task_id: str, request: Request, data: dict):
    user = _get_user(request)
    try:
        return collab_mod.change_status(user["username"], task_id, data.get("status", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{task_id}/remind")
def api_task_remind(task_id: str, request: Request):
    user = _get_user(request)
    try:
        return collab_mod.remind(user["username"], task_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/notifications")
def api_notifications(request: Request, unread: str = ""):
    user = _get_user(request)
    return collab_mod.list_notifications(user["username"], unread_only=(unread == "1"))


@app.post("/api/notifications/{notify_id}/read")
def api_notify_read(notify_id: str, request: Request):
    user = _get_user(request)
    try:
        return collab_mod.mark_read(user["username"], notify_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------- 三期：模板比对 ----------

@app.post("/api/contracts/{cid}/compare")
def compare_contract(cid: str, request: Request, data: dict):
    """合同与标准模板差异比对，body:{tpl_id}"""
    user = _get_user(request)
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    tpl_id = (data or {}).get("tpl_id", "")
    tpl = kb_mod.get_template(tpl_id)
    if not tpl:
        raise HTTPException(404, f"模板不存在: {tpl_id}")
    result = compare_mod.compare_to_template(
        [cl.model_dump() for cl in c.clauses], tpl.get("content", ""))
    audit_mod.log(user["username"], "compare", cid,
                  f"与模板{tpl_id}《{tpl.get('name', '')}》比对，"
                  f"匹配度{result['similarity']}，缺失{len(result['missing'])}项")
    return {"tpl_id": tpl_id, "tpl_name": tpl.get("name", ""), **result}


@app.get("/api/contracts/{cid}/compare/{tpl_id}")
def compare_contract_get(cid: str, tpl_id: str, request: Request):
    """GET方式的模板比对（等价POST实现）"""
    return compare_contract(cid, request, {"tpl_id": tpl_id})


# ---------- 三期：协同流程管理 ----------

@app.post("/api/tasks")
def api_create_task(request: Request, data: dict):
    """创建审查任务{contract_id,title,assignee,reviewer,deadline,priority}"""
    user = _get_user(request)
    try:
        task = collab_mod.create_task(
            contract_id=data.get("contract_id", ""),
            title=data.get("title", ""),
            assignee=data.get("assignee", ""),
            reviewer=data.get("reviewer", ""),
            creator=user["username"],
            deadline=data.get("deadline", ""),
            priority=data.get("priority", "中"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(user["username"], "task_create", task["contract_id"],
                  f"创建审查任务《{task['title']}》(T{task['task_id']})，审查人{task['assignee']}，复核人{task['reviewer']}")
    return task


@app.get("/api/tasks")
def api_list_tasks(request: Request, status: str = "", mine: str = "", contract_id: str = ""):
    """任务列表；mine=1时只返回assignee或reviewer是当前用户的任务"""
    user = _get_user(request)
    try:
        return collab_mod.list_tasks(
            contract_id=contract_id or None,
            status=status or None,
            mine_user=user["username"] if mine == "1" else None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: str, request: Request):
    _get_user(request)
    try:
        return collab_mod.get_task(task_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/tasks/{task_id}/assign")
def api_assign_task(task_id: str, request: Request, data: dict):
    """改派审查人，仅creator或admin"""
    user = _get_user(request)
    try:
        task = collab_mod.get_task(task_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    if user["role"] != "admin" and task["creator"] != user["username"]:
        raise HTTPException(403, "仅任务创建者或系统管理员可改派")
    try:
        updated = collab_mod.assign(task_id, data.get("assignee", ""), user["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(user["username"], "task_assign", task["contract_id"],
                  f"任务T{task_id}审查人改派为{data.get('assignee', '')}")
    return updated


@app.post("/api/tasks/{task_id}/status")
def api_update_task_status(task_id: str, request: Request, data: dict):
    """任务流转：assignee/reviewer/admin可操作，写history与audit"""
    user = _get_user(request)
    try:
        task = collab_mod.get_task(task_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    if user["role"] != "admin" and user["username"] not in (task["assignee"], task["reviewer"]):
        raise HTTPException(403, "仅任务审查人、复核人或系统管理员可流转任务")
    try:
        updated = collab_mod.update_status(task_id, data.get("status", ""),
                                           operator=user["username"], detail=data.get("detail", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(user["username"], "task_status", task["contract_id"],
                  f"任务T{task_id}状态变更为{updated['status']}，{data.get('detail', '')}")
    return updated


@app.post("/api/tasks/remind")
def api_remind_tasks(request: Request):
    """催办：将超期未完成任务标记due_reminded并返回，仅creator或admin"""
    user = _get_user(request)
    if user["role"] != "admin":
        tasks = collab_mod.list_tasks()
        if not any(t["creator"] == user["username"] for t in tasks):
            raise HTTPException(403, "仅任务创建者或系统管理员可催办")
    reminded = collab_mod.remind_due()
    for t in reminded:
        audit_mod.log(user["username"], "task_remind", t["contract_id"],
                      f"任务T{t['task_id']}《{t['title']}》超期催办")
    return {"count": len(reminded), "tasks": reminded}


@app.get("/api/notifications")
def api_notifications(request: Request):
    """当前用户通知：从任务中取assignee/reviewer是当前用户且近期状态变化或超期的任务生成通知"""
    user = _get_user(request)
    me = user["username"]
    tasks = collab_mod.list_tasks(mine_user=me)
    notifications = []
    for t in tasks:
        # 状态变化通知：最近一条history是别人的状态/分配动作且时间在48小时内
        history = t.get("history", [])
        if history:
            last = history[-1]
            if last.get("user") != me and last.get("action") in ("status", "assign", "create", "remind"):
                notifications.append({
                    "type": "task_activity",
                    "task_id": t["task_id"],
                    "title": t["title"],
                    "contract_id": t["contract_id"],
                    "time": last.get("time", ""),
                    "message": f"任务《{t['title']}》：{last.get('user')} {last.get('detail') or last.get('action')}",
                    "overdue": t["due_reminded"],
                })
        # 超期通知
        if t["due_reminded"] and t["status"] not in ("completed", "cancelled"):
            notifications.append({
                "type": "task_overdue",
                "task_id": t["task_id"],
                "title": t["title"],
                "contract_id": t["contract_id"],
                "time": t["updated_at"],
                "message": f"任务《{t['title']}》已超期（截止{t['deadline'][:10] if t['deadline'] else '未设置'}），请尽快处理",
                "overdue": True,
            })
    notifications.sort(key=lambda n: n["time"], reverse=True)
    return notifications


# ---------- 系统设置（真实embedding服务与LLM在线配置，仅系统管理员） ----------

@app.get("/api/settings")
def api_settings_get(request: Request):
    """查询当前生效的系统设置；api_key掩码回显不泄露明文"""
    _get_user(request, "users_manage")
    return settings_mod.get_all()


@app.put("/api/settings")
def api_settings_put(request: Request, data: dict):
    """保存系统设置并热更新（无需重启）。api_key传"******"表示保留原值"""
    user = _get_user(request, "users_manage")
    saved = settings_mod.update(data)
    audit_mod.log(user["username"], "settings_update", "",
                  "更新系统设置：" + "、".join(sorted((data or {}).keys())))
    return {"ok": True, "settings": saved}


@app.post("/api/settings/test")
def api_settings_test(request: Request, data: dict = None):
    """连通性测试：分别对LLM与Embedding发起真实最小请求，返回耗时与结果"""
    _get_user(request, "users_manage")
    import time
    from . import llm as llm_mod
    from . import embedding as emb_mod

    target = ((data or {}).get("target") or "all").lower()
    out = {}

    def _llm_test():
        t0 = time.time()
        try:
            cfg = llm_mod.get_config()
            reply = llm_mod.chat("你是连通性测试助手", "请只回复：ok", timeout=15)
            out["llm"] = {"ok": True, "runtime": cfg["runtime"], "model": cfg["model"],
                          "reply": (reply or "").strip()[:50],
                          "elapsed_ms": int((time.time() - t0) * 1000)}
        except llm_mod.LLMNotConfigured:
            out["llm"] = {"ok": False, "error": "LLM未配置（base_url/model为空）"}
        except Exception as e:
            out["llm"] = {"ok": False, "error": str(e)[:200]}

    def _emb_test():
        t0 = time.time()
        if not emb_mod.is_configured():
            out["embedding"] = {"ok": False, "error": "Embedding未配置（base_url/model为空）"}
            return
        try:
            vecs = emb_mod.embed(["连通性测试"], timeout=15)
            if vecs is None:
                out["embedding"] = {"ok": False, "error": "调用失败，详见服务端日志"}
            else:
                out["embedding"] = {"ok": True, "dim": len(vecs[0]),
                                    "elapsed_ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            out["embedding"] = {"ok": False, "error": str(e)[:200]}

    if target in ("all", "llm"):
        _llm_test()
    if target in ("all", "embedding"):
        _emb_test()
    return out


# ---------- F1：批注式审查报告回写Word + 条款级版本比对 ----------

from . import annotator as annotator_mod
from . import diff as diff_mod


def _find_source_docx(c):
    """在uploads目录定位原始docx文件，供批注回写复用原文档"""
    if not c:
        return None
    # 上传时保存为 {cid}_{filename}
    candidate = config.UPLOAD_DIR / f"{c.contract_id}_{c.filename}"
    if candidate.exists():
        return candidate
    # 兜底：按文件名匹配
    for p in config.UPLOAD_DIR.glob(f"{c.contract_id}_*"):
        return p
    return None


@app.get("/api/contracts/{cid}/report/annotated")
def export_annotated_report(cid: str, request: Request, mode: str = "annotated"):
    """导出批注式审查报告docx；mode=annotated带批注版 / clean干净版"""
    _get_user(request, "report")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, "合同不存在")
    if not c.review or not c.review.findings:
        raise HTTPException(400, "请先执行审查且存在审查发现")
    source = _find_source_docx(c)
    try:
        if mode == "clean":
            out = config.REPORT_DIR / f"干净版_{cid}.docx"
            annotator_mod.generate_clean(c, out, source)
            download_name = f"干净版_{c.filename}"
        else:
            out = config.REPORT_DIR / f"带批注版_{cid}.docx"
            annotator_mod.generate_annotated(c, out, source)
            download_name = f"带批注版_{c.filename}"
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(_get_user(request)["username"], "export_annotated", cid,
                  f"导出{'干净版' if mode == 'clean' else '带批注版'}文档{out.name}")
    return FileResponse(str(out), filename=download_name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.post("/api/contracts/{cid}/diff")
def api_diff_contract(cid: str, request: Request, data: dict):
    """条款级版本比对：body:{target_id} 将cid(旧版)与target_id(新版)比对"""
    user = _get_user(request)
    old_c = _get_contract(cid)
    if not old_c:
        raise HTTPException(404, f"合同不存在: {cid}")
    target_id = (data or {}).get("target_id", "")
    new_c = _get_contract(target_id)
    if not new_c:
        raise HTTPException(404, f"比对目标合同不存在: {target_id}")
    result = diff_mod.diff_clauses(
        [cl.model_dump() for cl in old_c.clauses],
        [cl.model_dump() for cl in new_c.clauses],
    )
    audit_mod.log(user["username"], "diff", cid,
                  f"与合同{target_id}条款级比对：新增{result['summary']['added']}、"
                  f"删除{result['summary']['removed']}、修改{result['summary']['modified']}、"
                  f"高危{result['summary']['high_risk']}")
    return {
        "old_contract": {"contract_id": old_c.contract_id, "filename": old_c.filename},
        "new_contract": {"contract_id": new_c.contract_id, "filename": new_c.filename},
        **result,
    }


@app.get("/diff", response_class=HTMLResponse)
def diff_page(request: Request, old: str = "", new: str = ""):
    """条款级版本比对页面（简单页面，复用templates模式）"""
    tpl = Path(__file__).parent / "templates" / "diff.html"
    return HTMLResponse(content=tpl.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


# ---------- F4 范本基线与企业差异审查 ----------

from . import baseline as baseline_mod


@app.get("/api/baselines")
def api_list_baselines(request: Request, q: str = "", contract_type: str = "", status: str = ""):
    _get_user(request, "rules_view")
    return baseline_mod.list_baselines(q=q, contract_type=contract_type, status=status)


@app.post("/api/baselines")
def api_create_baseline(request: Request, data: dict):
    """创建范本基线：name必填；contract_type/business_line/stance区分多套范本"""
    user = _get_user(request, "rules_edit")
    b = baseline_mod.create_baseline(data or {}, user["username"])
    audit_mod.log(user["username"], "create_baseline", b["baseline_id"], f"创建范本基线{b['name']}")
    return b


@app.get("/api/baselines/{bid}")
def api_get_baseline(bid: str, request: Request):
    _get_user(request, "rules_view")
    b = baseline_mod.get_baseline(bid)
    if not b:
        raise HTTPException(404, f"基线不存在: {bid}")
    return b


@app.put("/api/baselines/{bid}")
def api_update_baseline(bid: str, request: Request, data: dict):
    """更新基线；content变更自动升版本并记录版本历史"""
    user = _get_user(request, "rules_edit")
    try:
        b = baseline_mod.update_baseline(bid, data or {}, user["username"])
    except ValueError as e:
        raise HTTPException(404, str(e))
    audit_mod.log(user["username"], "update_baseline", bid,
                  f"更新基线{b['name']}至版本v{b['version']}")
    return b


@app.delete("/api/baselines/{bid}")
def api_delete_baseline(bid: str, request: Request):
    user = _get_user(request, "rules_edit")
    try:
        baseline_mod.delete_baseline(bid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    audit_mod.log(user["username"], "delete_baseline", bid, f"删除范本基线{bid}")
    return {"ok": True}


@app.get("/api/baselines/{bid}/versions")
def api_baseline_versions(bid: str, request: Request):
    _get_user(request, "rules_view")
    if not baseline_mod.get_baseline(bid):
        raise HTTPException(404, f"基线不存在: {bid}")
    return baseline_mod.list_versions(bid)


@app.get("/api/baselines/{bid}/versions/{version}")
def api_baseline_version(bid: str, version: int, request: Request):
    _get_user(request, "rules_view")
    v = baseline_mod.get_version(bid, version)
    if not v:
        raise HTTPException(404, f"版本不存在: v{version}")
    return v


@app.post("/api/contracts/{cid}/baseline-check")
def api_baseline_check(cid: str, request: Request, data: dict):
    """待审合同对照范本基线做条款级差异审查，输出缺失/偏离/不利条款及修改建议"""
    user = _get_user(request, "review")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, f"合同不存在: {cid}")
    bid = (data or {}).get("baseline_id", "")
    b = baseline_mod.get_baseline(bid)
    if not b:
        raise HTTPException(404, f"基线不存在: {bid}")
    version = (data or {}).get("version")
    if version:
        v = baseline_mod.get_version(bid, int(version))
        if not v:
            raise HTTPException(404, f"基线版本不存在: v{version}")
        b["content"] = v["content"]
        b["version"] = v["version"]
    result = baseline_mod.baseline_diff(
        [cl.model_dump() for cl in c.clauses], b)
    audit_mod.log(user["username"], "baseline_check", cid,
                  f"对照基线{b['name']}v{b['version']}差异审查：缺失{result['summary']['missing']}、"
                  f"偏离{result['summary']['deviation']}、不利{result['summary']['unfavorable']}")
    return {
        "contract": {"contract_id": c.contract_id, "filename": c.filename},
        "baseline": {"baseline_id": b["baseline_id"], "name": b["name"],
                     "contract_type": b["contract_type"], "business_line": b["business_line"],
                     "stance": b["stance"], "version": b["version"]},
        **result,
    }


@app.get("/baseline", response_class=HTMLResponse)
def baseline_page(request: Request):
    """范本基线管理与差异审查页面"""
    tpl = Path(__file__).parent / "templates" / "baseline.html"
    return HTMLResponse(content=tpl.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


# ---------- F8 履约期义务提醒 ----------

from . import obligations as obligations_mod

obligations_mod.start_scheduler()  # 进程内daemon线程，每日扫描到期节点


# ---------- F8→F7联调：履约提醒经钉钉Connector外发 ----------
# 履约扫描到overdue/upcoming节点时，若钉钉Connector已启用，则经钉钉工作通知提醒；
# 脱敏由Connector内部mask_sensitive负责；钩子异常被吞，不阻塞扫描调度。

def _dingtalk_obligation_notify(node: dict, channel: str) -> None:
    """把履约义务节点转发钉钉工作通知（复用hub的Connector实例，异常不阻塞）"""
    try:
        from .integrations import base as _ibase
        conn = _ibase.get_connector("dingtalk")
        if not conn:
            return
        oblig_type_cn = {"payment": "回款", "milestone": "交付里程碑",
                         "renewal": "续约", "breach": "违约"}.get(node.get("oblig_type"), "义务")
        status_cn = "逾期" if node.get("status") == "overdue" else "临近到期"
        amount = node.get("amount")
        amount_txt = f"{amount}元" if amount is not None else "金额未定"
        title = f"履约提醒｜{oblig_type_cn}{status_cn}：{node.get('title', '')}"
        content = (f"**类型**：{oblig_type_cn}\\n**到期日**：{node.get('due_date', '')}\\n"
                   f"**金额**：{amount_txt}\\n**状态**：{status_cn}\\n"
                   f"**说明**：{node.get('description', '') or '-'}")
        conn.send_message(node.get("owner") or node.get("contract_id") or "admin",
                          title, content, contract_id=node.get("contract_id", ""))
    except Exception:
        pass  # 钩子异常不阻塞扫描调度


from .integrations.base import enabled_connectors as _enabled_connectors

if "dingtalk" in _enabled_connectors():
    obligations_mod.register_notify_hook("dingtalk", _dingtalk_obligation_notify)


@app.post("/api/contracts/{cid}/obligations/extract")
def obligations_extract(cid: str, request: Request):
    """从合同抽取义务节点并写入履约台账（LLM优先，降级启发式）"""
    user = _get_user(request, "review")
    c = _get_contract(cid)
    if not c:
        raise HTTPException(404, f"合同不存在: {cid}")
    result = obligations_mod.extract_obligations(c.raw_text or "", cid)
    obligations_mod.insert_obligations(cid, result["obligations"], result["source"])
    audit_mod.log(user["username"], "obligation_extract", cid,
                  f"抽取义务节点{len(result['obligations'])}个（来源{result['source']}）")
    return {"contract_id": cid, "count": len(result["obligations"]),
            "source": result["source"], "llm_status": result["llm_status"],
            "llm_error": result["llm_error"],
            "obligations": result["obligations"]}


@app.get("/api/contracts/{cid}/obligations")
def obligations_list(cid: str, request: Request):
    """单合同履约台账"""
    _get_user(request)
    if not db_mod.contract_exists(cid):
        raise HTTPException(404, f"合同不存在: {cid}")
    return {"contract_id": cid, "total": len(obligations_mod.list_obligations(cid)),
            "items": obligations_mod.list_obligations(cid)}


@app.put("/api/obligations/{oblig_id}")
def obligation_update(oblig_id: str, request: Request, data: dict):
    """人工修正台账条目（自动标记manually_edited，后续抽取不覆盖）"""
    user = _get_user(request, "review")
    try:
        node = obligations_mod.update_obligation(oblig_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_mod.log(user["username"], "obligation_update", node["contract_id"],
                  f"修正义务节点{oblig_id}：{', '.join(f'{k}' for k in data)}")
    return node


@app.get("/api/obligations/overview")
def obligations_overview(request: Request, cid: str = ""):
    """全局履约台账+逾期统计；带cid时返回该合同台账"""
    user = _get_user(request)
    stats = obligations_mod.overdue_stats()
    items = obligations_mod.list_obligations(cid) if cid else obligations_mod.list_obligations()
    return {"stats": stats, "total": len(items), "items": items}


@app.get("/api/obligations/export")
def obligations_export(request: Request, cid: str = ""):
    """台账导出CSV（Excel可直接打开，UTF-8带BOM）"""
    user = _get_user(request)
    items = obligations_mod.list_obligations(cid) if cid else obligations_mod.list_obligations()
    content = obligations_mod.export_csv(items)
    audit_mod.log(user["username"], "obligation_export", cid or "-", f"导出台账{len(items)}条")
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=obligations.csv"})


@app.post("/api/obligations/scan")
def obligations_scan(request: Request):
    """手动触发到期扫描（调度线程每日自动执行，此接口供自测/补偿）"""
    user = _get_user(request, "review")
    result = obligations_mod.scan_and_notify()
    return result


# ---------- F9 统计与质检看板增强 ----------

from . import qc as qc_mod
from . import weekly as weekly_mod

QC_DIR = config.STORAGE_DIR / "reports"
QC_PAGE = Path(__file__).parent / "templates" / "qc.html"


@app.get("/api/qc/overview")
def qc_overview_api(request: Request):
    """质检看板：各规则准确率/误报率 + 复核进度 + 待复核列表"""
    _get_user(request, "qc_manage")
    return qc_mod.qc_overview()


@app.post("/api/qc/assign")
def qc_assign_api(request: Request, data: dict = None):
    """生成质检抽样任务（rate可选，默认QC_SAMPLE_RATE；>=1全量）"""
    user = _get_user(request, "qc_manage")
    data = data or {}
    rate = data.get("rate")
    result = qc_mod.assign_batch(db_mod.list_contract_rows(), user["username"], rate=rate)
    audit_mod.log(user["username"], "qc_assign", "-",
                  f"批次{result['batch_id']} 抽样{result['sampled']}/{result['finding_total']}")
    return result


@app.post("/api/qc/review")
def qc_review_api(request: Request, data: dict):
    """提交复核结论：qc_id + verdict(correct/wrong/partial) + comment"""
    user = _get_user(request, "qc_manage")
    try:
        ok = qc_mod.submit_review(int(data.get("qc_id", 0)),
                                  str(data.get("verdict", "")),
                                  str(data.get("comment", "")),
                                  user["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "质检任务不存在")
    audit_mod.log(user["username"], "qc_review", "-",
                  f"qc_id={data.get('qc_id')} verdict={data.get('verdict')}")
    return {"ok": True}


@app.get("/api/stats/dims")
def api_stats_dims(request: Request):
    """业务看板增强：按部门/合同类型/风险等级多维统计 + 审查人效率排行（数据不足返回空集合）"""
    _get_user(request)
    return {"dimensions": qc_mod.biz_dimensions(db_mod.list_contract_rows()),
            "reviewer_efficiency": qc_mod.reviewer_efficiency()}


@app.get("/api/report/weekly")
def api_weekly_report(request: Request):
    """导出docx统计周报（风险分布+质检指标+审查趋势+审查人效率）"""
    user = _get_user(request, "report")
    out = QC_DIR / f"weekly_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    weekly_mod.generate_weekly_report(out)
    audit_mod.log(user["username"], "weekly_report", "-", f"导出周报{out.name}")
    return FileResponse(str(out), filename=out.name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/qc", response_class=HTMLResponse)
def qc_page(request: Request):
    """质检看板页面"""
    return HTMLResponse(content=QC_PAGE.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


# ---------- 小灵通悬浮球助手 ----------

_ASSISTANT_SYSTEM = (
    "你是ClauseGuard合同智能审查系统的内置助手「小灵通」，亲切、简洁、专业。"
    "你负责解答：系统各功能的使用方法（合同上传/审查/批注报告/版本比对/范本基线/履约台账/质检看板/知识库等）、"
    "合同审查的常识性问题与日常问答。回答控制在150字以内；不确定的法律法规细节不要编造，"
    "建议用户使用系统的智能审查功能或咨询专业法务。不要透露系统内部实现细节。"
)

@app.post("/api/assistant/chat")
def assistant_chat_api(request: Request, data: dict):
    """小灵通多轮对话：message当前消息 + history最近历史（≤10条）"""
    user = _get_user(request)
    message = str(data.get("message", "")).strip()
    if not message:
        raise HTTPException(400, "消息不能为空")
    history = data.get("history") or []
    if not isinstance(history, list):
        history = []
    # 只保留role/content，最多10条，防止提示词注入超长
    msgs = [{"role": "system", "content": _ASSISTANT_SYSTEM}]
    for h in history[-10:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": str(h["content"])[:2000]})
    msgs.append({"role": "user", "content": message[:2000]})
    if not llm_mod.is_configured():
        return {"ok": False, "reply": "小灵通暂不可用：系统尚未配置LLM服务，请联系管理员在系统设置中配置。"}
    try:
        reply = llm_mod.chat_messages(msgs, timeout=60)
    except Exception:
        reply = "抱歉，小灵通刚才开小差了，请稍后再试～"
    audit_mod.log(user["username"], "assistant_chat", "-", f"消息长度{len(message)}")
    return {"ok": True, "reply": reply}
