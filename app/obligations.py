"""F8 履约期义务提醒模块

职责：
1. 义务节点抽取：LLM结构化抽取（chat_json，JSON Schema约束+重试），LLM未配置时
   降级为正则/关键词启发式抽取（日期+金额模式），保证基线可用。
2. 履约台账：obligations表的增查改、导出、逾期统计。
3. 提醒调度：轻量后台线程每日扫描（APScheduler未纳入依赖，取舍见模块尾部说明），
   按提前量生成站内通知（复用collab的notifications表），并预留notify回调钩子供F7接入。
"""
import csv
import io
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta

from . import db

# 义务类型：回款计划/交付里程碑/续约窗口/违约触发
OBLIGATION_TYPES = ("payment", "milestone", "renewal", "breach")
TYPE_NAMES = {"payment": "回款计划", "milestone": "交付里程碑",
              "renewal": "续约窗口", "breach": "违约触发"}
# 台账状态：待履行/已临近/已完成/已逾期
VALID_STATUS = {"pending", "upcoming", "done", "overdue"}
STATUS_NAMES = {"pending": "待履行", "upcoming": "已临近", "done": "已完成", "overdue": "已逾期"}

OBLIGATION_SCHEMA = {
    "type": "object",
    "required": ["obligations"],
    "properties": {
        "obligations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["oblig_type", "due_date"],
                "properties": {
                    "oblig_type": {"type": "string",
                                   "enum": ["payment", "milestone", "renewal", "breach"]},
                    "title": {"type": "string"},
                    "amount": {"type": "number"},
                    "due_date": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        }
    },
}

_LLM_SYSTEM = (
    "你是合同履约义务抽取助手。从合同文本中抽取关键履约义务节点，包括："
    "回款计划（oblig_type=payment，金额/日期）、交付里程碑（milestone）、"
    "续约窗口（renewal，如'合同到期前30日协商续约'）、违约条款触发条件（breach）。"
    "due_date统一为YYYY-MM-DD格式；相对期限（如'签署后30日内'）无法换算时留空；"
    "没有义务节点时返回空数组。"
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- LLM抽取通道 ----------

def llm_obligations(text: str) -> list:
    """LLM抽取义务节点，返回规整节点列表；未配置/失败抛llm异常（调用方降级）"""
    from . import llm
    if not llm.is_configured():
        raise llm.LLMNotConfigured("LLM未配置")
    snippet = text if len(text) <= 12000 else text[:9000] + "\n...\n" + text[-2500:]
    obj = llm.chat_json(_LLM_SYSTEM, f"合同文本：\n{snippet}\n请输出JSON。",
                        schema=OBLIGATION_SCHEMA)
    out = []
    for item in obj.get("obligations", [])[:100]:
        node = _normalize_node(item)
        if node:
            out.append(node)
    return out


def _normalize_node(item: dict) -> dict:
    """规整单个节点：类型合法化、日期格式化、金额转数值"""
    if not isinstance(item, dict):
        return None
    otype = str(item.get("oblig_type", "")).strip().lower()
    if otype not in OBLIGATION_TYPES:
        return None
    due = str(item.get("due_date", "")).strip()
    due = _format_date(due)
    amount = item.get("amount")
    try:
        amount = round(float(amount), 2) if amount not in (None, "",) else None
    except (TypeError, ValueError):
        amount = None
    return {
        "oblig_type": otype,
        "title": str(item.get("title", "")).strip()[:200] or TYPE_NAMES.get(otype, otype),
        "amount": amount,
        "due_date": due,
        "description": str(item.get("description", "")).strip()[:2000],
    }


def _format_date(s: str) -> str:
    """尽力把常见中文/数字日期规整为YYYY-MM-DD，失败返回空串"""
    s = s.strip().replace("年", "-").replace("月", "-").replace("日", "").replace("号", "")
    s = re.sub(r"[./]", "-", s).strip("-")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    # 无年份：默认当前年，若已过期超1年视为跨年文本，跳过（保守不猜）
    return ""


# ---------- 降级：正则/关键词启发式 ----------

_DATE_PAT = re.compile(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})[日号]?")
_AMOUNT_PAT = re.compile(r"([0-9,，]+(?:\.[0-9]+)?)\s*万?元")

