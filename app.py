"""LocalizedTool — 网页版游戏本地化流程工具。

Flask 入口 + 全部 API 路由。功能：
  1. 用户管理（管理员建用户/建项目/分配权限，用户自配大模型）
  2. 术语提取（大模型 + Excel + 项目术语库）
  3. 模型翻译（术语翻译 + 批量翻译 + 进度 + 下载）
  4. 翻译校验（复用 LQA 校验器，自动判语种 + 项目术语库注入）
"""

import logging
import os
import threading
import uuid

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

import auth
import db
import languages
from config import get_flask_secret, MAX_UPLOAD_MB, PROJECTS_DIR, DATA_DIR
from excel_utils import columns_of, list_sheets, save_upload
from security import decrypt_token, encrypt_token, hash_password, verify_password

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = get_flask_secret()
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

# ── Runtime state ────────────────────────────────────────────────────────
# 每个 (用户, 项目, 页签) 当前的上传文件上下文（内存态，重启后需重新上传）。
_current = {}
# 后台任务进度（内存态）。
_tasks = {}


def _tab_state(user_id: int, project_id: int, tab: str) -> dict:
    return _current.get((user_id, project_id, tab)) or {}


def _set_tab_state(user_id: int, project_id: int, tab: str, data: dict) -> None:
    _current[(user_id, project_id, tab)] = data


def _start_task(kind: str, user_id: int, project_id: int, fn) -> str:
    task_id = uuid.uuid4().hex
    _tasks[task_id] = {
        'task_id': task_id, 'kind': kind, 'user_id': user_id,
        'project_id': project_id, 'status': 'starting', 'progress': 0,
        'phase': '准备中...', 'result': None, 'error': None,
    }

    def runner():
        try:
            fn(_tasks[task_id])
        except Exception as e:
            logger.exception('Task %s failed', task_id)
            _tasks[task_id].update(
                {'status': 'error', 'error': str(e), 'progress': 0,
                 'phase': f'失败: {e}'})

    threading.Thread(target=runner, daemon=True).start()
    return task_id


def _ctx():
    """Resolve (user, project, error). error is a JSON-response dict or None."""
    user = auth.current_user()
    if user is None:
        return None, None, ({'error': '未登录或会话已过期'}, 401)
    pid = session.get('current_project')
    if not pid:
        return user, None, ({'error': '请先选择一个项目'}, 400)
    if not db.has_project_access(user['id'], pid):
        return user, None, ({'error': '无权访问该项目'}, 403)
    project = db.get_project(pid)
    if project is None:
        return user, None, ({'error': '项目不存在'}, 404)
    return user, project, None


def _llm_cfg(user) -> dict | None:
    """User's LLM config with the decrypted API key, or None if incomplete."""
    settings = db.get_llm_settings(user['id'])
    if settings is None or not (settings['base_url'] and settings['model']):
        return None
    return {
        'base_url': settings['base_url'],
        'model': settings['model'],
        'api_key': decrypt_token(settings['api_key_enc']),
    }


def _project_lang_names(project) -> list[str]:
    return [r['name'] for r in db.get_project_languages(project['id'])]


# ── Pages ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if auth.current_user() is None:
        return redirect(url_for('login'))
    return redirect(url_for('app_page'))


@app.route('/login')
def login():
    return render_template('login.html')


@app.route('/app')
@auth.login_required
def app_page():
    return render_template('index.html')


@app.route('/api/meta', methods=['GET'])
@auth.login_required
def api_meta():
    """规范语言列表等前端元数据。"""
    return jsonify({'languages': languages.LANGUAGES,
                    'lang_names': languages.LANG_NAMES})


# ── Auth API ────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    user = db.get_user_by_username(username)
    if user is None or not verify_password(user['password_hash'], password):
        return jsonify({'error': '用户名或密码错误'}), 401
    session.clear()
    session['user_id'] = user['id']
    return jsonify({'message': '登录成功', 'user': _user_payload(user)})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'message': '已退出登录'})


@app.route('/api/me', methods=['GET'])
@auth.login_required
def api_me():
    user = auth.current_user()
    return jsonify({'user': _user_payload(user)})


