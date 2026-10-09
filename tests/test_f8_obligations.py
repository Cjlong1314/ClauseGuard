# -*- coding: utf-8 -*-
"""F8履约期义务提醒自测：降级抽取/mock LLM/到期扫描/幂等不重复提醒"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EMBEDDING_FAKE", "1")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))


CONTRACT = """
买卖合同
甲方：北京某某科技有限公司
乙方：上海某某贸易有限公司
第三条 付款方式：乙方应于2025年12月31日前支付货款人民币80万元至甲方账户。
第四条 交付：乙方应于2025年11月20日前完成全部设备交付并通过甲方验收。
第五条 续约：合同期满前30日内，双方可协商续签本合同。
第六条 违约责任：任一方逾期未履行义务的，每逾期一日按合同金额的千分之五支付违约金。
签署日期：2025年1月15日
"""


def main():
    from app import db as db_mod
    db_mod.init_db()  # 幂等建表（含F8 obligations表）
    from app import obligations as obl

    # 1. LLM未配置降级启发式抽取
    from app import llm
    configured = llm.is_configured()
    r = obl.extract_obligations(CONTRACT, "test_f8")
    check("1.1 LLM未配置时降级rule来源", (not configured) and r["source"] == "rule"
          or (configured and r["source"] == "llm"),
          f"configured={configured}, source={r['source']}")
    nodes = r["obligations"]
    check("1.2 启发式抽取出付款节点(日期+金额)", any(
        n["oblig_type"] == "payment" and n["due_date"] == "2025-12-31"
        and n["amount"] == 800000 for n in nodes),
        f"nodes={[(n['oblig_type'], n['due_date'], n['amount']) for n in nodes]}")

    # 2. mock LLM抽取（直接monkeypatch llm层）
    import app.llm as llm_mod
    orig_cfg, orig_cj = llm_mod.get_config, llm_mod.chat_json
    llm_mod.get_config = lambda prefix="": {"base_url": "http://mock", "api_key": "x",
                                           "model": "mock", "runtime": "openai"}
    llm_mod.chat_json = lambda system, user, schema=None, max_retry=None, timeout=60, prefix="": {
        "obligations": [
            {"oblig_type": "payment", "title": "首期款", "amount": 500000,
             "due_date": "2025-06-30", "description": "合同生效后支付"},
            {"oblig_type": "renewal", "title": "续约协商", "due_date": "2025-12-01"},
            {"oblig_type": "bad_type", "title": "非法类型应被过滤", "due_date": "2025-01-01"},
        ]}
    r2 = obl.extract_obligations(CONTRACT, "test_f8")
    check("2.1 mock LLM走llm来源", r2["source"] == "llm" and r2["llm_status"] == "ok")
    check("2.2 非法类型节点被过滤", len(r2["obligations"]) == 2
          and all(n["oblig_type"] in obl.OBLIGATION_TYPES for n in r2["obligations"]))
    # LLM失败重试耗尽→降级
    def boom(*a, **k):
        raise llm_mod.LLMResponseError("mock失败")
    llm_mod.chat_json = boom
    r3 = obl.extract_obligations(CONTRACT, "test_f8")
    check("2.3 LLM失败降级rule且记录错误", r3["source"] == "rule"
          and r3["llm_status"] == "failed" and "mock失败" in r3["llm_error"])
    llm_mod.get_config, llm_mod.chat_json = orig_cfg, orig_cj

    # 3. 台账写入/状态刷新/逾期统计
    obl.insert_obligations("test_f8", r3["obligations"], "rule")
    items = obl.list_obligations("test_f8")
    check("3.1 台账写入", len(items) >= 2)
    # 造一条过去日期（逾期）和一条临近日期（提前量内）
    from datetime import date, timedelta
    past = (date.today() - timedelta(days=3)).isoformat()
    near = (date.today() + timedelta(days=2)).isoformat()
    obl.insert_obligations("test_f8", [
        {"oblig_type": "payment", "title": "逾期款", "amount": 100000, "due_date": past,
         "description": "自测"},
        {"oblig_type": "milestone", "title": "临近交付", "amount": None, "due_date": near,
         "description": "自测"},
    ], "rule")
    items = obl.list_obligations("test_f8")
    overdue = [o for o in items if o["due_date"] == past][0]
    near_n = [o for o in items if o["due_date"] == near][0]
    # 手动改提前量便于测试
    obl.update_obligation(overdue["oblig_id"], {"lead_days": 0})
    obl.update_obligation(near_n["oblig_id"], {"lead_days": 7})
    stats = obl.overdue_stats()
    check("3.2 逾期状态刷新", stats["overdue"] >= 1, f"stats={ {k: stats[k] for k in ('total','overdue','upcoming')} }")
    check("3.3 临近状态刷新", stats["upcoming"] >= 1)
    check("3.4 逾期金额统计", stats["overdue_amount"] >= 100000)

    # 4. 到期扫描生成通知+幂等
    recipients = obl._reminder_recipients()
    if not recipients:
        check("4.0 存在提醒接收人(admin/assistant)", False, "请先注册用户")
    else:
        before = obl.scan_and_notify()
        check("4.1 扫描生成通知", before["sent"] >= 1, str(before))
        notifs = obl.list_notifications if hasattr(obl, "list_notifications") else None
        from app import collab
        n1 = collab.list_notifications(recipients[0])
        hit = [n for n in n1 if "履约" in n["title"]]
        check("4.2 站内通知落collab通知表", len(hit) >= 1, f"通知数={len(hit)}")
        again = obl.scan_and_notify()
        check("4.3 同日幂等不重复提醒", again["sent"] == 0 and again["skipped"] >= 1, str(again))
        n2 = collab.list_notifications(recipients[0])
        check("4.4 通知数量未增长", len(n2) == len(n1))

    # 5. notify钩子
    fired = []
    obl.register_notify_hook("dingtalk", lambda node, channel: fired.append((channel, node["title"])))
    # 重置一条临近节点的last_notified再扫描
    near_n2 = [o for o in obl.list_obligations("test_f8") if o["due_date"] == near][0]
    from app import db
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE obligations SET last_notified='' WHERE oblig_id=%s",
                        (near_n2["oblig_id"],))
        conn.commit()
    obl.scan_and_notify()
    check("5.1 notify钩子触发(F7联调点)", len(fired) >= 1, str(fired))

    # 6. CSV导出
    csv_text = obl.export_csv(obl.list_obligations("test_f8"))
    check("6.1 CSV导出含表头与数据", "义务ID" in csv_text and "test_f8" in csv_text)

    print(f"\n结果：{len(PASS)} PASS, {len(FAIL)} FAIL")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