# 关键词→类型（按优先级，先命中先归类）
_KEYWORDS = [
    ("renewal", ("续约", "续签", "重新签订", "顺延")),
    ("payment", ("回款", "付款", "支付", "货款", "到账", "结算")),
    ("milestone", ("交付", "验收", "里程碑", "竣工", "完成")),
    ("breach", ("违约", "逾期未", "滞纳", "赔偿金")),
]


def rule_obligations(text: str) -> list:
    """启发式抽取基线：按条款/句子切分，命中关键词的句子中找日期+金额，组成节点"""
    out = []
    # 按句切分（中文句号/分号/换行）
    sentences = re.split(r"[。；;\n]", text)
    for sent in sentences:
        sent = sent.strip()
        if not (20 <= len(sent) <= 300):
            continue
        otype = None
        for t, kws in _KEYWORDS:
            if any(k in sent for k in kws):
                otype = t
                break
        if not otype:
            continue
        dm = _DATE_PAT.search(sent)
        if not dm:
            continue  # 无具体日期不建节点（相对期限无法兜底判定）
        try:
            due = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3))).isoformat()
        except ValueError:
            continue
        amount = None
        am = _AMOUNT_PAT.search(sent)
        if am:
            val = am.group(1).replace(",", "").replace("，", "")
            try:
                amount = float(val) * (10000 if "万" in am.group(0) else 1)
                amount = round(amount, 2)
            except ValueError:
                amount = None
        out.append({
            "oblig_type": otype,
            "title": sent[:60],
            "amount": amount,
            "due_date": due,
            "description": sent[:500],
        })
    # 去重：同类型+同日期保留首条
    seen = set()
    uniq = []
    for n in out:
        key = (n["oblig_type"], n["due_date"])
        if key not in seen:
            seen.add(key)
            uniq.append(n)
    return uniq[:100]


def extract_obligations(text: str, contract_id: str = "") -> dict:
    """F8抽取总入口：LLM通道优先，未配置/失败降级启发式。返回{obligations, source, llm_status, llm_error}"""
    from . import llm
    if llm.is_configured():
        try:
            nodes = llm_obligations(text)
            return {"obligations": nodes, "source": "llm",
                    "llm_status": "ok", "llm_error": ""}
        except llm.LLError as e:
            nodes = rule_obligations(text)
            return {"obligations": nodes, "source": "rule",
                    "llm_status": "failed", "llm_error": str(e)}
    nodes = rule_obligations(text)
    return {"obligations": nodes, "source": "rule",
            "llm_status": "disabled", "llm_error": ""}


# ---------- 台账持久化 ----------

_OBLIG_COLS = ("oblig_id", "contract_id", "oblig_type", "title", "amount", "due_date",
               "lead_days", "status", "source", "description", "manually_edited",
               "last_notified", "created_at", "updated_at")


def _row_to_oblig(r):
    o = dict(zip(_OBLIG_COLS, r))
    o["amount"] = float(o["amount"]) if o["amount"] is not None else None
    o["manually_edited"] = bool(o["manually_edited"])
    return o


def insert_obligations(contract_id: str, nodes: list, source: str,
                       lead_days: int = 7, keep_manual: bool = True) -> int:
    """批量写入台账。keep_manual=True时不覆盖人工修正过的旧节点（按类型+日期匹配保留）"""
    now = _now()
    existing = list_obligations(contract_id)
    manual_keys = {(o["oblig_type"], o["due_date"])
                   for o in existing if o["manually_edited"]}
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            if not keep_manual:
                cur.execute("DELETE FROM obligations WHERE contract_id=%s", (contract_id,))
            for n in nodes:
                key = (n["oblig_type"], n["due_date"])
                if keep_manual and key in manual_keys:
                    continue  # 人工修正优先，不重复覆盖
                cur.execute(
                    """INSERT INTO obligations (oblig_id, contract_id, oblig_type, title, amount,
                       due_date, lead_days, status, source, description, manually_edited,
                       last_notified, created_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false,'',%s,%s)
                       ON CONFLICT (oblig_id) DO NOTHING""",
                    (uuid.uuid4().hex[:16], contract_id, n["oblig_type"], n["title"],
                     n["amount"], n["due_date"], lead_days, "pending", source,
                     n.get("description", ""), now, now))
        conn.commit()
    return len(nodes)