def _user_payload(user) -> dict:
    settings = db.get_llm_settings(user['id'])
    if user['is_admin']:
        projects = [_project_payload(p) for p in db.list_projects()]
    else:
        projects = db.list_user_projects(user['id'])
    return {
        'id': user['id'],
        'username': user['username'],
        'is_admin': bool(user['is_admin']),
        'llm_configured': bool(settings and settings['base_url'] and settings['model']),
        'projects': projects,
        'current_project': session.get('current_project'),
    }


# ── Project selection ───────────────────────────────────────────────────

@app.route('/api/project/select', methods=['POST'])
@auth.login_required
def api_project_select():
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    pid = data.get('project_id')
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return jsonify({'error': '项目无效'}), 400
    if not db.has_project_access(user['id'], pid):
        return jsonify({'error': '无权访问该项目'}), 403
    project = db.get_project(pid)
    if project is None:
        return jsonify({'error': '项目不存在'}), 404

    from glossary import ensure_glossary
    ensure_glossary(pid, project['source_col_name'], _project_lang_names(project))

    session['current_project'] = pid
    return jsonify({'message': f'已切换到项目「{project["name"]}」',
                    'project': _project_payload(project)})


@app.route('/api/project/current', methods=['GET'])
@auth.login_required
def api_project_current():
    user = auth.current_user()
    pid = session.get('current_project')
    if not pid or not db.has_project_access(user['id'], pid):
        return jsonify({'project': None})
    project = db.get_project(pid)
    if project is None:
        return jsonify({'project': None})
    return jsonify({'project': _project_payload(project)})


def _project_payload(project) -> dict:
    langs = db.get_project_languages(project['id'])
    return {
        'id': project['id'], 'name': project['name'],
        'source_col_name': project['source_col_name'],
        'source_lang': project['source_lang'],
        'description': project['description'],
        'languages': [{'name': l['name'], 'source_lang': l['source_lang']} for l in langs],
    }


# ── User settings: LLM ──────────────────────────────────────────────────

@app.route('/api/settings/llm', methods=['GET'])
@auth.login_required
def api_get_llm_settings():
    user = auth.current_user()
    settings = db.get_llm_settings(user['id'])
    return jsonify({
        'base_url': settings['base_url'] if settings else '',
        'model': settings['model'] if settings else '',
        'has_key': bool(settings and settings['api_key_enc']),
    })


@app.route('/api/settings/llm', methods=['POST'])
@auth.login_required
def api_save_llm_settings():
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    base_url = (data.get('base_url') or '').strip()
    model = (data.get('model') or '').strip()
    if not base_url or not model:
        return jsonify({'error': '请填写大模型地址和模型名称'}), 400

    existing = db.get_llm_settings(user['id'])
    api_key_enc = existing['api_key_enc'] if existing else ''
    new_key = (data.get('api_key') or '').strip()
    if new_key:
        api_key_enc = encrypt_token(new_key)

    db.save_llm_settings(user['id'], base_url, model, api_key_enc)
    return jsonify({'message': '大模型设置已保存'})


@app.route('/api/password', methods=['POST'])
@auth.login_required
def api_change_password():
    """修改当前登录用户的密码（用户与管理员通用）。"""
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    old_password = data.get('old_password') or ''
    new_password = data.get('new_password') or ''
    if not old_password or not new_password:
        return jsonify({'error': '请填写当前密码和新密码'}), 400
    if len(new_password) < 6:
        return jsonify({'error': '新密码至少 6 位'}), 400
    if not verify_password(user['password_hash'], old_password):
        return jsonify({'error': '当前密码不正确'}), 400
    db.update_password(user['id'], hash_password(new_password))
    return jsonify({'message': '密码已修改，下次登录请使用新密码'})


@app.route('/api/settings/llm/test', methods=['POST'])
@auth.login_required
def api_test_llm():
    import llm
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    base_url = (data.get('base_url') or '').strip()
    model = (data.get('model') or '').strip()
    api_key = (data.get('api_key') or '').strip()
    if not api_key:
        settings = db.get_llm_settings(user['id'])
        if settings:
            api_key = decrypt_token(settings['api_key_enc'])
    if not base_url or not model:
        return jsonify({'error': '请先填写地址和模型名称'}), 400
    ok, message = llm.test_connection(base_url, api_key, model)
    return jsonify({'ok': ok, 'message': message})


# ── Admin: users ────────────────────────────────────────────────────────

