"""用户与权限模块：注册/登录/RBAC角色管理（PostgreSQL存储）
角色定义（与方案1.3一致）：
- admin 系统管理员：用户管理、审计日志
- kb_admin 知识库管理员：规则库、法规库维护
- lawyer 执业律师：复核审查结果、出具修改建议
- assistant 律师助理/办公人员：上传合同、发起审查、导出报告
"""
import hashlib
import secrets
import threading
import time

from . import db

_fail_lock = threading.Lock()
# 登录失败锁定仅存内存（临时性状态）
TOKEN_TTL = 2 * 3600  # 三期安全加固：Token有效期收紧为2小时

# 三期安全加固：连续登录失败锁定
MAX_FAIL = 5
LOCK_SECONDS = 600  # 10分钟
_FAILS: dict = {}  # username -> {"fail_count": n, "lock_until": ts}

ROLE_NAMES = {
    "admin": "系统管理员",
    "manager": "法务负责人",
    "kb_admin": "知识库管理员",
    "lawyer": "执业律师",
    "assistant": "律师助理",
}

# 角色权限矩阵（方案1.3）；qc_manage：质检抽样/复核（F9），manager与admin可操作
ROLE_PERMS = {
    "admin": {"upload", "review", "report", "rules_view", "rules_edit", "users_manage", "audit", "qc_manage"},
    "manager": {"upload", "review", "report", "rules_view", "audit", "qc_manage"},
    "kb_admin": {"rules_view", "rules_edit"},
    "lawyer": {"upload", "review", "report", "rules_view"},
    "assistant": {"upload", "review", "report", "rules_view"},
}


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100000).hex()


def register(username: str, password: str, role: str = "assistant"):
    """注册新用户，首个注册用户自动成为系统管理员"""
    if not username or len(username) < 3:
        raise ValueError("用户名至少3个字符")
    if len(password) < 6:
        raise ValueError("密码至少6位")
    if role not in ROLE_NAMES:
        raise ValueError("无效角色")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users")
            has_users = cur.fetchone()[0] > 0
            if not has_users:
                role = "admin"  # 首个用户为系统管理员
            salt = secrets.token_hex(16)
            try:
                cur.execute(
                    "INSERT INTO users (username, salt, password, role, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (username, salt, _hash_password(password, salt), role,
                     time.strftime("%Y-%m-%d %H:%M:%S")))
            except Exception:
                raise ValueError("用户名已存在")
        conn.commit()
    return {"username": username, "role": role}


def _is_locked(username: str):
    with _fail_lock:
        f = _FAILS.get(username)
        if f and f["lock_until"] > time.time():
            remain = int(f["lock_until"] - time.time())
            return True, remain
    return False, 0


def _record_fail(username: str):
    with _fail_lock:
        f = _FAILS.setdefault(username, {"fail_count": 0, "lock_until": 0})
        f["fail_count"] += 1
        if f["fail_count"] >= MAX_FAIL:
            f["lock_until"] = time.time() + LOCK_SECONDS


def _clear_fail(username: str):
    with _fail_lock:
        _FAILS.pop(username, None)


def login(username: str, password: str):
    locked, remain = _is_locked(username)
    if locked:
        raise ValueError(f"连续登录失败次数过多，账号已锁定，请{remain}秒后重试")
    u = _get_user_row(username)
    if not u or _hash_password(password, u["salt"]) != u["password"]:
        _record_fail(username)
        with _fail_lock:
            n = _FAILS.get(username, {}).get("fail_count", 0)
        left = MAX_FAIL - n
        tip = f"用户名或密码错误，还可尝试{left}次" if left > 0 else "已触发锁定，10分钟后可重试"
        raise ValueError(tip)
    _clear_fail(username)
    token = secrets.token_hex(24)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sessions (token, username, role, expire) VALUES (%s,%s,%s,%s)",
                        (token, username, u["role"], time.time() + TOKEN_TTL))
        conn.commit()
    return {"token": token, "username": username, "role": u["role"],
            "role_name": ROLE_NAMES[u["role"]]}


def logout(token: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
        conn.commit()


def current_user(token: str):
    """校验token，返回{username, role}，无效返回None"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, role, expire FROM sessions WHERE token=%s", (token,))
            row = cur.fetchone()
    if not row:
        return None
    username, role, expire = row
    if expire < time.time():
        logout(token)
        return None
    return {"username": username, "role": role}


def has_perm(role: str, perm: str) -> bool:
    return perm in ROLE_PERMS.get(role, set())


def _get_user_row(username: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, salt, password, role, created_at FROM users WHERE username=%s",
                        (username,))
            row = cur.fetchone()
    if not row:
        return None
    return {"username": row[0], "salt": row[1], "password": row[2],
            "role": row[3], "created_at": row[4]}


def list_users():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, role, created_at FROM users ORDER BY created_at")
            rows = cur.fetchall()
    return [{"username": r[0], "role": r[1], "role_name": ROLE_NAMES.get(r[1], r[1]),
             "created_at": r[2]} for r in rows]


def set_role(username: str, role: str):
    if role not in ROLE_NAMES:
        raise ValueError("无效角色")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role=%s WHERE username=%s", (role, username))
            if cur.rowcount == 0:
                raise ValueError("用户不存在")
        conn.commit()


def delete_user(username: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE username=%s", (username,))
            row = cur.fetchone()
            if not row:
                raise ValueError("用户不存在")
            if row[0] == "admin":
                cur.execute("SELECT count(*) FROM users WHERE role='admin'")
                if cur.fetchone()[0] <= 1:
                    raise ValueError("不能删除最后一个系统管理员")
            cur.execute("DELETE FROM users WHERE username=%s", (username,))
        conn.commit()


def reset_password(username: str, new_password: str):
    if len(new_password) < 6:
        raise ValueError("密码至少6位")
    salt = secrets.token_hex(16)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET salt=%s, password=%s WHERE username=%s",
                        (salt, _hash_password(new_password, salt), username))
            if cur.rowcount == 0:
                raise ValueError("用户不存在")
        conn.commit()


def change_password(username: str, old_password: str, new_password: str):
    """用户本人修改密码：校验旧密码，成功后清除该用户全部会话"""
    if len(new_password) < 6:
        raise ValueError("新密码至少6位")
    if old_password == new_password:
        raise ValueError("新密码不能与旧密码相同")
    u = _get_user_row(username)
    if not u or _hash_password(old_password, u["salt"]) != u["password"]:
        raise ValueError("旧密码错误")
    salt = secrets.token_hex(16)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 旧密码改掉后，踢掉该用户所有在线会话
            cur.execute("DELETE FROM sessions WHERE username=%s", (username,))
            cur.execute("UPDATE users SET salt=%s, password=%s WHERE username=%s",
                        (salt, _hash_password(new_password, salt), username))
        conn.commit()
    return {"ok": True}