def list_obligations(contract_id: str = "") -> list:
    sql = "SELECT %s FROM obligations" % ",".join(_OBLIG_COLS)
    args = ()
    if contract_id:
        sql += " WHERE contract_id=%s"
        args = (contract_id,)
    sql += " ORDER BY due_date, oblig_type"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return [_row_to_oblig(r) for r in rows]


def update_obligation(oblig_id: str, data: dict) -> dict:
    """人工修正台账条目：允许改类型/标题/金额/到期日/提前量/状态/描述"""
    cur_row = get_obligation(oblig_id)
    if not cur_row:
        raise ValueError("义务节点不存在")
    fields = {}
    for k in ("oblig_type", "title", "amount", "due_date", "lead_days",
              "status", "description"):
        if k in data and data[k] is not None:
            fields[k] = data[k]
    if "oblig_type" in fields and fields["oblig_type"] not in OBLIGATION_TYPES:
        raise ValueError("无效义务类型")
    if "status" in fields and fields["status"] not in VALID_STATUS:
        raise ValueError("无效状态")
    if "due_date" in fields:
        fields["due_date"] = _format_date(str(fields["due_date"]))
        if not fields["due_date"]:
            raise ValueError("到期日格式无效，需YYYY-MM-DD")
    if "lead_days" in fields:
        fields["lead_days"] = max(0, int(fields["lead_days"]))
    if not fields:
        raise ValueError("无可更新字段")
    sets = ", ".join(f"{k}=%s" for k in fields)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE obligations SET {sets}, manually_edited=true, "
                        f"updated_at=%s WHERE oblig_id=%s",
                        tuple(fields.values()) + (_now(), oblig_id))
        conn.commit()
    return get_obligation(oblig_id)


def get_obligation(oblig_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM obligations WHERE oblig_id=%s"
                        % (",".join(_OBLIG_COLS), "%s"), (oblig_id,))
            r = cur.fetchone()
    return _row_to_oblig(r) if r else None


def refresh_status() -> int:
    """按当前日期刷新全部节点状态：过期未完成→overdue，临近（提前量内）→upcoming"""
    today = date.today()
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oblig_id, due_date, lead_days, status FROM obligations")
            for oid, due, lead, status in cur.fetchall():
                if status == "done" or not due:
                    continue
                try:
                    d = date.fromisoformat(due)
                except ValueError:
                    continue
                if d < today:
                    new = "overdue"
                elif d <= today + timedelta(days=int(lead or 0)):
                    new = "upcoming"
                else:
                    new = "pending"
                if new != status:
                    cur.execute("UPDATE obligations SET status=%s, updated_at=%s "
                                "WHERE oblig_id=%s", (new, _now(), oid))
                    n += 1
        conn.commit()
    return n


def overdue_stats() -> dict:
    """逾期/临近统计（全局）"""
    refresh_status()
    rows = list_obligations()
    stat = {"total": len(rows), "overdue": 0, "upcoming": 0, "done": 0, "pending": 0,
            "overdue_amount": 0.0, "overdue_amount_known": 0,
            "by_type": {}}
    for o in rows:
        stat["by_type"].setdefault(o["oblig_type"], {"total": 0, "overdue": 0})
        stat["by_type"][o["oblig_type"]]["total"] += 1
        stat[o["status"]] = stat.get(o["status"], 0) + 1
        stat["by_type"][o["oblig_type"]][o["status"]] = \
            stat["by_type"][o["oblig_type"]].get(o["status"], 0) + 1
        if o["status"] == "overdue":
            if o["amount"] is not None:
                stat["overdue_amount"] += o["amount"]
                stat["overdue_amount_known"] += 1
    return stat


# ---------- 导出 ----------

