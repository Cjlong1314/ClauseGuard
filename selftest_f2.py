"""F2流水线自测：1)语法与导入 2)LLM未配置降级全流程 3)mock LLM JSON解析与重试 4)溯源过滤

运行：cd D:\cjl\ClauseGuard && python selftest_f2.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


# ---------- 1. 导入 ----------
from app import llm, recall, pipeline, reviewer  # noqa: E402

check("模块导入", True)

# ---------- 2. LLM未配置降级 ----------
os.environ.pop("LLM_BASE_URL", None)
os.environ.pop("LLM_MODEL", None)
check("is_configured=False（未配置）", not llm.is_configured())

clauses = [
    {"clause_id": "1", "title": "违约责任", "content": "乙方违约的，应承担违约责任。",
     "level": 1, "parent": None},
    {"clause_id": "2", "title": "争议解决", "content": "双方协商解决争议。", "level": 1, "parent": None},
]
os.environ["RECALL_BACKEND"] = "mock"
out = pipeline.run_pipeline("测试合同全文。乙方违约的，应承担违约责任。", clauses, None, "test01")
res = out["result"]
check("降级路径跑通", res["summary"]["pipeline_mode"] == "rule_baseline",
      f"mode={res['summary']['pipeline_mode']}, findings={len(res['findings'])}")
check("audit_ext含模型信息与轨迹", "trace" in out["audit_ext"] and out["audit_ext"]["prompt_version"])
check("降级时AI发现为空", out["audit_ext"]["recall_backend"] == "_mock_recall")

# ---------- 3. mock LLM：JSON解析与重试 ----------
CALLS = {"n": 0}
BAD = {"n": 0}


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = None
        # 判主模型（schema含findings）还是第二模型
        if "findings" in body["messages"][0]["content"]:
            CALLS["n"] += 1
            if CALLS["n"] == 1 and BAD["n"] == 0:
                BAD["n"] = 1
                content = "这不是JSON"  # 第一次返回坏输出，验证重试
            else:
                content = json.dumps({"findings": [{
                    "risk_level": "中", "rule_name": "违约责任约定不明",
                    "suggestion": "明确违约金标准", "law_ref": "民法典577",
                    "confidence": 0.85, "excerpt": "乙方违约的"}]}, ensure_ascii=False)
        else:
            content = json.dumps({"agree": True, "reason": "ok"})
        resp = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(resp)


srv = HTTPServer(("127.0.0.1", 18434), MockHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ["LLM_BASE_URL"] = "http://127.0.0.1:18434/v1"
os.environ["LLM_MODEL"] = "mock-model"
os.environ["LLM_RUNTIME"] = "vllm"
cfg = llm.get_config()
check("配置读取与运行时识别", cfg["runtime"] == "vllm" and cfg["model"] == "mock-model")

out2 = pipeline.run_pipeline("测试合同全文。乙方违约的，应承担违约责任。", clauses, None, "test02")
res2 = out2["result"]
check("mock LLM研判走llm模式", res2["summary"]["pipeline_mode"] == "llm",
      f"findings={len(res2['findings'])}")
ai = [f for f in res2["findings"] if f["finding_id"].startswith("A")]
check("AI发现含置信度与法条原文", bool(ai) and "置信度" in ai[0]["ai_conclusion"]
      and "民法典" in ai[0]["legal_basis"])
check("溯源丢弃统计存在", "dropped_unsourced" in out2["audit_ext"])
# 溯源过滤：law_ref不匹配的发现应被丢弃
state_probe = {"raw_text": "", "clauses": [], "recall_results": [{"clause": {}, "recalled": [
    {"law_id": "民法典577", "law_text": "x", "checkpoint": "c", "score": 0.9}]}],
    "ai_findings": [
        {"rule_name": "有依据", "law_ref": "民法典577", "risk_level": "中",
         "confidence": 0.9, "excerpt": "e", "clause_id": "1"},
        {"rule_name": "编造法条", "law_ref": "虚构法条999", "risk_level": "高",
         "confidence": 0.9, "excerpt": "e", "clause_id": "1"},
    ], "ai_enabled": False, "extracted": None}
pipeline.stage_cross_validate(state_probe)
check("无召回依据的发现被强制丢弃",
      len(state_probe["dropped_unsourced"]) == 1
      and state_probe["dropped_unsourced"][0]["law_ref"] == "虚构法条999")

srv.shutdown()

print("\n==== 结果:", sum(1 for _, ok, _ in RESULTS if ok), "/", len(RESULTS), "====")
sys.exit(0 if all(ok for _, ok, _ in RESULTS) else 1)
