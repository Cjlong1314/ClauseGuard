"""e签宝Connector自测（F7第二部分）

mock e签宝开放平台（本地http.server）验证：token获取与缓存、流程状态查询、
回调验签（正确/错误签名）、事件派发（非esign事件跳过、失败落重试队列）、
禁用不发送。运行：python test_f7_esignbao.py
"""
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["ESIGNBAO_APP_ID"] = "test_app_id"
os.environ["ESIGNBAO_SECRET"] = "test_secret"
os.environ["ESIGNBAO_CALLBACK_SECRET"] = "cb_secret"
os.environ["ESIGNBAO_API_BASE"] = "http://127.0.0.1:18601"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'} - {name} {detail}")


# ---------- mock e签宝API ----------
TOKEN_COUNT = {"n": 0}


class MockEsign(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        if self.path == "/v1/oauth2/token":
            if body.get("app_id") != "test_app_id" or body.get("secret") != "test_secret":
                self._send({"code": 1430, "msg": "app_id/secret不匹配"})
                return
            TOKEN_COUNT["n"] += 1
            self._send({"code": 0, "msg": "成功", "access_token": "tok_123",
                        "expires_in": 7200})
            return
        self._send({"code": 404, "msg": "not found"}, 404)

    def do_GET(self):
        if self.path.startswith("/v1/signflows/"):
            auth = self.headers.get("X-Tsign-Open-Api-Auth", "")
            if auth != "Bearer tok_123":
                self._send({"code": 1431, "msg": "token无效"})
                return
            flow_id = self.path.rsplit("/", 1)[-1]
            if "FAIL" in flow_id.upper():
                # 构造业务失败场景：流程不存在
                self._send({"code": 1432, "msg": "流程不存在"})
                return
            self._send({"code": 0, "msg": "成功",
                        "data": {"signFlowStatus": 2, "flowId": flow_id,
                                 "flowDescription": "采购合同签署"}})
            return
        self._send({"code": 404, "msg": "not found"}, 404)


server = HTTPServer(("127.0.0.1", 18601), MockEsign)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.3)

from app.integrations.base import ConnectorBase, enabled_connectors, get_connector
from app import config

# 清理已加载注册表，保证用当前环境变量重新实例化
import app.integrations.base as base_mod
base_mod._registry.clear()
base_mod._load_errors.clear()

os.environ["INTEGRATIONS_ENABLED"] = "esignbao"
conn = get_connector("esignbao")
check("Connector启用加载", conn is not None and conn.name == "esignbao",
      str(type(conn)))

# 1. token获取
tok1 = conn.get_access_token()
check("access_token获取", tok1 == "tok_123")
tok2 = conn.get_access_token()
check("token缓存（未重复请求）", tok2 == "tok_123" and TOKEN_COUNT["n"] == 1,
      f"token请求次数={TOKEN_COUNT['n']}")

# 2. 状态查询（signFlowStatus=2→COMPLETED）
flow = conn.query_flow("FLOW_ABC_123")
check("签署状态查询", flow["status"] == "COMPLETED" and flow["raw"]["flowId"] == "FLOW_ABC_123",
      str(flow["status"]))

# 3. 非esign事件返回False跳过
check("非esign事件跳过",
      conn.send_event({"event_id": "e1", "event_type": "task.assigned", "payload": {}}) is False)

# 4. esign.status_changed事件处理（内部走查询+审计回写）
ok = conn.send_event({"event_id": "e2", "event_type": "esign.status_changed",
                      "contract_id": "c1", "actor": "esignbao",
                      "payload": {"flow_id": "FLOW_ABC_123", "status": "COMPLETED"}})
check("esign.status_changed处理成功", ok is True)

# 5. 回调验签
body = json.dumps({"flowId": "FLOW_X", "signFlowStatus": 2, "contract_id": "c1"})
sig = hmac.new(b"cb_secret", body.encode("utf-8"), hashlib.sha256).hexdigest()
check("回调验签-正确签名", conn.verify_callback_signature(body, sig) is True)
check("回调验签-错误签名", conn.verify_callback_signature(body, "deadbeef") is False)

# 6. 失败落重试队列：让查询走一个不存在的flow（mock返回404→RuntimeError→总线落outbox）
from app.integrations import hub
from app.integrations.queue import outbox_items
hub.publish("esign.status_changed", {"flow_id": "FLOW_FAIL_404", "status": "SIGNING"},
            actor="esignbao", contract_id="c1")
time.sleep(1.5)  # 派发是异步线程
items = outbox_items(status="pending", limit=10)
hit = [i for i in items if i["event_type"] == "esign.status_changed"
       and i["payload"].get("payload", {}).get("flow_id") == "FLOW_FAIL_404"]
check("失败落重试队列", len(hit) >= 1, f"pending条数={len(items)}")

# 7. 禁用不发送：禁用后publish不派发
os.environ["INTEGRATIONS_ENABLED"] = ""
before = TOKEN_COUNT["n"]
hub.publish("esign.status_changed", {"flow_id": "FLOW_DISABLED", "status": "COMPLETED"},
            actor="esignbao", contract_id="c1")
time.sleep(0.8)
check("禁用后不发送", TOKEN_COUNT["n"] == before,
      f"token请求次数变化={TOKEN_COUNT['n'] - before}")

# 8. health_check
os.environ["INTEGRATIONS_ENABLED"] = "esignbao"
base_mod._registry.clear()
conn2 = get_connector("esignbao")
h = conn2.health_check()
check("health_check", h.get("ok") is True, h.get("detail", ""))

# 9. send_message（走站内通知表，需PG可达）
try:
    r = conn2.send_message("admin", "签署提醒", "请及时查看签署进度", contract_id="c1")
    check("send_message站内通知", r is True)
except Exception as e:
    check("send_message站内通知", False, f"异常：{e}")

server.shutdown()
print(f"\n结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("失败项：", FAIL)
    raise SystemExit(1)