def export_csv(obligations: list) -> str:
    """台账导出为CSV字符串（UTF-8带BOM，Excel可直接打开）"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["义务ID", "合同ID", "类型", "标题", "金额", "到期日",
                     "提前量(天)", "状态", "来源", "描述"])
    for o in obligations:
        writer.writerow([o["oblig_id"], o["contract_id"], TYPE_NAMES.get(o["oblig_type"], o["oblig_type"]),
                         o["title"], o["amount"] if o["amount"] is not None else "",
                         o["due_date"], o["lead_days"], STATUS_NAMES.get(o["status"], o["status"]),
                         "LLM" if o["source"] == "llm" else "规则",
                         o["description"]])
    return "\ufeff" + buf.getvalue()


# ---------- 提醒调度 ----------

# notify回调钩子（F7联调点）：签名 notify_hook(node: dict, channel: str) -> None
# node为义务节点dict（含contract_id/oblig_type/title/amount/due_date/lead_days），
# channel为通知通道名：站内固定为"inapp"，F7接入钉钉后注册"dinding"通道回调。
# F7接入方式：obligations.register_notify_hook("dingtalk", my_func)，扫描到临近节点时
# 会对每个已注册通道回调一次（站内通道不回调钩子，直接写notifications表）。
_NOTIFY_HOOKS = {}


def register_notify_hook(channel: str, func):
    """注册外发通知钩子：func(node: dict, channel: str) -> None；抛异常不影响主流程"""
    _NOTIFY_HOOKS[channel] = func


def _fire_hooks(node: dict):
    for channel, func in list(_NOTIFY_HOOKS.items()):
        try:
            func(node, channel)
        except Exception:
            pass  # 外发失败不阻塞站内通知与扫描


def _notify_inapp(username: str, node: dict, overdue: bool):
    """复用collab的notifications表发站内通知（直接写表，避免引入循环依赖）"""
    tname = TYPE_NAMES.get(node["oblig_type"], node["oblig_type"])
    if overdue:
        title = "履约义务已逾期"
        content = f"合同{node['contract_id']}的{tname}《{node['title']}》已于{node['due_date']}到期未履行"
    else:
        title = "履约义务到期提醒"
        content = (f"合同{node['contract_id']}的{tname}《{node['title']}》"
                   f"将于{node['due_date']}到期（提前{node['lead_days']}天提醒）")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notifications (notify_id, username, title, content, read, time) "
                "VALUES (%s,%s,%s,%s,false,%s)",
                (uuid.uuid4().hex[:10], username, title, content, _now()))
        conn.commit()


def _reminder_recipients() -> list:
    """提醒接收人：全部admin与assistant角色用户（台账管理角色）"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE role IN ('admin','assistant')")
            return [r[0] for r in cur.fetchall()]


def scan_and_notify() -> dict:
    """扫描到期/临近节点并生成通知（幂等：同一天同一节点只提醒一次，按last_notified记录）"""
    today = date.today().isoformat()
    refresh_status()
    sent = skipped = 0
    recipients = _reminder_recipients()
    rows = list_obligations()
    for node in rows:
        if node["status"] not in ("overdue", "upcoming"):
            continue
        if node["last_notified"] == today:
            skipped += 1
            continue  # 幂等：今日已提醒
        overdue = node["status"] == "overdue"
        for username in recipients:
            _notify_inapp(username, node, overdue)
        _fire_hooks(node)
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE obligations SET last_notified=%s, updated_at=%s "
                            "WHERE oblig_id=%s", (today, _now(), node["oblig_id"]))
            conn.commit()
        sent += 1
    return {"sent": sent, "skipped": skipped, "recipients": len(recipients)}


def _scheduler_loop(interval_hours: int = 24):
    """后台线程：启动即扫一次，之后每interval_hours小时轮询一次"""
    while True:
        try:
            scan_and_notify()
        except Exception:
            pass
        time.sleep(interval_hours * 3600)


_scheduler_started = False


def start_scheduler(interval_hours: int = 24):
    """启动提醒调度（进程内只启动一次）。轻量daemon线程实现，不引入APScheduler依赖"""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, args=(interval_hours,),
                         name="obligation-reminder", daemon=True)
    t.start()

# 调度取舍说明：方案建议APScheduler+PostgreSQL任务表；requirements未含APScheduler，
# 且单实例部署下每日一次扫描用daemon线程+sleep即可满足，少一个依赖更利于私有化
# 离线部署（F3）。若未来多实例部署，需改用APScheduler+数据库锁（advisory lock）防
# 重复扫描，接口scan_and_notify()已独立可测，替换成本仅限本文件。
