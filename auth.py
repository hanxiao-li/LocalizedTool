"""Authentication helpers and route decorators.

Uses Flask's signed session cookie (server secret from config). API routes
return JSON 401; page routes redirect to the login page.
"""

from functools import wraps

from flask import g, jsonify, redirect, request, session, url_for

import db


def current_user():
    """Return the logged-in user Row, or None."""
    uid = session.get('user_id')
    if not uid:
        return None
    if 'user' not in g:
        g.user = db.get_user(uid)
    return g.user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if request.path.startswith('/api/'):
                return jsonify({'error': '未登录或会话已过期'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if request.path.startswith('/api/'):
                return jsonify({'error': '未登录或会话已过期'}), 401
            return redirect(url_for('login'))
        if not user['is_admin']:
            if request.path.startswith('/api/'):
                return jsonify({'error': '需要管理员权限'}), 403
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return wrapper


def project_access_required(f):
    """Require login + access to the project in the route/body.

    The project id may come from a `<project_id>` path segment or from a
    JSON/query `project_id` field. On failure returns JSON 403.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify({'error': '未登录或会话已过期'}), 401

        project_id = kwargs.pop('project_id', None)
        if project_id is None:
            project_id = request.view_args.get('project_id')
        if project_id is None:
            data = request.get_json(silent=True) or {}
            project_id = data.get('project_id')
        if project_id is None:
            project_id = request.args.get('project_id')
        if project_id is None:
            project_id = session.get('current_project')

        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return jsonify({'error': '缺少或无效的项目'}), 400

        if not db.has_project_access(user['id'], project_id):
            return jsonify({'error': '无权访问该项目'}), 403

        kwargs['project_id'] = project_id
        return f(*args, **kwargs)
    return wrapper


def require_llm_configured(user):
    """Return an error message if the user hasn't set up their LLM, else None."""
    settings = db.get_llm_settings(user['id'])
    if settings is None or not (settings['base_url'] and settings['model']):
        return '请先在右上角「设置」中配置大模型地址和模型名称'
    return None


def require_project_selected(user):
    """Return an error message if no project is selected, else None."""
    pid = session.get('current_project')
    if pid is None:
        return '请先选择一个项目'
    if not db.has_project_access(user['id'], pid):
        return '无权访问该项目'
    return None
