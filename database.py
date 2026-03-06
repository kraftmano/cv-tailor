"""
database.py - SQLite helpers for user accounts and credit management.

Schema:
  users(id, email, password_hash, credits, created_at)

The DB file path is read from the DB_PATH env var (default: cv_tailor.db).
On Railway, set DB_PATH=/data/cv_tailor.db and mount a volume at /data.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "cv_tailor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    credits       INTEGER NOT NULL DEFAULT 0,
    created_at    DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_role_cvs (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id),
    role_cvs_json TEXT    NOT NULL,
    generated_dir TEXT    NOT NULL,
    updated_at    DATETIME DEFAULT (datetime('now'))
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # allows concurrent reads during writes
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist. Call once at startup."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)


def create_user(email: str, password_hash: str) -> int:
    """Insert a new user. Raises sqlite3.IntegrityError if email already exists."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, credits) VALUES (?, ?, 0)",
            (email.strip().lower(), password_hash),
        )
        return cur.lastrowid


def get_user_by_email(email: str) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def add_credits(user_id: int, amount: int):
    """Add credits to a user's balance (called by Stripe webhook)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET credits = credits + ? WHERE id = ?",
            (amount, user_id),
        )


def save_role_cvs(user_id: int, role_cvs: list, generated_dir: str):
    """Persist a user's generated role CVs so they survive across browser sessions."""
    import json
    with get_db() as conn:
        conn.execute(
            """INSERT INTO user_role_cvs (user_id, role_cvs_json, generated_dir, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                 role_cvs_json = excluded.role_cvs_json,
                 generated_dir = excluded.generated_dir,
                 updated_at    = excluded.updated_at""",
            (user_id, json.dumps(role_cvs), generated_dir),
        )


def get_role_cvs(user_id: int) -> dict | None:
    """Load a user's persisted role CVs. Returns None if none saved yet."""
    import json
    with get_db() as conn:
        row = conn.execute(
            "SELECT role_cvs_json, generated_dir FROM user_role_cvs WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {"role_cvs": json.loads(row["role_cvs_json"]), "generated_dir": row["generated_dir"]}


def deduct_credit(user_id: int) -> int:
    """
    Deduct 1 credit from the user. Returns the new credit balance.
    Raises ValueError if the user has no credits.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT credits FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or row["credits"] < 1:
            raise ValueError("No credits remaining")
        conn.execute(
            "UPDATE users SET credits = credits - 1 WHERE id = ?", (user_id,)
        )
        return row["credits"] - 1
