"""术语库：追加 / 覆盖 / 下载 / 未翻译判定 / 术语映射 / 覆盖前比对。"""

import os

from conftest import make_project, make_excel

import db
import glossary


def test_diff_glossary(tmp_path):
    """覆盖术语库前比对：新增/删除/修改（需求15）。"""
    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕', '金币', '旧词'])
    glossary.update_translations(pid, [
        {'id': 1, 'lang': 'English', 'text': 'Bracer'},
        {'id': 2, 'lang': 'English', 'text': 'Gold'},
    ])
    # 新文件：新增『新词』、删除『旧词』、修改『金币』的 English 译文
    new_path = make_excel(os.path.join(tmp_path, 'new.xlsx'), [
        {'中文': '护腕', 'English': 'Bracer'},
        {'中文': '金币', 'English': 'Coin'},
        {'中文': '新词', 'English': 'NewTerm'},
    ])
    d = glossary.diff_glossary(pid, '中文', ['English', '日本語'], new_path)
    assert d['added'] == ['新词']
    assert d['deleted'] == ['旧词']
    assert d['modified_count'] == 1
    assert d['modified'][0]['term'] == '金币'
    assert d['modified'][0]['changes'][0]['new'] == 'Coin'
    assert d['unchanged_count'] == 1  # 护腕
    assert d['old_total'] == 3 and d['new_total'] == 3


def test_add_terms_auto_id_and_dedupe():
    project = make_project()
    pid = project['id']

    r1 = glossary.add_terms(pid, '中文', ['护腕', '护腕', '迷雾战弓'])
    assert r1['added'] == 2
    assert r1['skipped'] == 1

    df = glossary.read_glossary(pid)
    assert len(df) == 2
    assert list(df['ID']) == [1, 2]
    assert df['中文'].tolist() == ['护腕', '迷雾战弓']

    r2 = glossary.add_terms(pid, '中文', ['护腕', '新术语'])
    assert r2['added'] == 1
    assert r2['skipped'] == 1
    df = glossary.read_glossary(pid)
    assert list(df['ID']) == [1, 2, 3]
    # ID + 中文 + 两个语种列（项目定义了 English / 日本語）
    assert len(df.columns.tolist()) == 4


def test_update_translations():
    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕'])

    changed = glossary.update_translations(pid, [
        {'id': 1, 'lang': 'English', 'text': 'Bracer'},
        {'id': 1, 'lang': '日本語', 'text': 'ブレスレット'},
        {'id': 999, 'lang': 'English', 'text': 'x'},  # 不存在的ID忽略
    ])
    assert changed == 2

    df = glossary.read_glossary(pid)
    assert df.loc[0, 'English'] == 'Bracer'
    assert df.loc[0, '日本語'] == 'ブレスレット'


def test_untranslated_terms():
    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕', '金币'])
    glossary.update_translations(pid, [
        {'id': 1, 'lang': 'English', 'text': 'Bracer'},
        {'id': 1, 'lang': '日本語', 'text': 'ブレスレット'},
    ])

    langs = ['English', '日本語']
    untranslated = glossary.untranslated_terms(pid, langs)
    # 护腕(ID1)已全语种翻译；金币(ID2)未翻译
    assert len(untranslated) == 1
    assert untranslated[0]['source'] == '金币'
    assert untranslated[0]['missing_langs'] == ['English', '日本語']


def test_build_glossary_map():
    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕', '金币'])
    glossary.update_translations(pid, [
        {'id': 1, 'lang': 'English', 'text': 'Bracer'},
        {'id': 2, 'lang': 'English', 'text': 'Gold Coin'},
    ])

    mapping = glossary.build_glossary_map(pid, 'English', '中文')
    assert mapping == {'护腕': 'Bracer', '金币': 'Gold Coin'}


def test_overwrite_glossary_normalizes(tmp_path):
    project = make_project()
    pid = project['id']
    path = make_excel(str(tmp_path / 'upload.xlsx'), [
        {'ID': 1, '中文': '旧词', 'English': 'Old'},
        {'ID': 2, '中文': '新词'},
    ])
    result = glossary.overwrite_glossary(pid, '中文', ['English', '日本語'], path)
    assert result['rows'] == 2

    df = glossary.read_glossary(pid)
    assert list(df['ID']) == [1, 2]
    assert df.loc[0, 'English'] == 'Old'
    assert df.loc[1, 'English'] == ''          # 缺失语种列补空
    assert '日本語' in df.columns
    assert 'ID' in df.columns


def test_glossary_table_serialization():
    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕'])
    data = glossary.glossary_table(pid)
    assert data['total'] == 1
    assert '中文' in data['columns']
    assert data['rows'][0][0] == '1'
