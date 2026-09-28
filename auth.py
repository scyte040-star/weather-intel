"""Accounts and sessions: SQLite users, scrypt password hashes, hashed session tokens."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB = Path(__file__).parent / "data.db"
SESSION_S = 30 * 86400
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@contextmanager
def tx():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # commit on success, roll back on error
            yield conn
    finally:
        conn.close()


def hash_pw(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_pw(password, stored):
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(hash_pw(password, bytes.fromhex(salt)).split("$")[2], digest)


DUMMY_HASH = hash_pw("no such user")  # checked for unknown emails too, so response time doesn't reveal accounts


def init():
    with tx() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user', disabled INTEGER NOT NULL DEFAULT 0, alert TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
        """)
        # The admin account comes from the environment, so it survives hosts that wipe the disk on restart.
        email, password = os.environ.get("ADMIN_EMAIL", "").strip().lower(), os.environ.get("ADMIN_PASSWORD", "")
        if email and password:
            db.execute("""INSERT INTO users (email, name, pw, role, created) VALUES (?, 'Administrator', ?, 'admin', ?)
                          ON CONFLICT(email) DO UPDATE SET pw = excluded.pw, role = 'admin', disabled = 0""",
                       (email, hash_pw(password), time.time()))


def public(row):
    return row and {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"],
                    "disabled": bool(row["disabled"]), "created": row["created"],
                    "alert": json.loads(row["alert"]) if row["alert"] else None}


def check_new_password(password):
    return None if 8 <= len(password) <= 128 else "Use a password of 8 to 128 characters."


def signup(name, email, password):
    """-> (user, error)"""
    name, email = name.strip()[:80], email.strip().lower()
    if not name:
        return None, "Enter your name."
    if not EMAIL_RE.match(email) or len(email) > 254:
        return None, "Enter a valid email address."
    if error := check_new_password(password):
        return None, error
    try:
        with tx() as db:
            cur = db.execute("INSERT INTO users (email, name, pw, created) VALUES (?, ?, ?, ?)",
                             (email, name, hash_pw(password), time.time()))
            return public(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()), None
    except sqlite3.IntegrityError:
        return None, "An account with this email already exists. Sign in instead."


def login(email, password):
    with tx() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    ok = check_pw(password, row["pw"] if row else DUMMY_HASH)
    return public(row) if row and ok and not row["disabled"] else None


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()  # a leaked database doesn't leak usable sessions


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    with tx() as db:
        db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (_token_hash(token), user_id, time.time() + SESSION_S))
    return token


def user_for(token):
    if not token:
        return None
    with tx() as db:
        row = db.execute("""SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
                            WHERE token_hash = ? AND expires > ? AND disabled = 0""",
                         (_token_hash(token), time.time())).fetchone()
    return public(row)


def end_session(token):
    with tx() as db:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token or ""),))


def change_password(user_id, current, new, keep_token):
    if error := check_new_password(new):
        return error
    with tx() as db:
        row = db.execute("SELECT pw FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or not check_pw(current, row["pw"]):
            return "Your current password is incorrect."
        db.execute("UPDATE users SET pw = ? WHERE id = ?", (hash_pw(new), user_id))
        # sign out every other device
        db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _token_hash(keep_token)))
    return None


def set_alert(user_id, alert):
    with tx() as db:
        db.execute("UPDATE users SET alert = ? WHERE id = ?", (json.dumps(alert) if alert else None, user_id))


def list_users():
    with tx() as db:
        return [public(r) for r in db.execute("SELECT * FROM users ORDER BY created DESC")]


def update_user(user_id, role=None, disabled=None):
    with tx() as db:
        if role is not None:
            db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        if disabled is not None:
            db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))
            if disabled:
                db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return public(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def reset_password(user_id):
    """Admin reset: returns a one-time temporary password and signs the user out everywhere."""
    temp = secrets.token_urlsafe(9)
    with tx() as db:
        if not db.execute("UPDATE users SET pw = ? WHERE id = ?", (hash_pw(temp), user_id)).rowcount:
            return None
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return temp
