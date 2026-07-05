import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Cookie, Depends, HTTPException, Request, status

from .database import Database


ALL_PAGES = [
    "dashboard", "persons", "timeline", "map", "search", "review", "sources",
    "tasks", "users", "config", "audit",
]
DEFAULT_USER_PAGES = ["dashboard", "persons", "timeline", "map", "search"]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_value, digest_value = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_value.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def parse_password_file(path: Path) -> List[Tuple[str, str, str]]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# 格式: username:password:role  (role 取值: admin | user)\n"
            "# 请在首次登录后修改默认密码。\n"
            "admin:admin123:admin\n",
            encoding="utf-8",
        )
    users: List[Tuple[str, str, str]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 2)
        if len(parts) != 3 or parts[2] not in {"admin", "user"} or not parts[0] or not parts[1]:
            raise ValueError("password.txt 第 {} 行格式无效".format(number))
        users.append((parts[0].strip(), parts[1], parts[2].strip()))
    return users


def sync_users(db: Database, password_file: Path) -> None:
    now = utc_now()
    with db.transaction() as connection:
        for username, password, role in parse_password_file(password_file):
            source_hash = hashlib.sha256((username + "\0" + password + "\0" + role).encode("utf-8")).hexdigest()
            row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                cursor = connection.execute(
                    "INSERT INTO users(username,password_hash,role,password_source_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (username, hash_password(password), role, source_hash, now, now),
                )
                if role == "user":
                    connection.executemany(
                        "INSERT INTO page_permissions(user_id,page_key,can_access) VALUES(?,?,1)",
                        [(cursor.lastrowid, page) for page in DEFAULT_USER_PAGES],
                    )
            elif row["password_source_hash"] != source_hash or row["role"] != role:
                connection.execute(
                    "UPDATE users SET password_hash=?,role=?,password_source_hash=?,updated_at=? WHERE id=?",
                    (hash_password(password), role, source_hash, now, row["id"]),
                )
                connection.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))


def create_session(db: Database, user_id: int, hours: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO sessions(user_id,token_hash,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?)",
        (user_id, token_hash, (now + timedelta(hours=hours)).replace(microsecond=0).isoformat(), now.replace(microsecond=0).isoformat(), now.replace(microsecond=0).isoformat()),
    )
    return token


def get_session_user(db: Database, token: str) -> Optional[Dict[str, Any]]:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = utc_now()
    row = db.fetch_one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1",
        (token_hash, now),
    )
    if row:
        db.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, token_hash))
    return row


def revoke_session(db: Database, token: str) -> None:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))


def user_pages(db: Database, user: Dict[str, Any]) -> List[str]:
    if user["role"] == "admin":
        return list(ALL_PAGES)
    rows = db.fetch_all("SELECT page_key FROM page_permissions WHERE user_id=? AND can_access=1", (user["id"],))
    return [row["page_key"] for row in rows]


def current_user(request: Request, pfts_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    if not pfts_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    user = get_session_user(request.app.state.db, pfts_session)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已失效")
    user["pages"] = user_pages(request.app.state.db, user)
    return user


def require_page(page_key: str):
    def dependency(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        if page_key not in user["pages"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有访问该页面的权限")
        return user
    return dependency


def require_admin(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可执行此操作")
    return user

