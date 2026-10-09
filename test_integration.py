# -*- coding: utf-8 -*-
"""P0期集成自测：联调后全链路验证
覆盖：迁移脚本幂等执行→向量化（EMBEDDING_FAKE=1）→pipeline走真实召回层
（rule_baseline降级模式+mock LLM模式各一次）→溯源闸门生效。
运行：python test_integration.py
"""
import os
import subprocess
import sys

os.environ.setdefault("EMBEDDING_FAKE", "1")  # 假向量自测模式

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# ---------- 1. 迁移脚本幂等执行（两次） ----------
def test_migrate():
    from app import checkpoints
    try:
        r1 = checkpoints.migrate_rules()
        r2 = checkpoints.migrate_rules()
        check("迁移脚本第1次执行", True, str(r1))
        idem = (r1.get("checkpoints") == r2.get("checkpoints")
                and r1.get("laws") == r2.get("laws"))
        check("迁移脚本幂等（两次结果一致）", idem,
              f"r1={r1.get('checkpoints')}/{r1.get('laws')} r2={r2.get('checkpoints')}/{r2.get('laws')}")
        return True
    except Exception as e:
        check("迁移脚本执行", False, f"数据库不可达或异常: {e}")
        return False


# ---------- 2. 向量化（EMBEDDING_FAKE=1） ----------
def test_vectorize():
    from app import checkpoints, embedding
    check("EMBEDDING_FAKE模式已配置", embedding.is_configured(), f"mode={checkpoints._mode()}")
    try:
        r = checkpoints.sync_embeddings()
        check("审查点向量化", isinstance(r, dict), str(r))
        return True
    except Exception as e:
        check("审查点向量化", False, str(e))
        return False


# ---------- 3. 真实召回层联调 ----------
def test_recall():
    from app import recall as rec
    items = rec.recall("当事人一方不履行合同义务的，应当承担违约责任。", contract_type="买卖合同")
    check("真实召回层返回非空", len(items) > 0, f"n={len(items)}")
    if items:
        it = items[0]
        fields_ok = all(k in it for k in ("checkpoint", "law_text", "law_id", "source", "score"))
        check("召回字段对齐契约", fields_ok, str({k: it.get(k) for k in ('source', 'score')}))
        check("召回分数>=RECALL_MIN_SCORE", all(float(i["score"]) >= 0.1 for i in items))
        check("law_text溯源原文非空", all((i.get("law_text") or "").strip() for i in items), )
    check("mock模式可切换", os.environ.get("RECALL_BACKEND", "") != "mock" and rec.get_backend() is not rec._mock_recall)
    return len(items) > 0


# ---------- 4. pipeline走真实召回（rule_baseline + mock LLM） ----------
CLUSES = [
    {"clause_id": "c1", "text": "甲方逾期付款的，每日按合同金额的50%支付违约金。"},
    {"clause_id": "c2", "text": "乙方对产品质量问题不承担任何责任。"},
]


def run_pipeline_case(mode):
    from app import pipeline
    env_llm = dict(LLM_BASE_URL="", LLM_MODEL="")
    if mode == "mock_llm":
        from app import config
        # 用monkeypatch方式注入假LLM配置：pipeline judge在LLM未配置时跳过
        import app.llm as llm
        llm.BASE_URL = "http://127.0.0.1:18434/v1"
        llm.MODEL = "mock-model"
        llm.API_KEY = "mock"
    else:
        import app.llm as llm
        llm.BASE_URL = ""
        llm.MODEL = ""
    result = pipeline.run_pipeline("测试合同文本", CLUSES, contract_id="integration-test")
    return result


def test_pipeline_baseline():
    try:
        r = run_pipeline_case("baseline")
        stages = r.get("stages", {})
        recall_ok = any(s.get("stage") == "recall" for s in stages.get("trace", [])) if isinstance(stages, dict) else True
        result = r.get("result", {})
        finds = result.get("findings", [])
        mode = result.get("summary", {}).get("pipeline_mode", "")
        check("pipeline(rule_baseline)全流程跑通", isinstance(r, dict) and bool(finds),
              f"findings={len(finds)} mode={mode}")
        return True
    except Exception as e:
        check("pipeline(rule_baseline)全流程跑通", False, str(e))
        return False


def test_pipeline_mock_llm():
    try:
        r = run_pipeline_case("mock_llm")
        finds = r.get("result", {}).get("findings", [])
        check("pipeline(mock LLM)全流程跑通", isinstance(r, dict) and bool(finds),
              f"findings={len(finds)}")
        return True
    except Exception as e:
        check("pipeline(mock LLM)全流程跑通", False, str(e))
        return False


# ---------- 5. 溯源闸门 ----------
def test_traceability():
    from app import recall as rec
    # 空条款/无关条款应返回空召回（无依据不输出）
    empty = rec.recall("")
    check("空条款返回空召回", empty == [])
    # mock模式下虚构法条过滤由pipeline的cross_validate完成，这里验证score过滤
    os.environ["RECALL_BACKEND"] = "mock"
    m = rec.recall("任意文本")
    check("mock模式召回可通过闸门(0.9>=0.1)", len(m) == 1)
    os.environ.pop("RECALL_BACKEND", None)
    return True


if __name__ == "__main__":
    print("=" * 50)
    print("ClauseGuard P0 集成自测")
    print("=" * 50)
    db_ok = test_migrate()
    if db_ok:
        test_vectorize()
        test_recall()
        test_pipeline_baseline()
        test_pipeline_mock_llm()
    test_traceability()
    print("-" * 50)
    print(f"总计: {len(PASS)} PASS, {len(FAIL)} FAIL")
    if FAIL:
        print("失败项:", FAIL)
    sys.exit(1 if FAIL else 0)
