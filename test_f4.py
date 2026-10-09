"""F4范本基线自测：建基线→上传对照合同→差异审查输出全流程"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import baseline as bm
from app import parser, extractor, config
import tempfile, os

PASS = []
def check(name, cond, detail=""):
    PASS.append(cond)
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))

# ---------- 1. 建基线（含业务线/立场多套） ----------
b1 = bm.create_baseline({
    "name": "采购合同范本（买方立场）", "contract_type": "买卖",
    "business_line": "采购", "stance": "买方",
    "content": (
        "第一条 标的与价款\n采购货物总价款为人民币五十万元，含税含运费。\n"
        "第二条 交付期限\n卖方应于合同签订后三十日内交付全部货物至买方指定地点。\n"
        "第三条 质量与验收\n货物应符合国家标准，买方验收合格后十日内付款。\n"
        "第四条 违约责任\n卖方逾期交付的，每逾期一日按总价款的千分之三支付违约金。\n"
        "第五条 保密条款\n双方对因本合同知悉的对方商业秘密负有保密义务。\n"
        "第六条 争议解决\n因本合同发生的争议，提交买方所在地人民法院管辖。\n"
    ),
    "suggestions": {"保密": "任何一方对在合作期间知悉的对方商业秘密负有保密义务，保密期限自合同终止后持续三年。",
                    "争议解决": "因本合同发生的争议，双方协商不成的，提交合同签订地人民法院管辖。"},
}, "tester")
check("创建基线b1", bool(b1.get("baseline_id")), f"baseline_id={b1['baseline_id']} v{b1['version']}")

b2 = bm.create_baseline({
    "name": "采购合同范本（卖方立场）", "contract_type": "买卖",
    "business_line": "采购", "stance": "卖方",
    "content": "第一条 标的与价款\n货物总价款五十万元，款到发货。\n第二条 交付期限\n收到全款后十五日内交付。\n",
}, "tester")
check("同类型多套基线（按立场区分）", b1["baseline_id"] != b2["baseline_id"]
      and len(bm.list_baselines(contract_type="买卖")) >= 2)

# ---------- 2. 版本管理 ----------
bm.update_baseline(b1["baseline_id"], {
    "content": b1["content"] + "第七条 不可抗力\n因不可抗力不能履行合同的，根据影响部分或全部免除责任。\n",
    "comment": "补充不可抗力条款",
}, "tester")
b1v2 = bm.get_baseline(b1["baseline_id"])
vers = bm.list_versions(b1["baseline_id"])
check("content变更升版本", b1v2["version"] == 2 and len(vers) == 2, f"v{b1v2['version']}, 历史{len(vers)}条")
old = bm.get_version(b1["baseline_id"], 1)
check("历史版本可查", old and old["version"] == 1 and "不可抗力" not in old["content"])

# ---------- 3. 对照合同（含缺失/偏离/不利条款） ----------
contract_text = (
    "买卖合同\n"
    "第一条 标的与价款：采购货物总价款为人民币八十万元，含税含运费。\n"
    "第二条 交付期限：卖方应于合同签订后三十日内交付全部货物至买方指定地点。\n"
    "第三条 质量与验收：货物应符合国家标准，买方验收合格后十日内付款。\n"
    "第四条 违约责任：卖方逾期交付的，每逾期一日按总价款的千分之五支付违约金。\n"
    "第五条 特别约定：买方逾期付款的，卖方有权无偿留置全部货物且不承担任何货物毁损灭失责任，买方不得异议。\n"
)
tmp = os.path.join(tempfile.gettempdir(), "f4_contract.txt")
open(tmp, "w", encoding="utf-8").write(contract_text)
text = parser.extract_text(__import__("pathlib").Path(tmp))
clauses = parser.split_clauses(text)
check("对照合同切分", len(clauses) >= 5, f"{len(clauses)}条")

result = bm.baseline_diff([c if isinstance(c, dict) else c for c in clauses], b1v2)
s = result["summary"]
missing = [i for i in result["items"] if i["type"] == "missing"]
deviation = [i for i in result["items"] if i["type"] == "deviation"]
unfav = [i for i in result["items"] if i["type"] == "unfavorable"]

check("缺失条款识别（保密/争议解决）",
      s["missing"] >= 1 and any("保密" in (i["baseline_item"]["title"] + i["baseline_item"]["content"]) or
                                "争议" in (i["baseline_item"]["title"] + i["baseline_item"]["content"])
                                for i in missing),
      f"缺失{s['missing']}条")
check("改写偏离识别（价款80万/违约金千分五）",
      s["deviation"] >= 1 and any("八十万" in str(i) or "千分之五" in str(i) for i in deviation),
      f"偏离{s['deviation']}条")
check("不利己方识别（无偿留置+不得异议）",
      s["unfavorable"] >= 1 and any("留置" in str(i) or "不得异议" in str(i) for i in unfav),
      f"不利{s['unfavorable']}条")
check("偏离条目均附修改建议",
      all(i["suggestion"] for i in result["items"] if i["type"] in ("missing", "deviation", "unfavorable")))
check("建议引用范本措辞",
      any("保密" in i["suggestion"] and "三年" in i["suggestion"] for i in missing if i["suggestion"]))
check("高危标记生效", s["high_risk"] >= 1, f"高危{s['high_risk']}条")
check("偏离率统计", 0 < s["deviation_rate"] <= 100, f"{s['deviation_rate']}%")

# ---------- 4. compare.py兼容 ----------
from app import compare as cm
tpl = bm.get_baseline(b1["baseline_id"])["content"]
old_res = cm.compare(clauses, tpl)
check("compare原接口兼容", "similarity" in old_res and "matched" in old_res and "missing" in old_res,
      f"整体相似度{old_res['similarity']}")

print(f"\n{sum(PASS)}/{len(PASS)} PASS")
sys.exit(0 if all(PASS) else 1)
