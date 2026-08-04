"""端到端全流程（Flask test client，LLM 全部打桩）。

管理员建用户/建项目/分配 → 用户配 LLM → 术语提取 → 确认 → 术语翻译 →
批量翻译 → 校验 → 下载，覆盖权限控制与核心业务闭环。
"""

import io

from conftest import make_excel, wait_task

from config import PROJECTS_DIR
import db
import os


def _excel_bytes(rows):
    """Return an in-memory .xlsx bytes object for upload."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
    tmp.close()
    make_excel(tmp.name, rows)
    with open(tmp.name, 'rb') as f:
        data = f.read()
    os.remove(tmp.name)
    return data


def test_full_flow_with_permissions(client, admin,
                                    mock_llm_extract, mock_llm_translate,
                                    mock_llm_test):
    # ── 管理员创建项目 + 用户并分配 ──────────────────────────────
    rv = client.post('/api/admin/projects', json={
        'name': '游戏A', 'source_col_name': '中文', 'source_lang': 'zh',
        'languages': [
            {'name': 'English', 'source_lang': 'zh'},
            {'name': '日本語', 'source_lang': 'zh'},
        ],
    })
    assert rv.status_code == 200
    project = next(p for p in db.list_projects() if p['name'] == '游戏A')
    project_id = project['id']

    rv = client.post('/api/admin/users', json={
        'username': 'tester', 'password': 'pass123',
        'is_admin': False, 'project_ids': [project_id],
    })
    assert rv.status_code == 200

    # 普通用户无法访问管理接口
    client.post('/api/logout')
    rv = client.post('/api/login', json={'username': 'tester', 'password': 'pass123'})
    assert rv.status_code == 200
    rv = client.get('/api/admin/users')
    assert rv.status_code == 403

    # 无法访问未分配项目
    rv = client.post('/api/project/select', json={'project_id': 99999})
    assert rv.status_code in (400, 403)

    # ── 用户配置大模型 ────────────────────────────────────────────
    rv = client.post('/api/settings/llm', json={
        'base_url': 'https://api.example.com/v1', 'model': 'test-model',
        'api_key': 'sk-secret',
    })
    assert rv.status_code == 200
    rv = client.post('/api/settings/llm/test', json={
        'base_url': 'https://api.example.com/v1', 'model': 'test-model',
    })
    assert rv.get_json()['ok'] is True

    # API Key 不应明文回传
    rv = client.get('/api/settings/llm')
    payload = rv.get_json()
    assert payload['has_key'] is True
    assert 'sk-secret' not in str(payload)

    # ── 选择项目 ──────────────────────────────────────────────────
    rv = client.post('/api/project/select', json={'project_id': project_id})
    assert rv.status_code == 200

    # 未配置 LLM 前不允许提取（这里已配置，先验证正常路径）
    # ── 术语提取 ──────────────────────────────────────────────────
    rv = client.post('/api/upload', data={
        'file': (io.BytesIO(_excel_bytes([
            {'中文': '护腕'}, {'中文': '迷雾战弓'}, {'中文': '金币'}, {'中文': '你好'},
        ])), 'extract.xlsx'),
        'tab': 'extract',
    }, content_type='multipart/form-data')
    assert rv.status_code == 200
    cols = rv.get_json()['columns']
    assert '中文' in cols

    rv = client.post('/api/extract/start', json={})  # 不再传源列，自动识别中文列
    assert rv.status_code == 200
    t = wait_task(client, rv.get_json()['task_id'])
    assert t['status'] == 'done'
    candidates = t['result']['candidates']
    assert {c['term'] for c in candidates} == {'护腕', '迷雾战弓', '金币'}

    rv = client.post('/api/terms/confirm',
                     json={'terms': ['护腕', '迷雾战弓', '金币']})
    assert rv.status_code == 200

    # ── 术语库已写入 ──────────────────────────────────────────────
    rv = client.get('/api/glossary')
    g = rv.get_json()
    assert g['total'] == 3
    rv = client.get('/api/glossary/download')
    assert rv.status_code == 200
    _ = rv.get_data()  # 消费流式响应，释放文件句柄

    # ── 术语翻译 ──────────────────────────────────────────────────
    rv = client.post('/api/upload', data={
        'file': (io.BytesIO(_excel_bytes([
            {'中文': '护腕', '上限': 100},
            {'中文': '迷雾战弓', '上限': 100},
            {'中文': '金币', '上限': 100},
            {'中文': '你好', '上限': 100},
        ])), 'tr.xlsx'),
        'tab': 'translate',
    }, content_type='multipart/form-data')
    assert rv.status_code == 200

    rv = client.get('/api/translate/status')
    st = rv.get_json()
    assert st['untranslated_count'] == 3

    rv = client.post('/api/translate/terms/start', json={})
    t = wait_task(client, rv.get_json()['task_id'])
    assert t['status'] == 'done'
    rows = t['result']['rows']
    assert len(rows) == 6  # 3术语 × 2语种

    rv = client.post('/api/translate/terms/confirm', json={
        'rows': [{'id': r['id'], 'lang': r['lang'], 'text': r['translation']}
                 for r in rows],
    })
    assert rv.status_code == 200
    rv = client.get('/api/translate/status')
    assert rv.get_json()['untranslated_count'] == 0

    # ── 批量翻译 ──────────────────────────────────────────────────
    rv = client.post('/api/translate/start', json={
        'targets': [
            {'lang': 'English', 'limit_col': '上限'},
            {'lang': '日本語', 'limit_col': '上限'},
        ],
    })
    assert rv.status_code == 200
    task_id = rv.get_json()['task_id']
    t = wait_task(client, task_id)
    assert t['status'] == 'done'
    assert t['result']['total_rows'] == 4

    rv = client.get(f'/api/translate/download/{task_id}')
    assert rv.status_code == 200
    assert 'attachment' in rv.headers.get('Content-Disposition', '')
    _ = rv.get_data()  # 消费流式响应，释放文件句柄

    # ── 校验（使用翻译结果）───────────────────────────────────────
    rv = client.post('/api/check/use-result', json={'task_id': task_id})
    assert rv.status_code == 200
    rv = client.get('/api/check/preview')
    preview = rv.get_json()
    eng = next(l for l in preview['languages'] if l['name'] == 'English')
    assert eng['source_col'] == '中文' and eng['target_col'] == 'English'

    rv = client.post('/api/check/start', json={
        'languages': ['English'],
        'length_limits': [],
        'enabled_checks': ['completeness', 'placeholder', 'numbers', 'terminology'],
    })
    assert rv.status_code == 200
    t = wait_task(client, rv.get_json()['task_id'])
    assert t['status'] == 'done'
    assert t['result']['total_results'] >= 0

    if t['result']['total_results'] > 0:
        rv = client.get('/api/check/download/' + t['result']['results_id'])
        assert rv.status_code == 200
        _ = rv.get_data()  # 消费流式响应，释放文件句柄

    # ── 任务状态越权检查 ──────────────────────────────────────────
    client.post('/api/logout')
    rv = client.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert rv.status_code == 200
    # admin 访问 tester 的任务也应被拒绝（任务属于 tester）
    rv = client.get(f'/api/tasks/{task_id}')
    assert rv.status_code == 403


def test_admin_cannot_be_deleted(client, admin):
    uid = admin['id']
    rv = client.delete(f'/api/admin/users/{uid}')
    assert rv.status_code == 400


def test_project_creation_source_always_zh(client, admin):
    """建项目时源语言固定中文、源列固定「中文(简体)」（需求8）。"""
    rv = client.post('/api/admin/projects', json={
        'name': '固定源', 'source_col_name': 'English', 'source_lang': 'en',
        'languages': [{'name': 'Deutsch', 'source_lang': 'en'},
                      {'name': 'English', 'source_lang': 'zh'}],
    })
    assert rv.status_code == 200
    project = next(p for p in db.list_projects() if p['name'] == '固定源')
    assert project['source_lang'] == 'zh'
    assert project['source_col_name'] == '中文(简体)'
    langs = {l['name']: l['source_lang'] for l in db.get_project_languages(project['id'])}
    assert langs == {'Deutsch': 'en', 'English': 'zh'}  # 每语种源设置保留


def test_project_edit_updates_name_desc_langs(client, admin):
    """管理员可编辑已建项目：名称/说明/语种（源语言固定中文不变）（需求1）。"""
    rv = client.post('/api/admin/projects', json={
        'name': '待编辑', 'languages': [{'name': 'English', 'source_lang': 'zh'}],
    })
    assert rv.status_code == 200
    pid = next(p for p in db.list_projects() if p['name'] == '待编辑')['id']

    rv = client.put(f'/api/admin/projects/{pid}', json={
        'name': '已编辑', 'description': '新说明',
        'languages': [{'name': 'English', 'source_lang': 'en'},
                      {'name': 'Deutsch', 'source_lang': 'en'}],
    })
    assert rv.status_code == 200
    p = db.get_project(pid)
    assert p['name'] == '已编辑'
    assert p['description'] == '新说明'
    assert p['source_col_name'] == '中文(简体)' and p['source_lang'] == 'zh'  # 源不变
    langs = {l['name']: l['source_lang'] for l in db.get_project_languages(pid)}
    assert langs == {'English': 'en', 'Deutsch': 'en'}

    # 改成另一个已存在项目的名称 → 报错
    rv = client.post('/api/admin/projects', json={
        'name': '另一项目', 'languages': [{'name': 'English', 'source_lang': 'zh'}],
    })
    assert rv.status_code == 200
    rv = client.put(f'/api/admin/projects/{pid}', json={
        'name': '另一项目', 'languages': [{'name': 'English', 'source_lang': 'zh'}],
    })
    assert rv.status_code == 400


def test_translate_missing_en_source_col_errors(client, admin, mock_llm_translate):
    """en 源语种缺少对应源列时，翻译启动应报错（需求1 找不到先报错）。"""
    rv = client.post('/api/admin/projects', json={
        'name': '缺源列', 'languages': [
            {'name': 'English', 'source_lang': 'zh'},
            {'name': 'Deutsch', 'source_lang': 'en'},
        ],
    })
    assert rv.status_code == 200
    project_id = next(p for p in db.list_projects() if p['name'] == '缺源列')['id']
    rv = client.post('/api/project/select', json={'project_id': project_id})
    assert rv.status_code == 200
    rv = client.post('/api/settings/llm', json={
        'base_url': 'https://x/v1', 'model': 'm', 'api_key': 'k'})
    assert rv.status_code == 200

    # 上传只有中文列（无 English 源列）的文件
    rv = client.post('/api/upload', data={
        'file': (io.BytesIO(_excel_bytes([{'中文': '你好', 'Deutsch': 'Hi'}])), 'tr.xlsx'),
        'tab': 'translate',
    }, content_type='multipart/form-data')
    assert rv.status_code == 200

    # 勾选 Deutsch（en 源），但缺少 English 源列 → 400
    rv = client.post('/api/translate/start', json={'targets': [{'lang': 'Deutsch'}]})
    assert rv.status_code == 400
    assert '源语言列' in rv.get_json()['error']


def test_check_missing_target_col_warns(client, admin, mock_llm_translate):
    """校验时所选语种缺少目标列应报错提醒（需求2）。"""
    rv = client.post('/api/admin/projects', json={
        'name': '缺目标列', 'languages': [
            {'name': 'English', 'source_lang': 'zh'},
            {'name': '日本語', 'source_lang': 'zh'},
        ],
    })
    assert rv.status_code == 200
    project_id = next(p for p in db.list_projects() if p['name'] == '缺目标列')['id']
    rv = client.post('/api/project/select', json={'project_id': project_id})
    assert rv.status_code == 200

    # 上传只有中文 + English 的文件（缺 日本語 目标列）
    rv = client.post('/api/upload', data={
        'file': (io.BytesIO(_excel_bytes([{'中文': '你好', 'English': 'Hello'}])), 'ck.xlsx'),
        'tab': 'check',
    }, content_type='multipart/form-data')
    assert rv.status_code == 200

    rv = client.post('/api/check/start', json={
        'languages': ['日本語'], 'length_limits': [], 'enabled_checks': ['completeness'],
    })
    assert rv.status_code == 400
    assert '目标语言列' in rv.get_json()['error']


def test_login_required(client):
    rv = client.get('/api/me')
    assert rv.status_code == 401
    rv = client.get('/app')
    assert rv.status_code == 302
