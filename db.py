"""SQLite data-access layer.

Schema (data/app.db):
  users               id, username, password_hash, is_admin, created_at
  projects            id, name, source_col_name, source_lang, description,
                      created_by, created_at
  project_languages   id, project_id, name, source_lang   (UNIQUE project+name)
  user_projects       id, user_id, project_id             (UNIQUE user+project)
  user_llm_settings   id, user_id UNIQUE, base_url, model, api_key_enc, updated_at

All writes go through parameterized statements; only SQLite stdlib is used.
"""

import sqlite3
from datetime import datetime

from config import DB_PATH, ensure_dirs


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def get_conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


# ── Schema ───────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    source_col_name TEXT NOT NULL,
    source_lang     TEXT NOT NULL DEFAULT 'zh',
    description     TEXT NOT NULL DEFAULT '',
    created_by      INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_languages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    source_lang TEXT NOT NULL DEFAULT 'zh',
    UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS user_projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    UNIQUE (user_id, project_id)
);

CREATE TABLE IF NOT EXISTS user_llm_settings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER UNIQUE NOT NULL,
    base_url    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    api_key_enc TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create tables and seed the default admin account (admin/admin123)."""
    conn = get_conn()
    conn.executescript(_SCHEMA)
    conn.commit()

    cur = conn.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1")
    if cur.fetchone()['n'] == 0:
        from security import hash_password
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at)"
            " VALUES (?, ?, 1, ?)",
            ('admin', hash_password('admin123'), _now()),
        )
        conn.commit()
    conn.close()


# ── Users ────────────────────────────────────────────────────────────────

def create_user(username: str, password_hash: str, is_admin: bool = False) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at)"
            " VALUES (?, ?, ?, ?)",
            (username, password_hash, int(is_admin), _now()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f'用户名已存在: {username}')
    finally:
        conn.close()


def get_user(user_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()


def get_user_by_username(username: str) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()


def list_users() -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    finally:
        conn.close()


def delete_user(user_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM user_projects WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_llm_settings WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# ── Projects ─────────────────────────────────────────────────────────────

def create_project(name: str, source_col_name: str, source_lang: str,
                   description: str, created_by: int) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO projects (name, source_col_name, source_lang,"
            " description, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, source_col_name, source_lang, description, created_by, _now()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f'项目名称已存在: {name}')
    finally:
        conn.close()


def get_project(project_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    finally:
        conn.close()


def list_projects() -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
    finally:
        conn.close()


def update_password(user_id: int, password_hash: str) -> None:
    """Update a user's password hash."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (password_hash, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_project(project_id: int, name: str, description: str) -> None:
    """Update a project's name and description (source col/lang unchanged)."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE projects SET name=?, description=? WHERE id=?",
            (name, description, project_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f'项目名称已存在: {name}')
    finally:
        conn.close()


def delete_project(project_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM project_languages WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM user_projects WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


def add_project_language(project_id: int, name: str, source_lang: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO project_languages (project_id, name, source_lang)"
            " VALUES (?, ?, ?)",
            (project_id, name, source_lang),
        )
        conn.commit()
    finally:
        conn.close()


def set_project_languages(project_id: int, languages: list[dict]) -> None:
    """Replace a project's language list. languages: [{name, source_lang}]."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM project_languages WHERE project_id = ?", (project_id,))
        for lang in languages:
            conn.execute(
                "INSERT INTO project_languages (project_id, name, source_lang)"
                " VALUES (?, ?, ?)",
                (project_id, lang['name'], lang.get('source_lang', 'zh')),
            )
        conn.commit()
    finally:
        conn.close()


def get_project_languages(project_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM project_languages WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    finally:
        conn.close()


# ── User <-> Project assignments ────────────────────────────────────────

def assign_projects(user_id: int, project_ids: list[int]) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM user_projects WHERE user_id = ?", (user_id,))
        for pid in project_ids:
            conn.execute(
                "INSERT OR IGNORE INTO user_projects (user_id, project_id)"
                " VALUES (?, ?)",
                (user_id, pid),
            )
        conn.commit()
    finally:
        conn.close()


def list_user_project_ids(user_id: int) -> list[int]:
    conn = get_conn()
    try:
        return [r['project_id'] for r in conn.execute(
            "SELECT project_id FROM user_projects WHERE user_id = ?", (user_id,))]
    finally:
        conn.close()


def has_project_access(user_id: int, project_id: int) -> bool:
    """Admins can access every project; users only their assigned ones."""
    user = get_user(user_id)
    if user is None:
        return False
    if user['is_admin']:
        return True
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM user_projects"
            " WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        ).fetchone()['n'] > 0
    finally:
        conn.close()


def list_user_projects(user_id: int) -> list[dict]:
    """Assigned projects with their language lists (for the project selector)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.* FROM projects p JOIN user_projects up"
            " ON p.id = up.project_id WHERE up.user_id = ? ORDER BY p.id",
            (user_id,),
        ).fetchall()
        result = []
        for r in rows:
            langs = conn.execute(
                "SELECT name, source_lang FROM project_languages"
                " WHERE project_id = ? ORDER BY id", (r['id'],)).fetchall()
            result.append({
                'id': r['id'], 'name': r['name'],
                'source_col_name': r['source_col_name'],
                'source_lang': r['source_lang'],
                'languages': [{'name': l['name'], 'source_lang': l['source_lang']} for l in langs],
            })
        return result
    finally:
        conn.close()


# ── LLM settings ────────────────────────────────────────────────────────

def get_llm_settings(user_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM user_llm_settings WHERE user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()


def save_llm_settings(user_id: int, base_url: str, model: str, api_key_enc: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO user_llm_settings (user_id, base_url, model, api_key_enc, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " base_url = excluded.base_url, model = excluded.model,"
            " api_key_enc = excluded.api_key_enc, updated_at = excluded.updated_at",
            (user_id, base_url, model, api_key_enc, _now()),
        )
        conn.commit()
    finally:
        conn.close()
