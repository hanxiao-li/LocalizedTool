"""翻译校验：自动判语种 + 9 项校验（无 LLM 依赖）。"""

import os

from conftest import make_project, make_excel

import db
import glossary
from check_engine import auto_detect_pairs, lang_name_to_code, run_check
from excel_utils import read_excel


def _task(task_id='c1'):
    return {'task_id': task_id, 'status': 'starting', 'progress': 0,
            'phase': '', 'result': None, 'error': None}


def test_lang_name_to_code():
    assert lang_name_to_code('English') == 'en'
    assert lang_name_to_code('日本語') == 'ja'
    assert lang_name_to_code('中文') == 'zh-cn'
    assert lang_name_to_code('Deutsch') == 'de'
    assert lang_name_to_code('unknown-lang') == 'en'  # 默认


def test_auto_detect_pairs():
    detected = auto_detect_pairs(
        ['中文', 'English', '日本語', '备注'], '中文', ['English', '日本語'])
    assert detected['source_col'] == '中文'
    assert detected['pairs'] == [
        {'col': 'English', 'lang': 'English'},
        {'col': '日本語', 'lang': '日本語'},
    ]


def test_run_check_detects_issues(tmp_path):
    project = make_project()
    pid = project['id']
    # 术语库：护腕 → Bracer
    glossary.add_terms(pid, '中文', ['护腕'])
    glossary.update_translations(pid, [{'id': 1, 'lang': 'English', 'text': 'Bracer'}])

    path = make_excel(os.path.join(tmp_path, 'check.xlsx'), [
        {'中文': '护腕', 'English': 'Bracer'},            # 正常
        {'中文': '获得护腕', 'English': 'Get item'},       # 术语不一致
        {'中文': '获得 {0} 金币', 'English': 'Get gold'},   # 占位符缺失
        {'中文': '你有 100 金币', 'English': 'You have 200 gold'},  # 数字不一致
        {'中文': '你好', 'English': ''},                  # 未翻译
    ])

    task = _task()
    run_check(task, path,
              [{'lang': 'English', 'source_col': '中文', 'target_col': 'English',
                'source_lang': 'zh'}],
              [], ['completeness', 'placeholder', 'numbers', 'terminology'],
              {'id': pid, 'source_lang': 'zh', 'source_col_name': '中文'})

    assert task['status'] == 'done'
    results = task['result']['results']

    issues = [(r['row'], r['check_type']) for r in results]
    # index2 → row3 占位符缺失
    assert (3, 'placeholder') in issues
    # index3 → row4 数字不一致
    assert (4, 'numbers') in issues
    # index4 → row5 未翻译
    assert (5, 'completeness') in issues
    # index1 → row2 术语不一致
    assert (2, 'terminology') in issues

    # 第一行（index0 → row1）应无任何问题
    row1_issues = [r for r in results if r['row'] == 1]
    assert row1_issues == []

    # 错误计数
    assert task['result']['errors'] >= 4


def test_run_check_empty_terminology_no_crash(tmp_path):
    """术语库为空时 terminology 检查不应报错。"""
    project = make_project()
    pid = project['id']
    path = make_excel(os.path.join(tmp_path, 'check.xlsx'), [
        {'中文': '护腕', 'English': 'Bracer'},
    ])
    task = _task()
    run_check(task, path,
              [{'lang': 'English', 'source_col': '中文', 'target_col': 'English',
                'source_lang': 'zh'}],
              [], ['terminology'],
              {'id': pid, 'source_lang': 'zh', 'source_col_name': '中文'})
    assert task['status'] == 'done'
    assert task['result']['total_results'] == 0


def test_run_check_writes_downloadable_excel(tmp_path):
    project = make_project()
    pid = project['id']
    path = make_excel(os.path.join(tmp_path, 'check.xlsx'), [
        {'中文': '你好', 'English': ''},
    ])
    task = _task()
    run_check(task, path,
              [{'lang': 'English', 'source_col': '中文', 'target_col': 'English',
                'source_lang': 'zh'}],
              [], ['completeness'],
              {'id': pid, 'source_lang': 'zh', 'source_col_name': '中文'})
    results_id = task['result']['results_id']
    from config import PROJECTS_DIR
    xlsx = os.path.join(PROJECTS_DIR, str(pid), 'check_results', f'{results_id}.xlsx')
    assert os.path.exists(xlsx)
    # 需求6：下载文件 = 上传原表 + 「问题_English」列，含具体问题内容
    df = read_excel(xlsx)
    assert '中文' in df.columns and 'English' in df.columns
    assert '问题_English' in df.columns
    assert '目标文本为空' in df.loc[0, '问题_English']


# ── 需求1/3：每语种独立源列 + en 源术语一致性 ────────────────────────────

def test_run_check_per_lang_source_columns(tmp_path):
    """English 源=中文列，Deutsch 源=English 列，各自按配置校验。"""
    project = make_project('混合源', '中文', [
        {'name': 'English', 'source_lang': 'zh'},
        {'name': 'Deutsch', 'source_lang': 'en'},
    ])
    pid = project['id']
    path = make_excel(os.path.join(tmp_path, 'check.xlsx'), [
        {'中文': '你好', 'English': 'Hello', 'Deutsch': 'Hallo'},
        {'中文': '你好', 'English': 'Hello', 'Deutsch': ''},       # Deutsch 未翻译
    ])
    task = _task()
    run_check(task, path,
              [{'lang': 'English', 'source_col': '中文', 'target_col': 'English',
                'source_lang': 'zh'},
               {'lang': 'Deutsch', 'source_col': 'English', 'target_col': 'Deutsch',
                'source_lang': 'en'}],
              [], ['completeness'],
              {'id': pid, 'source_lang': 'zh', 'source_col_name': '中文'})
    assert task['status'] == 'done'
    issues = [(r['row'], r['target_language']) for r in task['result']['results']]
    # 第二行（row2）Deutsch 为空 → 问题落在 Deutsch 上
    assert (2, 'Deutsch') in issues
    # 第一行无问题
    assert (1, 'English') not in issues and (1, 'Deutsch') not in issues


def test_run_check_en_source_terminology(tmp_path):
    """en 源语种（Deutsch）术语一致性用术语库 English 列作源键匹配。"""
    project = make_project('混合源', '中文', [
        {'name': 'English', 'source_lang': 'zh'},
        {'name': 'Deutsch', 'source_lang': 'en'},
    ])
    pid = project['id']
    glossary.add_terms(pid, '中文', ['你好'])
    glossary.update_translations(pid, [
        {'id': 1, 'lang': 'English', 'text': 'Hello'},
        {'id': 1, 'lang': 'Deutsch', 'text': 'Hallo'},
    ])

    path = make_excel(os.path.join(tmp_path, 'check.xlsx'), [
        {'中文': '你好', 'English': 'Hello', 'Deutsch': 'Hallo'},   # 术语正确
        {'中文': '你好', 'English': 'Hello', 'Deutsch': 'Hi'},       # 术语不一致
    ])
    task = _task()
    run_check(task, path,
              [{'lang': 'Deutsch', 'source_col': 'English', 'target_col': 'Deutsch',
                'source_lang': 'en'}],
              [], ['terminology'],
              {'id': pid, 'source_lang': 'zh', 'source_col_name': '中文'})
    assert task['status'] == 'done'
    term_issues = [r for r in task['result']['results'] if r['check_type'] == 'terminology']
    assert len(term_issues) == 1
    assert term_issues[0]['row'] == 2
    assert 'Hallo' in term_issues[0]['issue']
