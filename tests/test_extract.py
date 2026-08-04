"""术语提取（LLM 打桩）。"""

import os

from conftest import make_excel
from extract import run_extract


def _task():
    return {'task_id': 't1', 'status': 'starting', 'progress': 0,
            'phase': '', 'result': None, 'error': None}


def test_run_extract_collects_candidates(mock_llm_extract, tmp_path):
    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [
        {'中文': '获得护腕，护腕+1', '其他': 'x'},
        {'中文': '迷雾战弓与金币', '其他': 'x'},
        {'中文': '迷雾战弓+2', '其他': 'x'},
    ])
    task = _task()
    run_extract(task, path, '中文', 'zh',
                {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})

    assert task['status'] == 'done'
    candidates = task['result']['candidates']
    terms = {c['term'] for c in candidates}
    assert terms == {'护腕', '迷雾战弓', '金币'}
    counts = {c['term']: c['count'] for c in candidates}
    assert counts['迷雾战弓'] == 2   # 两行包含


def test_run_extract_missing_column(tmp_path):
    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [{'英文': 'hello'}])
    task = _task()
    try:
        run_extract(task, path, '中文', 'zh',
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})
        assert False, '应抛出列不存在错误'
    except ValueError as e:
        assert '不存在' in str(e)


def test_run_extract_empty_column(tmp_path, mock_llm_extract):
    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [{'中文': ''}, {'中文': None}])
    task = _task()
    try:
        run_extract(task, path, '中文', 'zh',
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})
        assert False, '应抛出空列错误'
    except ValueError as e:
        assert '没有可提取' in str(e)