@app.route('/api/admin/users', methods=['GET'])
@auth.admin_required
def api_admin_list_users():
    users = []
    for u in db.list_users():
        users.append({
            'id': u['id'], 'username': u['username'],
            'is_admin': bool(u['is_admin']),
            'project_ids': db.list_user_project_ids(u['id']),
        })
    return jsonify({'users': users})


@app.route('/api/admin/users', methods=['POST'])
@auth.admin_required
def api_admin_create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    is_admin = bool(data.get('is_admin'))
    project_ids = [int(p) for p in (data.get('project_ids') or []) if str(p).isdigit()]
    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    try:
        uid = db.create_user(username, hash_password(password), is_admin)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if project_ids:
        db.assign_projects(uid, project_ids)
    return jsonify({'message': f'用户「{username}」已创建'})


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@auth.admin_required
def api_admin_delete_user(user_id: int):
    if user_id == auth.current_user()['id']:
        return jsonify({'error': '不能删除当前登录的管理员'}), 400
    db.delete_user(user_id)
    return jsonify({'message': '用户已删除'})


@app.route('/api/admin/users/<int:user_id>/projects', methods=['POST'])
@auth.admin_required
def api_admin_set_user_projects(user_id: int):
    data = request.get_json(silent=True) or {}
    project_ids = [int(p) for p in (data.get('project_ids') or []) if str(p).isdigit()]
    db.assign_projects(user_id, project_ids)
    return jsonify({'message': '项目权限已更新'})


# ── Admin: projects ─────────────────────────────────────────────────────

@app.route('/api/admin/projects', methods=['GET'])
@auth.admin_required
def api_admin_list_projects():
    result = []
    for p in db.list_projects():
        result.append(_project_payload(p))
    return jsonify({'projects': result})


