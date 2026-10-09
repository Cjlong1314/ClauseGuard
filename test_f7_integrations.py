"""F7集成适配层自测：mock钉钉API（本地http.server）验证
签名/脱敏/事件派发/失败重试落库/禁用不发送
运行：python test_f7_integrations.py
"""
import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, ".")

# 先设置环境变量再导入config
import os
MOCK_PORT = 18432
os.environ["DINGTALK_API_BASE"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["DINGTALK_APP_KEY"] = "test_key"
os.environ["DINGTALK_APP_SECRET"] = "test_secret"
os.environ["DINGTALK_AGENT_ID"] = "123"
os.environ["DINGTALK_CALLBACK_TOKEN"] = "cb_token"
os.environ["INTEGRATIONS_MAX_RETRY"] = "2"
os.environ["INTEGRATIONS_RETRY_INTERVAL"] = "1"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}: {name} {detail}")


# ---------- mock钉钉API ----------
sent_notifications = []


class MockDingTalk(BaseHTTPRequestHandler):
    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "/gettoken" in self.path:
            self._json({"errcode": 0, "access_token": "mock_token", "expires_in": 7200})
        else:
            self._json({"errcode": -1})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if "asyncsend_v2" in self.path:
            sent_notifications.append(json.loads(raw))
            self._json({"errcode": 0, "task_id": 999})
        else:
            self._json({"errcode": -1})

    def log_message(self, *a):
        pass


server = HTTPServer(("127.0.0.1", MOCK_PORT), MockDingTalk)
threading.Thread(target=server.serve_forever, daemon=True).start()

from app import config, db
from app.integrations.base import ConnectorBase, get_connector, enabled_connectors
from app.integrations.dingtalk.connector import DingTalkConnector, mask_sensitive
from app.integrations import hub, queue as outbox
from app.integrations import events as ev

# ---------- 1. 脱敏 ----------
t = mask_sensitive("联系张三13912345678，身份证110101199001011234，卡号6222020200112233445")
check("脱敏手机号", "139****5678" in t)
check("脱敏身份证", "1101**********1234" in t)
check("脱敏银行卡", "6222*********3445" in t)
check("普通文本不受影响", mask_sensitive("合同金额50万") == "合同金额50万")

# ---------- 2. Connector加载与禁用 ----------
os.environ.pop("INTEGRATIONS_ENABLED", None)
check("禁用时enabled为空", enabled_connectors() == [])
check("禁用时get_connector为None", get_connector("dingtalk") is None)
os.environ["INTEGRATIONS_ENABLED"] = "dingtalk"
conn = get_connector("dingtalk")
check("启用后加载钉钉Connector", isinstance(conn, DingTalkConnector))

# ---------- 3. access_token与工作通知 ----------
token = conn.get_access_token()
check("mock获取access_token", token == "mock_token")
r = conn.send_work_notification(["userA"], "**测试**通知", "标题")
check("发送工作通知errcode=0", r["errcode"] == 0 and sent_notifications[-1]["agent_id"] == 123)
check("通知走action_card", sent_notifications[-1]["msg"]["msgtype"] == "action_card")

# ---------- 4. 事件派发（任务分配→钉钉） ----------
before = len(sent_notifications)
ev_obj = hub.publish("task.assigned", {"task_id": "t1", "title": "采购合同审查13912345678",
                                       "contract_id": "c1", "assignee": "userA",
                                       "due_date": "2026-12-01", "contract_filename": "采购.docx"},
                     actor="admin", contract_id="c1")
time.sleep(1.0)  # 等待异步派发
check("事件发布返回event_id", bool(ev_obj.get("event_id")))
check("事件派发触发钉钉推送", len(sent_notifications) == before + 1)
pushed = json.dumps(sent_notifications[-1], ensure_ascii=False)
check("外发内容已脱敏", "139****5678" in pushed and "13912345678" not in pushed)

