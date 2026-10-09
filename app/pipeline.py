"""Agent审查流水线（F2第二部分）

编排：意图识别→逐条款召回→LLM研判（置信度+JSON Schema约束+重试）→交叉验证→输出
采用轻量自研状态机（显式阶段枚举+逐步推进+每阶段可独立测试），未引入LangGraph。

取舍说明：流水线为固定五阶段的线性DAG，无条件分支/循环/人工中断恢复需求，
LangGraph的图编排与checkpointer收益有限，反而引入LangChain全家桶依赖，
与"私有化离线部署、依赖最小化"目标冲突；自研状态机约200行、零新增依赖、
每阶段可单测，且llm.chat_json已实现Schema约束+重试。后续若出现多Agent
协作或断点续跑需求，可平滑迁移到LangGraph（阶段函数签名即节点函数）。

防幻觉机制：
1. 引用强制溯源：LLM输出引用的law_ref必须能在召回结果中匹配（law_id子串），
   否则丢弃该发现；
2. 输出附置信度（LLM自评0~1）与召回依据原文；
3. 保留人工确认状态字段（status默认open）；
4. 研判过程（模型版本、提示词版本、召回依据、阶段轨迹）写入audit_ext扩展字段。
"""
import time
from datetime import datetime

from . import extractor, llm, recall, reviewer
from .config import PROMPT_VERSION

# 状态机阶段定义（顺序执行，任一阶段失败均不抛出到主流程，降级处理）
STAGES = ["intent", "recall", "judge", "cross_validate", "assemble"]

JUDGE_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["risk_level", "rule_name", "law_ref", "confidence"],
                "properties": {
                    "risk_level": {"type": "string", "enum": ["高", "中", "低"]},
                    "rule_name": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "law_ref": {"type": "string"},      # 必须引用召回依据的law_id
                    "confidence": {"type": "number"},    # 0~1自评置信度
                    "excerpt": {"type": "string"},
                },
            },
        }
    },
}

JUDGE_SYSTEM = (
    "你是合同合规审查助手。基于给定的召回依据（审查点与法条原文）研判条款风险。"
    "规则：1)只能输出JSON；2)每条发现必须引用召回依据中的law_ref（法条编号），"
    "召回依据中没有对应依据的不要输出（严禁编造法条）；3)confidence为0~1自评置信度；"
    "4)没有风险时findings返回空数组。"
)


def _stage_trace(state: dict, stage: str, detail: str):
    state.setdefault("trace", []).append(
        {"stage": stage, "time": datetime.now().strftime("%H:%M:%S.%f")[:-3], "detail": detail})


# ---------- 阶段1：意图识别 ----------

def stage_intent(state: dict) -> dict:
    """识别合同类型：优先抽取器结果，LLM可用时二次确认（失败静默用抽取结果）"""
    text = state["raw_text"]
    extracted = state.get("extracted") or extractor.extract(text)
    ctype = extracted.contract_type or "未知"
    if llm.is_configured() and ctype == "未知":
        try:
            obj = llm.chat_json(
                "你是合同分类器。判断合同类型（买卖/租赁/服务/借款/劳动/保密/知识产权/其他），"
                '仅输出JSON：{"contract_type": "..."}',
                text[:2000],
                schema={"type": "object", "required": ["contract_type"],
                        "properties": {"contract_type": {"type": "string"}}},
                max_retry=1)
            if obj.get("contract_type"):
                ctype = obj["contract_type"]
        except llm.LLError:
            pass  # 分类失败不影响主流程
    state["contract_type"] = ctype
    state["extracted"] = extracted
    _stage_trace(state, "intent", f"合同类型={ctype}")
    return state


# ---------- 阶段2：逐条款召回 ----------

def stage_recall(state: dict) -> dict:
    """逐条款调用召回层，获取审查点/法条依据"""
    results = []
    for cl in state["clauses"]:
        items = recall.recall(cl.get("content", ""), state["contract_type"], top_k=5)
        results.append({"clause": cl, "recalled": items})
    state["recall_results"] = results
    n = sum(len(r["recalled"]) for r in results)
    _stage_trace(state, "recall", f"{len(results)}条款，共召回{n}条依据")
    return state


# ---------- 阶段3：LLM研判 ----------

def stage_judge(state: dict) -> dict:
    """逐条款LLM研判；LLM未配置或失败时该条款无ai_findings（降级到规则引擎复核）"""
    ai_findings = []
    judge_errors = []
    ai_on = llm.is_configured()
    state["ai_enabled"] = ai_on
    if not ai_on:
        _stage_trace(state, "judge", "LLM未配置，跳过研判（降级）")
        state["ai_findings"] = []
        return state
    for r in state["recall_results"]:
        clause = r["clause"]
        recalled = r["recalled"]
        if not clause.get("content"):
            continue
        basis = "\n".join(
            f"[{i.get('law_id', '')}] 审查点:{i.get('checkpoint', '')} 法条:{i.get('law_text', '')}"
            for i in recalled)
        user = (f"合同类型：{state['contract_type']}\n条款{clause.get('clause_id', '')}："
                f"{clause.get('content', '')}\n\n召回依据：\n{basis or '（无）'}")
        try:
            obj = llm.chat_json(JUDGE_SYSTEM, user, schema=JUDGE_SCHEMA)
        except llm.LLError as e:
            judge_errors.append({"clause_id": clause.get("clause_id", ""), "error": str(e)[:200]})
            continue
        for f in obj.get("findings", []):
            ai_findings.append({**f, "clause_id": clause.get("clause_id", ""),
                                "excerpt": f.get("excerpt") or clause.get("content", "")[:120]})
    state["ai_findings"] = ai_findings
    state["judge_errors"] = judge_errors
    _stage_trace(state, "judge", f"AI发现{len(ai_findings)}条，失败{len(judge_errors)}条款")
    return state


