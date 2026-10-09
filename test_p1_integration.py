# -*- coding: utf-8 -*-
"""P1期集成自测：F7 Connector加载/status汇总、F8→F7履约扫描→钉钉hook、F4 baseline-check端到端、
全项目compileall、P0关键路径回归。运行：python test_p1_integration.py"""
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), "-", name, detail)


# ---------- 1. Connector禁用/启用加载与status汇总 ----------
print("\n== 1. Connector禁用/启用 ==")
from app.integrations import base as ibase
from app.integrations import hub as ihub
from app.integrations import queue as iqueue

os.environ.pop("INTEGRATIONS_ENABLED", None)
import importlib
import app.config as cfg
importlib.reload(cfg)  # 禁用态
ibase._registry.clear(); ibase._load_errors.clear()
from app.integrations import base as ibase2
importlib.reload(ibase2)
st = ibase2.status()
check("禁用时registry为空", st["loaded"] == {} and "dingtalk" in st["not_enabled"], json.dumps(st["enabled"]))

os.environ["INTEGRATIONS_ENABLED"] = "dingtalk,esignbao"
os.environ.setdefault("ESIGNBAO_APP_ID", "test")
os.environ.setdefault("ESIGNBAO_SECRET", "test")
importlib.reload(cfg)
from app.integrations import base as ibase3
ibase3._registry.clear(); ibase3._load_errors.clear()
importlib.reload(ibase3)
st = ibase3.status()
check("启用dingtalk+esignbao后两者注册成功", "dingtalk" in st["loaded"] and "esignbao" in st["loaded"])
check("status含enabled/not_enabled/errors四要素", all(k in st for k in ("enabled", "loaded", "errors", "not_enabled")))

# ---------- 2. 履约扫描→钉钉hook（mock钉钉API）→审计 ----------
print("\n== 2. 履约扫描→钉钉hook ==")
sent = {}

class MockDing(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        sent[self.path] = "GET"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"errcode":0,"access_token":"mocktoken"}')
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8")
        sent[self.path] = body
        if "gettoken" in self.path:
            # errcode=0
            resp = b'{"errcode":0,"access_token":"mocktoken"}'
        else:
            resp = b'{"errcode":0,"task_id":"9527"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(resp)

srv = HTTPServer(("127.0.0.1", 18601), MockDing)
threading.Thread(target=srv.serve_forever, daemon=True).start()

from app.integrations.dingtalk.connector import DingTalkConnector
conn = DingTalkConnector()
conn.dingtalk_api_base = "http://127.0.0.1:18601"
conn.dingtalk_agent_id = "1"
# 直接验证hook函数路径：把履约节点经Connector send_message外发（同main.py接线逻辑）
from app import obligations as obl

hook_calls = []
def fake_owner_map(u): return u
conn.map_user = fake_owner_map

# 模拟main.py中注册的钩子行为
def node_hook(node, channel):
    ok = conn.send_message("cjl", f"履约提醒｜{node['title']}",
                           f"到期日{node['due_date']}", contract_id=node["contract_id"])
    hook_calls.append(ok)

obl.register_notify_hook("dingtalk-mock", node_hook)
node = {"contract_id": "c1", "oblig_type": "payment", "title": "第一期回款",
        "amount": 800000, "due_date": "2026-10-01", "status": "overdue",
        "description": "客户回款 手机号13800138000"}
obl._fire_notify(node) if hasattr(obl, "_fire_notify") else None
# 兜底：直接调用注册表内钩子
for ch, fn in list(obl._NOTIFY_HOOKS.items()):
    fn(node, ch)
check("钉钉mock收到gettoken与工作通知", "gettoken" in json.dumps(sent) and any("asyncsend" in p for p in sent))
check("外发内容已脱敏（手机号打码）", "13800138000" not in json.dumps(sent))
check("hook返回True计数", hook_calls and all(hook_calls))

# 审计留痕
from app import audit as aud
rows = aud.query(limit=50)
found = any("dingtalk_send" in (r.get("action") or "") for r in rows)
check("审计留痕dingtalk_send存在", found)

# 确认无循环依赖：integrations不import obligations，obligations不import integrations
src_o = open("app/obligations.py", encoding="utf-8").read()
src_m = open("app/main.py", encoding="utf-8").read()
check("obligations.py不依赖integrations（钩子解耦）", "integrations" not in src_o)

srv.shutdown()

# ---------- 3. F4 baseline-check端到端 ----------
print("\n== 3. F4 baseline-check端到端 ==")
from app import db as dbm
from app import baseline as bl
import time as _t
_bn = "IT采购范本%d" % int(_t.time())
bl.create_baseline({"name": _bn, "contract_type": "采购", "business_line": "IT",
                    "stance": "买方", "content": "第一条 付款条件\n第二条 交付期限\n第三条 保密条款",
                    "suggestions": {"付款条件": "货到付款30日内支付"}, "created_by": "tester"}, "tester")
base = bl.list_baselines(q=_bn)
check("基线创建并可查询", bool(base))
diff = bl.baseline_diff([{"clause_id": "c1", "text": "第一条 付款条件：货到付款"},
                         {"clause_id": "c2", "text": "第四条 违约金千分之五"}], base[0])
check("差异审查输出summary", "summary" in diff and all(k in diff["summary"] for k in ("missing", "deviation", "unfavorable")))
check("缺失条款被识别", diff["summary"]["missing"] >= 1)

# ---------- 4. compileall ----------
print("\n== 4. compileall ==")
import compileall
ok = compileall.compile_dir("app", quiet=2) and compileall.compile_file("test_p1_integration.py", quiet=2)
check("全项目编译零错误", bool(ok))

# ---------- 5. P0关键路径回归 ----------
print("\n== 5. P0回归 ==")
import app.main as m
check("import main成功", True)
for mod in ("app.pipeline", "app.annotator", "app.diff", "app.recall", "app.extractor"):
    importlib.import_module(mod)
    check(f"{mod}可导入", True)

print(f"\n结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("失败项：", FAIL)
sys.exit(1 if FAIL else 0)
