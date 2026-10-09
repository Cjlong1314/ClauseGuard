# -*- coding: utf-8 -*-
"""清理P1冒烟测试账号与测试数据"""
from app import db
import json, os

cid = bid = None
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_p1_result.json")
if os.path.exists(p):
    d = json.load(open(p, encoding="utf-8"))
    cid, bid = d.get("cid"), d.get("bid")

with db.get_conn() as conn:
    cur = conn.cursor()
    if cid:
        cur.execute("DELETE FROM obligations WHERE contract_id=%s", (cid,))
        cur.execute("DELETE FROM extraction_results WHERE contract_id=%s", (cid,))
        cur.execute("DELETE FROM extraction_corrections WHERE contract_id=%s", (cid,))
        cur.execute("DELETE FROM audit_log WHERE contract_id=%s", (cid,))
        cur.execute("DELETE FROM integration_outbox WHERE event_type::text LIKE %s OR payload::text LIKE %s", ('%' + cid + '%', '%' + cid + '%'))
        cur.execute("SELECT filename FROM contracts WHERE contract_id=%s", (cid,))
        row = cur.fetchone()
        cur.execute("DELETE FROM contracts WHERE contract_id=%s", (cid,))
        print("contract deleted:", cid, row)
    if bid:
        cur.execute("DELETE FROM kb_baseline_versions WHERE baseline_id=%s", (bid,))
        cur.execute("DELETE FROM kb_template_baselines WHERE baseline_id=%s", (bid,))
        print("baseline deleted:", bid)
    cur.execute("DELETE FROM users WHERE username='smoke_p1'")
    print("user deleted:", cur.rowcount)
    conn.commit()
print("cleanup done")
