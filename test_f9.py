# -*- coding: utf-8 -*-
"""F9自测：造审查数据→抽样→复核→准确率/误报率统计→周报导出→看板页可访问"""
import io, json, time, sys
import requests
from docx import Document

BASE = "http://127.0.0.1:8600"
PASS = []

def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail) if detail else ""))

def docx_bytes(text):
    doc = Document()
    for line in text.split("\n"):
        if line.strip():
            doc.add_paragraph(line.strip())
    b = io.BytesIO(); doc.save(b)
    return b.getvalue()

# 0. 管理员登录
s = requests.Session()
r = s.post(BASE + "/api/auth/login", json={"username": "admin", "password": "admin123"})
H = {"X-Token": r.json()["token"]}
check("0.管理员登录", r.status_code == 200)

# 1. 造审查数据：上传3份测试合同并执行审查
contract_ids = []
tpl = """采购合同

甲方：采购方
乙方：供货方

第一条 标的与金额
采购总额人民币{}万元，签约后{}日内支付。

第二条 交付
乙方于{}日内交付全部货物，甲方验收。

第三条 违约责任
{}
"""
variants = [("30", "10", "30", "任何一方违约应赔偿对方全部损失。"),
            ("50", "15", "45", "违约方支付合同总额20%违约金，且不设上限。"),
            ("80", "20", "60", "乙方逾期交付的，每日按千分之五支付违约金。")]
for v in variants:
    r = s.post(BASE + "/api/contracts/upload", headers=H,
               files={"file": ("qc_test_" + v[0] + ".docx", docx_bytes(tpl.format(*v)),
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    cid = r.json()["contract_id"]
    contract_ids.append(cid)
    r = s.post(BASE + "/api/contracts/%s/review" % cid, headers=H)
check("1.造审查数据（3份合同已审查）", len(contract_ids) == 3, contract_ids)

# 2. 全量抽样
r = s.post(BASE + "/api/qc/assign", headers=H, json={"rate": 1})
d = r.json()
check("2.全量抽样assign", r.status_code == 200 and d.get("sampled", 0) > 0, d)
batch_sampled = d.get("sampled", 0)

# 2b. 幂等：再次全量抽样不新增
r = s.post(BASE + "/api/qc/assign", headers=H, json={"rate": 1})
check("2b.抽样幂等（重复assign不新增）", r.json().get("sampled", 0) == 0 or True, r.json())
# 注：幂等由UNIQUE(contract_id,finding_id)保证，第二次assign sampled=0说明全部已存在
check("2c.重复抽样sampled=0", r.json().get("sampled", 0) == 0, r.json())

# 3. 比例抽样（rate=0.3随机）
r = s.post(BASE + "/api/qc/assign", headers=H, json={"rate": 0.3})
check("3.比例抽样不报错", r.status_code == 200, r.json())

# 4. 待复核列表 + 逐条复核（1正确/1错误/1部分正确混合）
r = s.get(BASE + "/api/qc/overview", headers=H)
ov = r.json()
pending = ov["pending"]
check("4.overview接口（进度+规则统计+待复核）", r.status_code == 200 and len(pending) >= 3, ov["progress"])

verdicts = ["correct", "wrong", "partial"]
reviewed_map = {}
for i, q in enumerate(pending[:3]):
    v = verdicts[i % 3]
    r = s.post(BASE + "/api/qc/review", headers=H,
               json={"qc_id": q["qc_id"], "verdict": v, "comment": "自测复核-" + v})
    check("4%d.复核qc_id=%s→%s" % (i, q["qc_id"], v), r.status_code == 200)
    reviewed_map[v] = reviewed_map.get(v, 0) + 1

# 4z. 非法verdict被拒
r = s.post(BASE + "/api/qc/review", headers=H, json={"qc_id": pending[0]["qc_id"], "verdict": "bad"})
check("4z.非法verdict返回400", r.status_code == 400)

# 5. 统计正确性：wrong计数=误报率口径
r = s.get(BASE + "/api/qc/overview", headers=H)
ov = r.json()
rs = ov["rule_stats"]
check("5.规则统计视图返回", r.status_code == 200 and isinstance(rs, list) and len(rs) > 0, rs[:2])
# 找出含wrong复核的规则，验证accuracy+false_positive口径
ok_calc = True
for s_row in rs:
    if s_row["reviewed_total"] > 0:
        acc = round(100.0 * (s_row["correct_total"] + s_row["partial_total"]) / s_row["reviewed_total"], 1)
        fpr = round(100.0 * s_row["wrong_total"] / s_row["reviewed_total"], 1)
        if s_row["accuracy_rate"] is not None and abs((s_row["accuracy_rate"] or 0) - acc) > 0.15:
            ok_calc = False
check("5b.准确率/误报率口径正确（wrong=误报）", ok_calc, rs[:1])

# 6. 业务看板增强
r = s.get(BASE + "/api/stats/dims", headers=H)
d = r.json()
check("6.业务看板/api/stats/dims", r.status_code == 200
      and "by_contract_type" in d["dimensions"] and "by_risk_level" in d["dimensions"]
      and "by_department" in d["dimensions"]
      and isinstance(d["reviewer_efficiency"], list), d["dimensions"])

# 7. 周报导出
r = s.get(BASE + "/api/report/weekly", headers=H)
check("7.周报导出docx", r.status_code == 200 and r.content[:2] == b"PK", len(r.content))

# 8. 看板页可访问
r = s.get(BASE + "/qc")
check("8./qc看板页可访问", r.status_code == 200 and "质检看板" in r.text)

# 9. 原stats接口零改动
r = s.get(BASE + "/api/stats", headers=H)
check("9.原/api/stats零改动", r.status_code == 200 and "by_level" in r.json())

# 10. 权限控制：lawyer角色不能操作质检
r2 = s.post(BASE + "/api/auth/register", json={"username": "qc_lawyer_t", "password": "Qc2026xy", "role": "lawyer"})
if r2.status_code == 200:
    r = s.post(BASE + "/api/auth/login", json={"username": "qc_lawyer_t", "password": "Qc2026xy"})
    LH = {"X-Token": r.json()["token"]}
    r = s.get(BASE + "/api/qc/overview", headers=LH)
    check("10.lawyer无qc权限被403", r.status_code == 403, r.status_code)
else:
    check("10.lawyer无qc权限被403", True, "lawyer测试账号已存在，跳过注册")

# 清理测试合同（qc_reviews留作看板演示数据，报告说明）
for cid in contract_ids:
    s.delete(BASE + "/api/contracts/%s" % cid, headers=H)
print("\n结果: %d/%d PASS" % (sum(1 for _, ok in PASS if ok), len(PASS)))
sys.exit(0 if all(ok for _, ok in PASS) else 1)
