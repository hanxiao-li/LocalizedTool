"""Shared pytest fixtures.

The whole suite runs against an isolated temp data dir; the in-memory task
state is reset before every test; the LLM is always stubbed (no network).
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

# ── Isolate runtime data BEFORE importing app ────────────────────────────
_TMP_DATA = tempfile.mkdtemp(prefix='localizedtool_test_')
os.environ['LOCALIZEDTOOL_DATA_DIR'] = _TMP_DATA

import app as app_module  # noqa: E402
from app import app as flask_app  # noqa: E402
import db  # noqa: E402


def _wipe_dir(path: str):
    """rmtree with retries — Windows may briefly lock files held by
    unconsumed streamed responses or background threads."""
    import gc
    import time
    for attempt in range(6):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == 5:
                raise
            gc.collect()
            time.sleep(0.25)


@pytest.fixture(autouse=True)
def reset_state():
    """Wipe the temp data dir + in-memory state before every test."""
    from config import DATA_DIR
    if os.path.isdir(DATA_DIR):
        _wipe_dir(DATA_DIR)
    db.init_db()
    app_module._tasks.clear()
    app_module._current.clear()
    yield


@pytest.fixture
def client():
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def admin(client):
    """Log in as the seeded default admin and return its /api/me payload."""
    rv = client.post('/api/login', json={
        'username': 'admin', 'password': 'admin123'})
    assert rv.status_code == 200
    return rv.get_json()['user']


# ── LLM stubs ───────────────────────────────────────────────────────────

@pytest.fixture
def mock_llm_translate(monkeypatch):
    """Stub translate.chat_json: replies 'TR:<text>' for each {id,text} item."""
    def fake(base_url, api_key, model, messages, **kwargs):
        content = ''
        for m in reversed(messages):
            if m['role'] == 'user':
                content = m['content']
                break
        items = []
        start = content.find('[')
        end = content.rfind(']')
        if start != -1 and end > start:
            try:
                items = json.loads(content[start:end + 1])
            except json.JSONDecodeError:
                items = []
        out = []
        for it in items:
            if isinstance(it, dict) and 'id' in it and 'text' in it:
                out.append({'id': it['id'], 'translation': 'TR:' + str(it['text'])})
        return out

    monkeypatch.setattr('translate.chat_json', fake)


@pytest.fixture
def mock_llm_extract(monkeypatch):
    """Stub extract.chat_json to return a fixed candidate list."""
    def fake(base_url, api_key, model, messages, **kwargs):
        return {'terms': ['护腕', '迷雾战弓', '金币']}

    monkeypatch.setattr('extract.chat_json', fake)


@pytest.fixture
def mock_llm_test(monkeypatch):
    """Stub llm.test_connection."""
    monkeypatch.setattr('llm.test_connection',
                        lambda base_url, api_key, model: (True, '测试成功'))


# ── Helpers ─────────────────────────────────────────────────────────────

def make_excel(filepath: str, rows: list[dict]) -> str:
    """Write a list-of-dicts Excel file and return its path."""
    import pandas as pd
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Sheet1', index=False)
    return filepath


def wait_task(client, task_id: str, timeout: float = 15.0):
    """Poll a background task until done/error. Returns the task dict."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        rv = client.get(f'/api/tasks/{task_id}')
        if rv.status_code != 200:
            return None
        t = rv.get_json()
        if t['status'] in ('done', 'error'):
            return t
        time.sleep(0.2)
    raise TimeoutError(f'task {task_id} did not finish in {timeout}s')


def make_project(name: str = '测试项目',
                 source_col_name: str = '中文',
                 languages: list = None) -> dict:
    """Create a project row directly through db (no HTTP)."""
    langs = languages or [
        {'name': 'English', 'source_lang': 'zh'},
        {'name': '日本語', 'source_lang': 'zh'},
    ]
    pid = db.create_project(name, source_col_name, 'zh', '', 1)
    db.set_project_languages(pid, langs)
    from glossary import ensure_glossary
    ensure_glossary(pid, source_col_name, [l['name'] for l in langs])
    return db.get_project(pid)