# ---------- 阶段4：交叉验证 ----------

def stage_cross_validate(state: dict) -> dict:
    """规则引擎复核 + 可选第二模型复核，输出final_findings"""
    # (a) 规则引擎复核：全量跑一遍，作为基线发现合并进来
    rule_findings = reviewer.review(state["raw_text"], state["clauses"], state.get("extracted"))
    # (b) AI发现溯源过滤：law_ref必须命中召回依据law_id（防幻觉核心闸门）
    recalled_ids = {i.get("law_id", "") for r in state["recall_results"] for i in r["recalled"]}
    sourced, dropped = [], []
    for f in state.get("ai_findings", []):
        ref = f.get("law_ref", "")
        if any(ref and (ref == lid or ref in lid or lid in ref) for lid in recalled_ids if lid):
            sourced.append(f)
        else:
            dropped.append(f)
    # (c) 可选第二模型交叉验证：仅对高置信发现做第二意见
    second_opinions = {}
    if state.get("ai_enabled") and llm.is_configured("CROSS_"):
        for f in sourced:
            try:
                obj = llm.chat_json(
                    "你是第二审查员。对以下审查发现给出独立判断，"
                    '仅输出JSON：{"agree": true/false, "reason": "..."}',
                    f"{f.get('rule_name', '')}：{f.get('excerpt', '')} 依据：{f.get('law_ref', '')}",
                    schema={"type": "object", "required": ["agree"],
                            "properties": {"agree": {"type": "boolean"},
                                           "reason": {"type": "string"}}},
                    max_retry=1, prefix="CROSS_")
                second_opinions[f.get("rule_name", "")] = obj
                # 第二模型明确不同意时下调置信度并标记待人工确认
                if obj.get("agree") is False:
                    f["confidence"] = round(float(f.get("confidence", 0.5)) * 0.6, 2)
                    f["needs_human_confirm"] = True
            except llm.LLError:
                continue
    # 合并：规则发现在前（finding_id F开头），溯源AI发现在后（A开头）
    final = list(rule_findings)
    seq = len(final)
    for f in sourced:
        seq += 1
        final.append({
            "finding_id": f"A{seq:03d}",
            "dimension": "风险条款",
            "risk_level": f.get("risk_level", "中"),
            "clause_id": f.get("clause_id", ""),
            "excerpt": f.get("excerpt", ""),
            "rule_name": f.get("rule_name", ""),
            "legal_basis": _law_text_of(f.get("law_ref", ""), state),
            "suggestion": f.get("suggestion", ""),
            "status": "open",
            "comment": "",
            "ai_conclusion": f"置信度{f.get('confidence', 0)}；依据{f.get('law_ref', '')}"
                             + ("；第二模型存异议，待人工确认" if f.get("needs_human_confirm") else ""),
            "recalled_laws": [{"law_id": f.get("law_ref", ""),
                               "content": _law_text_of(f.get("law_ref", ""), state)}],
        })
    state["rule_findings"] = rule_findings
    state["final_findings"] = final
    state["dropped_unsourced"] = dropped
    state["second_opinions"] = second_opinions
    _stage_trace(state, "cross_validate",
                 f"规则{len(rule_findings)}条，AI溯源通过{len(sourced)}/"
                 f"{len(sourced) + len(dropped)}（无依据丢弃{len(dropped)}），合并{len(final)}条")
    return state


def _law_text_of(law_ref: str, state: dict) -> str:
    """按law_ref回查召回结果中的法条原文（溯源输出必附原文）"""
    for r in state["recall_results"]:
        for i in r["recalled"]:
            lid = i.get("law_id", "")
            if law_ref and lid and (law_ref == lid or law_ref in lid or lid in law_ref):
                return i.get("law_text", "")
    return ""


# ---------- 阶段5：组装输出 ----------

def stage_assemble(state: dict) -> dict:
    from .reviewer import build_summary
    summary = build_summary(state["final_findings"])
    summary["ai_enabled"] = state.get("ai_enabled", False)
    summary["pipeline_mode"] = "llm" if state.get("ai_enabled") else "rule_baseline"
    state["result"] = {
        "contract_id": state["contract_id"],
        "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "findings": state["final_findings"],
        "summary": summary,
    }
    # 审计扩展字段：研判过程全记录
    state["audit_ext"] = {
        "model": llm.model_info(),
        "prompt_version": PROMPT_VERSION,
        "contract_type": state.get("contract_type", ""),
        "recall_backend": recall.get_backend().__name__,
        "recall_basis_count": sum(len(r["recalled"]) for r in state["recall_results"]),
        "dropped_unsourced": len(state.get("dropped_unsourced", [])),
        "judge_errors": state.get("judge_errors", []),
        "trace": state.get("trace", []),
        "elapsed_ms": int((time.time() - state.get("_t0", time.time())) * 1000),
    }
    _stage_trace(state, "assemble", f"输出{len(state['final_findings'])}条发现")
    return state


# ---------- 状态机入口 ----------

def run_pipeline(raw_text: str, clauses: list, extracted=None, contract_id: str = "") -> dict:
    """顺序执行五阶段状态机。返回{result, audit_ext, trace}

    降级保证：LLM未配置时stage_judge直接跳过，交叉验证阶段仍由规则引擎产出
    完整基线发现，全流程可用且可独立测试。
    """
    state = {"raw_text": raw_text, "clauses": clauses, "extracted": extracted,
             "contract_id": contract_id, "_t0": time.time(), "trace": []}
    for stage_fn in (stage_intent, stage_recall, stage_judge,
                     stage_cross_validate, stage_assemble):
        state = stage_fn(state)
    return {"result": state["result"], "audit_ext": state["audit_ext"]}
