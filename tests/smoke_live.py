"""Live-server smoke test (run against a running app, no pytest).

Verifies real HTTP flow with proper UTF-8: boot -> admin login ->
create project/user -> assign -> user login -> LLM settings -> project
select -> glossary download. LLM-dependent tasks are covered by the
mocked unit tests; here we only verify connection-failure handling.
"""

import io
import os
import sys
import requests

BASE = os.environ.get('BASE_URL', 'http://127.0.0.1:5001')


def main():
    s = requests.Session()

    # 未登录保护
    r = s.get(f'{BASE}/api/me')
    assert r.status_code == 401, r.status_code
    r = s.get(f'{BASE}/app')
    assert r.status_code == 200  # 重定向到登录页
    print('[OK] 未登录保护')

    # 登录页渲染
    r = s.get(f'{BASE}/login')
    assert r.status_code == 200 and 'LocalizedTool' in r.text
    print('[OK] 登录页')

    # 管理员登录
    r = s.post(f'{BASE}/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200, r.text
    print('[OK] 管理员登录')

    # 建项目（中文）
    r = s.post(f'{BASE}/api/admin/projects', json={
        'name': '中文游戏A', 'source_col_name': '中文', 'source_lang': 'zh',
        'languages': [{'name': 'English', 'source_lang': 'zh'},
                      {'name': '日本語', 'source_lang': 'zh'}],
    })
    assert r.status_code == 200, r.text
    projects = s.get(f'{BASE}/api/admin/projects').json()['projects']
    pid = next(p['id'] for p in projects if p['name'] == '中文游戏A')
    print(f'[OK] 建项目 pid={pid}')

    # 建用户并分配
    r = s.post(f'{BASE}/api/admin/users', json={
        'username': '玩家甲', 'password': 'pass123', 'is_admin': False,
        'project_ids': [pid],
    })
    assert r.status_code == 200, r.text
    print('[OK] 建用户')

    # 普通用户登录 + 权限
    s2 = requests.Session()
    r = s2.post(f'{BASE}/api/login', json={'username': '玩家甲', 'password': 'pass123'})
    assert r.status_code == 200
    r = s2.get(f'{BASE}/api/admin/users')
    assert r.status_code == 403
    r = s2.post(f'{BASE}/api/project/select', json={'project_id': 99999})
    assert r.status_code in (400, 403)
    print('[OK] 用户权限隔离')

    # 用户配置 LLM + 测试连接（无效地址应优雅失败）
    r = s2.post(f'{BASE}/api/settings/llm', json={
        'base_url': 'https://api.example.com/v1', 'model': 'test-model',
        'api_key': 'sk-secret-123',
    })
    assert r.status_code == 200
    r = s2.post(f'{BASE}/api/settings/llm/test', json={
        'base_url': 'https://api.example.com/v1', 'model': 'test-model',
    })
    payload = r.json()
    assert 'ok' in payload  # 可能 ok=True(若网络可达)或 False，但不报错
    r = s2.get(f'{BASE}/api/settings/llm')
    assert r.json()['has_key'] is True
    assert 'sk-secret-123' not in r.text
    print('[OK] LLM 设置与 Token 加密（明文不泄露）')

    # 选择项目 + 术语库可下载
    r = s2.post(f'{BASE}/api/project/select', json={'project_id': pid})
    assert r.status_code == 200
    r = s2.get(f'{BASE}/api/glossary')
    assert r.status_code == 200 and r.json()['total'] == 0
    r = s2.get(f'{BASE}/api/glossary/download')
    assert r.status_code == 200
    assert len(r.content) > 0
    print('[OK] 项目选择 + 空术语库下载')

    # 主页面渲染（含全部页签）
    r = s2.get(f'{BASE}/app')
    assert r.status_code == 200
    for kw in ('术语提取', '模型翻译', '翻译校验'):
        assert kw in r.text, kw
    print('[OK] 主页面渲染')

    # 静态资源
    for path in ('/static/css/style.css', '/static/js/main.js'):
        r = s2.get(BASE + path)
        assert r.status_code == 200
    print('[OK] 静态资源')

    print('\n全部冒烟测试通过 ✅')


if __name__ == '__main__':
    main()
