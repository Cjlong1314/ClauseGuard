# -*- coding: utf-8 -*-
"""F6要素抽取升级自测：降级路径、mock LLM的JSON解析与冲突标记"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("LLM_BASE_URL", None)
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_MODEL", None)

from app import extractor, llm
from app.extractor import _parse_llm_json, _validate_llm, _cross_check
from app.models import ExtractedInfo, Party

CONTRACT = """买卖合同

甲方：北京某某科技有限公司
统一社会信用代码：91110000MA01ABC234
乙方：上海某某贸易有限公司
统一社会信用代码：91310000MA02XYZ567

第一条 标的金额：合同总价为人民币1,000,000元（壹佰万元整）。
第二条 合同期限：2026年1月1日至2026年12月31日。
第三条 争议解决：因本合同发生争议，提交北京仲裁委员会仲裁。
签署日期：2025年12月20日
"""

ok = []

# ---------- 1. LLM未配置：降级纯正则 ----------
assert llm.is_configured() is False, "未配置环境变量时is_configured应为False"
r = extractor.extract_with_llm(CONTRACT, "test001")
assert r["llm_status"] == "disabled" and r["source"] == "regex", r
assert r["llm_extracted"] is None and r["conflicts"] == []
assert r["extracted"]["contract_type"] == "买卖合同"
assert r["extracted"]["amount"], "金额应为空抽取失败检查"
assert r["extracted"]["sign_date"] in ("2025年12月20日", "2026年1月1日"), r["extracted"]["sign_date"]
assert len(r["extracted"]["parties"]) == 2
ok.append("降级路径：LLM未配置时只用正则结果，llm_status=disabled")

# ---------- 2. JSON解析（含markdown包裹/杂质） ----------
assert _parse_llm_json('{"a":1}') == {"a": 1}
assert _parse_llm_json('```json\n{"contract_type": "租赁合同"}\n```') == {"contract_type": "租赁合同"}
assert _parse_llm_json('好的，结果如下：\n{"x": {"y": 2}}\n以上。') == {"x": {"y": 2}}
try:
    _parse_llm_json("没有json")
    raise AssertionError("应抛ValueError")
except ValueError:
    pass
ok.append("JSON解析：裸JSON/markdown代码块/前后杂质均可解析，无JSON时报错")

# ---------- 3. Schema校验与规整 ----------
bad = {"contract_type": "其他类型", "parties": [{"role": "甲方", "name": "A公司"}],
       "amount": "", "term": "", "sign_date": "", "jurisdiction": "",
       "dispute_resolution": "法院判决"}
try:
    _validate_llm(bad)
    raise AssertionError("非法contract_type应报错")
except ValueError:
    pass
good = dict(bad, contract_type="买卖合同", dispute_resolution="诉讼")
v = _validate_llm(good)
assert v["dispute_resolution"] == "诉讼"
v2 = _validate_llm(dict(good, dispute_resolution="法院", parties=[{"role": "甲方", "name": " A "}, {"name": 3}]))
assert v2["dispute_resolution"] == "未知" and len(v2["parties"]) == 1
ok.append("Schema校验：非法enum/字段类型被拒或规整，脏parties被过滤")

# ---------- 4. mock LLM：成功合并+冲突标记 ----------
class MockLLM:
    calls = 0
    @staticmethod
    def is_configured():
        return True
    @staticmethod
    def chat(system, user, timeout=60):
        MockLLM.calls += 1
        if MockLLM.calls == 1:
            # 第一次返回格式非法，触发重试
            return "抱歉我不确定"
        return ('```json\n{"contract_type": "租赁合同", "parties": ['
                '{"role": "甲方", "name": "北京某某科技有限公司", "identifier": "91110000MA01ABC234"},'
                '{"role": "乙方", "name": "上海某某贸易公司", "identifier": "91310000MA02XYZ567"}], '
                '"amount": "壹佰万元整", "term": "一年", "sign_date": "2025年12月20日", '
                '"jurisdiction": "北京仲裁委员会", "dispute_resolution": "仲裁"}\n```')

orig_chat, orig_cfg = llm.chat, llm.is_configured
llm.chat, llm.is_configured = MockLLM.chat, MockLLM.is_configured
try:
    r2 = extractor.extract_with_llm(CONTRACT, "test002")
finally:
    llm.chat, llm.is_configured = orig_chat, orig_cfg
assert MockLLM.calls == 2, f"应触发1次重试，实际调用{MockLLM.calls}次"
assert r2["llm_status"] == "ok" and r2["source"] == "llm_merged"
assert r2["extracted"]["contract_type"] == "租赁合同"
assert r2["extracted"]["dispute_resolution"] == "仲裁"
fields = {c["field"] for c in r2["conflicts"]}
assert "contract_type" in fields, f"类型冲突应标记: {fields}"
assert "amount" in fields
assert any(f.startswith("parties.乙方.name") for f in fields), f"乙方名称冲突应标记: {fields}"
assert all(c["status"] == "需人工确认" for c in r2["conflicts"])
ok.append(f"mock LLM：非法回复重试成功；交叉校验标记{len(r2['conflicts'])}个冲突项（含类型/金额/乙方名称），状态均为'需人工确认'")

# ---------- 5. mock LLM：重试耗尽降级 ----------
class MockFail:
    calls = 0
    @staticmethod
    def is_configured():
        return True
    @staticmethod
    def chat(system, user, timeout=60):
        MockFail.calls += 1
        raise llm.LLError("mock超时")

llm.chat, llm.is_configured = MockFail.chat, MockFail.is_configured
try:
    r3 = extractor.extract_with_llm(CONTRACT, "test003")
finally:
    llm.chat, llm.is_configured = orig_chat, orig_cfg
assert MockFail.calls == 3, f"应调用3次(1+2重试)，实际{MockFail.calls}"
assert r3["llm_status"] == "failed" and r3["source"] == "regex"
assert r3["extracted"]["contract_type"] == "买卖合同"
assert r3["llm_error"], "失败原因应记录"
ok.append("重试耗尽：调用1+2次后降级为正则结果，llm_status=failed并记录llm_error")

print("\n".join(f"[PASS] {s}" for s in ok))
print(f"\n全部{len(ok)}项自测通过")