# ---------- 5. 未知事件与禁用事件类型 ----------
check("未知事件类型被忽略", hub.publish("bogus.event", {}) is None)
check("不支持的事件返回False", conn.send_event({"event_type": "esign.status_changed", "payload": {}}) is False)

# ---------- 6. 失败重试落库 ----------
class FailConnector(ConnectorBase):
    name = "failtest"
    attempts = 0
    def send_event(self, event):
        FailConnector.attempts += 1
        raise RuntimeError("网络不可达")
    def send_message(self, *a, **k):
        return True
    def health_check(self):
        return {"ok": True}
fc = FailConnector.__new__(FailConnector)  # 跳过配置校验
ev_fail = {"event_id": "f7fail01", "event_type": "review.completed", "contract_id": "c1",
           "payload": {}}
outbox.enqueue_outbox("failtest", ev_fail["event_type"], ev_fail, error="网络不可达")
items = [i for i in outbox.outbox_items("pending") if i["payload"].get("event_id") == "f7fail01"]
check("失败事件落重试队列", len(items) == 1)

# 用monkeypatch让重试成功，验证process_pending出队
real_get = outbox.get_connector
outbox.get_connector = lambda name: {"failtest": _OkConn()}[name] if name == "failtest" else real_get(name)
class _OkConn:
    def send_event(self, event):
        return True
stats = outbox.process_pending()
items_after = [i for i in outbox.outbox_items("pending") if i["payload"].get("event_id") == "f7fail01"]
check("重试成功后出队", len(items_after) == 0, str(stats))
def db_audit_tail():
    from app import audit
    return audit.query(limit=30)
check("重试过程写审计", any(a["action"] == "outbox_retry" and a["username"] == "integration"
                        for a in db_audit_tail()))
check("outbox_retry审计存在", any(a["action"] == "outbox_retry" for a in db_audit_tail()))
outbox.get_connector = real_get

# 重试耗尽→failed终态
class AlwaysFail:
    def send_event(self, event):
        raise RuntimeError("持续失败")
from app import config as cfg
cfg.INTEGRATIONS_MAX_RETRY = 2  # 调低重试上限（config模块单例，与outbox共用）


def reset_next_retry():
    """把pending条目的next_retry拨到过去，绕过重试间隔门控（自测用）"""
    with db.get_conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE integration_outbox SET next_retry=0 WHERE status='pending'")
        c.commit()

ev_fail2 = {"event_id": "f7fail02", "event_type": "review.completed", "contract_id": "c1",
            "payload": {}}
outbox.enqueue_outbox("failtest", ev_fail2["event_type"], ev_fail2)
outbox.get_connector = lambda name: AlwaysFail() if name == "failtest" else real_get(name)
reset_next_retry()
outbox.process_pending()
reset_next_retry()
outbox.process_pending()
outbox.get_connector = real_get
failed_items = [i for i in outbox.outbox_items("failed") if i["payload"].get("event_id") == "f7fail02"]
check("重试耗尽标记failed", len(failed_items) == 1)

# ---------- 7. 禁用时不发送 ----------
os.environ["INTEGRATIONS_ENABLED"] = ""
before = len(sent_notifications)
hub.publish("task.assigned", {"task_id": "t2", "title": "x", "assignee": "userA"})
time.sleep(0.8)
check("禁用Connector后不再发送", len(sent_notifications) == before)

# ---------- 8. 回调验签 ----------
body = json.dumps({"event_type": "esign.status_changed", "contract_id": "c1"})
ts, nonce = "1690000000", "abc123"
sig = hmac.new(b"cb_token", f"{ts}\n{nonce}\n{body}".encode(), hashlib.sha256).hexdigest()
check("回调验签正确签名通过", conn.verify_callback_signature(ts, nonce, body, sig))
check("回调验签错误签名拒绝", not conn.verify_callback_signature(ts, nonce, body, "bad" + sig[3:]))

# ---------- 9. 健康检查 ----------
hc = conn.health_check()
check("health_check探活成功", hc.get("ok") is True)

server.shutdown()
print(f"\n结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
sys.exit(1 if FAIL else 0)