@app.route('/api/admin/projects', methods=['POST'])
@auth.admin_required
def api_admin_create_project():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()
    langs = data.get('languages') or []
    if not name:
        return jsonify({'error': '请填写项目名称'}), 400
    # 项目源语言固定为中文：源语言列名固定为规范中文名「中文(简体)」
    source_col_name = '中文(简体)'
    source_lang = 'zh'
    if not langs:
        return jsonify({'error': '请至少添加一个目标语种'}), 400
    normalized = []
    for lang in langs:
        lname = (lang.get('name') or '').strip()
        if not lname:
            return jsonify({'error': '目标语种名称不能为空'}), 400
        lname = languages.lang_name(languages.lang_code(lname))
        lsrc = (lang.get('source_lang') or '').strip()
        if lsrc not in ('zh', 'en'):
            return jsonify({'error': f'语种「{lname}」的源语言只能是 zh 或 en'}), 400
        normalized.append({'name': lname, 'source_lang': lsrc})

    try:
        pid = db.create_project(name, source_col_name, source_lang,
                                description, auth.current_user()['id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    db.set_project_languages(pid, normalized)

    from glossary import ensure_glossary
    ensure_glossary(pid, source_col_name, [l['name'] for l in normalized])
    return jsonify({'message': f'项目「{name}」已创建'})


@app.route('/api/admin/projects/<int:project_id>', methods=['PUT'])
@auth.admin_required
def api_admin_update_project(project_id: int):
    """编辑项目：名称/说明/语种及每语种源设置。源语言固定中文、源列不变。"""
    data = request.get_json(silent=True) or {}
    project = db.get_project(project_id)
    if project is None:
        return jsonify({'error': '项目不存在'}), 404
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()
    if not name:
        return jsonify({'error': '请填写项目名称'}), 400

    langs = data.get('languages') or []
    if not langs:
        return jsonify({'error': '请至少添加一个目标语种'}), 400
    normalized = []
    for lang in langs:
        lname = (lang.get('name') or '').strip()
        if not lname:
            return jsonify({'error': '目标语种名称不能为空'}), 400
        lname = languages.lang_name(languages.lang_code(lname))
        lsrc = (lang.get('source_lang') or '').strip()
        if lsrc not in ('zh', 'en'):
            return jsonify({'error': f'语种「{lname}」的源语言只能是 zh 或 en'}), 400
        normalized.append({'name': lname, 'source_lang': lsrc})

    try:
        db.update_project(project_id, name, description)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db.set_project_languages(project_id, normalized)

    from glossary import ensure_glossary
    ensure_glossary(project_id, project['source_col_name'],
                    [l['name'] for l in normalized])
    return jsonify({'message': f'项目「{name}」已更新'})


@app.route('/api/admin/projects/<int:project_id>', methods=['DELETE'])
@auth.admin_required
def api_admin_delete_project(project_id: int):
    db.delete_project(project_id)
    import shutil
    shutil.rmtree(os.path.join(PROJECTS_DIR, str(project_id)), ignore_errors=True)
    return jsonify({'message': '项目已删除'})


# ── File upload (per tab) ───────────────────────────────────────────────

@app.route('/api/upload', methods=['POST'])
@auth.login_required
def api_upload():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]

    if 'file' not in request.files:
        return jsonify({'error': '请选择文件'}), 400
    file = request.files['file']
    if not file.filename or not file.filename.lower().endswith(('.xlsx', '.xls')):
        return jsonify({'error': '请上传 Excel 文件 (.xlsx / .xls)'}), 400

    tab = request.form.get('tab', 'translate')
    if tab not in ('extract', 'translate', 'check'):
        return jsonify({'error': '页签无效'}), 400

    path = save_upload(file)
    try:
        cols = columns_of(path)
        sheets = list_sheets(path)
    except Exception as e:
        os.remove(path)
        return jsonify({'error': f'读取 Excel 失败: {e}'}), 400

    _set_tab_state(user['id'], project['id'], tab,
                   {'filepath': path, 'filename': file.filename, 'columns': cols})
    return jsonify({
        'message': f'上传成功: {file.filename}（{len(cols)}列）',
        'columns': cols, 'sheets': sheets, 'filename': file.filename,
    })


# ── Glossary ────────────────────────────────────────────────────────────

@app.route('/api/glossary', methods=['GET'])
@auth.login_required
def api_glossary():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    from glossary import glossary_table
    data = glossary_table(project['id'])
    data['source_col_name'] = project['source_col_name']
    data['languages'] = _project_lang_names(project)
    return jsonify(data)


@app.route('/api/glossary/diff', methods=['POST'])
@auth.login_required
def api_glossary_diff():
    """比对上传的术语库文件与当前术语库（新增/删除/修改），供覆盖前确认。"""
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    if 'file' not in request.files:
        return jsonify({'error': '请选择术语库文件'}), 400
    file = request.files['file']
    if not file.filename.lower().endswith(('.xlsx', '.xls')):
        return jsonify({'error': '请上传 Excel 文件'}), 400

    path = save_upload(file, subfolder='glossary_tmp')
    try:
        from glossary import diff_glossary
        result = diff_glossary(
            project['id'], project['source_col_name'],
            _project_lang_names(project), path)
    except Exception as e:
        return jsonify({'error': f'读取文件失败: {e}'}), 400
    finally:
        os.remove(path)
    return jsonify(result)


@app.route('/api/glossary/upload', methods=['POST'])
@auth.login_required
def api_glossary_upload():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    if 'file' not in request.files:
        return jsonify({'error': '请选择术语库文件'}), 400
    file = request.files['file']
    if not file.filename.lower().endswith(('.xlsx', '.xls')):
        return jsonify({'error': '请上传 Excel 文件'}), 400

    path = save_upload(file, subfolder='glossary_tmp')
    try:
        from glossary import overwrite_glossary
        result = overwrite_glossary(
            project['id'], project['source_col_name'],
            _project_lang_names(project), path)
    except Exception as e:
        return jsonify({'error': f'术语库覆盖失败: {e}'}), 400
    finally:
        os.remove(path)
    return jsonify(result)


@app.route('/api/glossary/download', methods=['GET'])
@auth.login_required
def api_glossary_download():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    from glossary import glossary_path, ensure_glossary
    ensure_glossary(project['id'], project['source_col_name'], _project_lang_names(project))
    path = glossary_path(project['id'])
    return send_file(path, as_attachment=True,
                     download_name=f'术语库_{project["name"]}.xlsx')


# ── Term extraction ─────────────────────────────────────────────────────

@app.route('/api/extract/start', methods=['POST'])
@auth.login_required
def api_extract_start():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    cfg = _llm_cfg(user)
    if cfg is None:
        return jsonify({'error': '请先在右上角「设置」中配置大模型'}), 400

    state = _tab_state(user['id'], project['id'], 'extract')
    if not state.get('filepath'):
        return jsonify({'error': '请先上传待提取的 Excel 文件'}), 400

    # 术语提取固定从中文（简体）列提取，不再让用户选择源列
    source_col = languages.find_lang_column(state.get('columns', []), '中文(简体)')
    if not source_col:
        return jsonify({'error': '未找到中文（简体）源语言列，请确认上传文件包含中文列'}), 400

    from extract import run_extract
    task_id = _start_task('extract', user['id'], project['id'],
                          lambda t: run_extract(t, state['filepath'], source_col,
                                                'zh', cfg))
    return jsonify({'task_id': task_id, 'message': '术语提取已启动'})


# ── Terms confirm (from extraction) ─────────────────────────────────────

@app.route('/api/terms/confirm', methods=['POST'])
@auth.login_required
def api_terms_confirm():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    data = request.get_json(silent=True) or {}
    terms = data.get('terms') or []
    if not terms:
        return jsonify({'error': '没有要确认的术语'}), 400
    from glossary import add_terms
    result = add_terms(project['id'], project['source_col_name'],
                       [str(t).strip() for t in terms])
    return jsonify({'message': f'已确认 {result["added"]} 条术语'
                               f'（跳过重复 {result["skipped"]} 条）', **result})


# ── Task progress ───────────────────────────────────────────────────────

@app.route('/api/tasks/<task_id>', methods=['GET'])
@auth.login_required
def api_task(task_id: str):
    task = _tasks.get(task_id)
    if task is None:
        return jsonify({'error': '任务不存在或已过期'}), 404
    if task['user_id'] != auth.current_user()['id']:
        return jsonify({'error': '无权访问该任务'}), 403
    # 移除敏感字段，只返回对外信息
    return jsonify({k: v for k, v in task.items() if k != 'user_id'})


# ── Term translation ────────────────────────────────────────────────────

@app.route('/api/translate/status', methods=['GET'])
@auth.login_required
def api_translate_status():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    from glossary import untranslated_terms
    lang_names = _project_lang_names(project)
    terms = untranslated_terms(project['id'], lang_names)
    state = _tab_state(user['id'], project['id'], 'translate')
    columns = state.get('columns', [])
    languages_info = []
    for l in db.get_project_languages(project['id']):
        src_col = languages.resolve_source_column(columns, project, l['source_lang'])
        languages_info.append({
            'name': l['name'], 'source_lang': l['source_lang'],
            'source_col': src_col,
            'source_col_found': bool(src_col),
        })
    return jsonify({
        'has_file': bool(state.get('filepath')),
        'columns': columns,
        'filename': state.get('filename'),
        'languages': languages_info,
        'untranslated_count': len(terms),
        'untranslated': terms[:200],
    })


@app.route('/api/translate/terms/start', methods=['POST'])
@auth.login_required
def api_translate_terms_start():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    cfg = _llm_cfg(user)
    if cfg is None:
        return jsonify({'error': '请先在右上角「设置」中配置大模型'}), 400

    from translate import run_term_translation
    project_langs = db.get_project_languages(project['id'])
    lang_names = [l['name'] for l in project_langs]
    lang_source = {l['name']: l['source_lang'] for l in project_langs}
    task_id = _start_task('term_translate', user['id'], project['id'],
                          lambda t: run_term_translation(
                              t, project['id'], project['source_col_name'],
                              lang_names, cfg, project['source_lang'], lang_source))
    return jsonify({'task_id': task_id, 'message': '术语翻译已启动'})


@app.route('/api/translate/terms/confirm', methods=['POST'])
@auth.login_required
def api_translate_terms_confirm():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    data = request.get_json(silent=True) or {}
    rows = data.get('rows') or []
    if not rows:
        return jsonify({'error': '没有可保存的内容'}), 400
    from glossary import update_translations
    changed = update_translations(project['id'], rows)
    return jsonify({'message': f'已保存 {changed} 条术语译文'})


# ── Batch translation ───────────────────────────────────────────────────

@app.route('/api/translate/start', methods=['POST'])
@auth.login_required
def api_translate_start():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    cfg = _llm_cfg(user)
    if cfg is None:
        return jsonify({'error': '请先在右上角「设置」中配置大模型'}), 400

    state = _tab_state(user['id'], project['id'], 'translate')
    if not state.get('filepath'):
        return jsonify({'error': '请先上传待翻译的 Excel 文件'}), 400

    data = request.get_json(silent=True) or {}
    targets = data.get('targets') or []
    if not targets:
        return jsonify({'error': '请选择至少一个目标语种'}), 400

    # 每个语种的源列由项目配置自动解析，用户不再选择
    columns = state.get('columns', [])
    project_langs = {l['name']: l['source_lang'] for l in db.get_project_languages(project['id'])}
    normalized = []
    for t in targets:
        lang = (t.get('lang') or '').strip()
        lang = languages.lang_name(languages.lang_code(lang))
        if lang not in project_langs:
            return jsonify({'error': f'目标语种「{lang}」不在项目语种中'}), 400
        t_src_lang = project_langs[lang]
        t_src_col = languages.resolve_source_column(columns, project, t_src_lang)
        if not t_src_col:
            src_desc = (languages.lang_name(t_src_lang)
                        if t_src_lang == 'en' else project['source_col_name'])
            return jsonify({'error': f'目标语种「{lang}」需要源语言列「{src_desc}」，'
                            '上传文件未找到该列'}), 400
        normalized.append({
            'lang': lang, 'source_col': t_src_col,
            'source_lang': t_src_lang,
            'limit_col': (t.get('limit_col') or '').strip() or None,
        })

    from translate import run_translation
    task_id = _start_task('translate', user['id'], project['id'],
                          lambda t: run_translation(
                              t, project['id'], project, state['filepath'],
                              normalized, cfg))
    return jsonify({'task_id': task_id, 'message': '翻译已启动'})


@app.route('/api/translate/download/<task_id>', methods=['GET'])
@auth.login_required
def api_translate_download(task_id: str):
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    task = _tasks.get(task_id)
    if task is None or task['status'] != 'done':
        return jsonify({'error': '任务不存在或未完成'}), 404
    if task['user_id'] != user['id'] or task['project_id'] != project['id']:
        return jsonify({'error': '无权访问该任务'}), 403
    fname = (task.get('result') or {}).get('file')
    if not fname:
        return jsonify({'error': '结果文件不存在'}), 404
    path = os.path.join(PROJECTS_DIR, str(project['id']), 'translations', fname)
    if not os.path.exists(path):
        return jsonify({'error': '结果文件已过期'}), 404
    return send_file(path, as_attachment=True,
                     download_name=f'翻译结果_{project["name"]}.xlsx')


# ── Check (翻译校验) ───────────────────────────────────────────────────

@app.route('/api/check/preview', methods=['GET'])
@auth.login_required
def api_check_preview():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    state = _tab_state(user['id'], project['id'], 'check')
    if not state.get('filepath'):
        return jsonify({'error': '请先上传待校验的 Excel 文件'}), 400
    columns = state.get('columns', [])
    language_resolutions = []
    for l in db.get_project_languages(project['id']):
        src_col = languages.resolve_source_column(columns, project, l['source_lang'])
        tgt_col = languages.find_lang_column(columns, l['name'])
        missing = []
        if not src_col:
            missing.append('源列')
        if not tgt_col:
            missing.append('目标列')
        language_resolutions.append({
            'name': l['name'], 'source_lang': l['source_lang'],
            'source_col': src_col, 'target_col': tgt_col,
            'missing': missing, 'ok': not missing,
        })
    return jsonify({
        'columns': columns,
        'filename': state['filename'],
        'languages': language_resolutions,
    })


@app.route('/api/check/use-result', methods=['POST'])
@auth.login_required
def api_check_use_result():
    """把某个翻译任务的输出文件作为校验文件（点「校验」按钮时调用）。"""
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    data = request.get_json(silent=True) or {}
    task_id = data.get('task_id')
    task = _tasks.get(task_id)
    if task is None or task['status'] != 'done':
        return jsonify({'error': '翻译任务不存在或未完成'}), 404
    if task['user_id'] != user['id'] or task['project_id'] != project['id']:
        return jsonify({'error': '无权访问该任务'}), 403
    fname = (task.get('result') or {}).get('file')
    if not fname:
        return jsonify({'error': '结果文件不存在'}), 404
    path = os.path.join(PROJECTS_DIR, str(project['id']), 'translations', fname)
    if not os.path.exists(path):
        return jsonify({'error': '结果文件已过期'}), 404
    _set_tab_state(user['id'], project['id'], 'check', {
        'filepath': path, 'filename': fname, 'columns': columns_of(path)})
    return jsonify({'message': '已载入翻译结果作为校验文件',
                    'filename': fname})


@app.route('/api/check/start', methods=['POST'])
@auth.login_required
def api_check_start():
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    state = _tab_state(user['id'], project['id'], 'check')
    if not state.get('filepath'):
        return jsonify({'error': '请先上传待校验的 Excel 文件'}), 400

    data = request.get_json(silent=True) or {}
    selected_langs = data.get('languages') or []
    length_limits = data.get('length_limits') or []
    enabled_checks = data.get('enabled_checks') or []
    if not selected_langs:
        return jsonify({'error': '请选择至少一个要校验的语种'}), 400

    # 由所选校验语种确定源语言列和目标语言列（项目配置驱动，用户不选列）
    columns = state.get('columns', [])
    project_langs = {l['name']: l['source_lang'] for l in db.get_project_languages(project['id'])}
    valid_pairs = []
    missing_msgs = []
    for lname in selected_langs:
        lang = languages.lang_name(languages.lang_code(lname))
        if lang not in project_langs:
            return jsonify({'error': f'语种「{lang}」不在项目语种中'}), 400
        src_lang = project_langs[lang]
        src_col = languages.resolve_source_column(columns, project, src_lang)
        tgt_col = languages.find_lang_column(columns, lang)
        if not src_col:
            src_desc = (languages.lang_name(src_lang)
                        if src_lang == 'en' else project['source_col_name'])
            missing_msgs.append(f'语种「{lang}」缺少源语言列「{src_desc}」')
        if not tgt_col:
            missing_msgs.append(f'语种「{lang}」缺少目标语言列「{lang}」')
        if src_col and tgt_col:
            valid_pairs.append({'lang': lang, 'source_col': src_col,
                                'target_col': tgt_col, 'source_lang': src_lang})
    if missing_msgs:
        return jsonify({'error': '；'.join(missing_msgs) + '，请检查上传文件列名'}), 400
    if not valid_pairs:
        return jsonify({'error': '没有有效的语种可校验'}), 400

    from check_engine import run_check
    task_id = _start_task('check', user['id'], project['id'],
                          lambda t: run_check(
                              t, state['filepath'], valid_pairs,
                              length_limits, enabled_checks,
                              {'id': project['id'], 'source_lang': project['source_lang'],
                               'source_col_name': project['source_col_name']}))
    return jsonify({'task_id': task_id, 'message': '校验已启动'})


@app.route('/api/check/download/<results_id>', methods=['GET'])
@auth.login_required
def api_check_download(results_id: str):
    user, project, err = _ctx()
    if err:
        return jsonify(err[0]), err[1]
    path = os.path.join(PROJECTS_DIR, str(project['id']), 'check_results',
                        f'{results_id}.xlsx')
    if not os.path.exists(path):
        return jsonify({'error': '结果文件已过期'}), 404
    return send_file(path, as_attachment=True,
                     download_name=f'校验结果_{project["name"]}.xlsx')


# ── Main ────────────────────────────────────────────────────────────────

def _ssl_context():
    """若 data/ 下存在自签证书则返回 (cert, key)，否则 None（HTTP）。"""
    cert = os.path.join(DATA_DIR, 'cert.pem')
    key = os.path.join(DATA_DIR, 'ssl_key.pem')
    if os.path.exists(cert) and os.path.exists(key):
        return (cert, key)
    return None


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    db.init_db()
    port = int(os.environ.get('PORT', 5000))
    ssl_ctx = _ssl_context()
    scheme = 'https' if ssl_ctx else 'http'
    print('=' * 50)
    print('  LocalizedTool - 游戏本地化流程工具')
    print('  默认管理员: admin / admin123（请及时在管理端修改）')
    print(f'  访问地址: {scheme}://127.0.0.1:{port}')
    if ssl_ctx:
        print('  HTTPS 已启用（局域网加密）。生成/更新证书请运行 python gen_cert.py')
    else:
        print('  未配置证书，使用 HTTP（生成自签证书可开启局域网加密：python gen_cert.py）')
    print('=' * 50)
    app.run(host='0.0.0.0', port=port, debug=False, ssl_context=ssl_ctx)


if __name__ == '__main__':
    main()
