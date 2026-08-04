"""模型翻译（LLM 打桩）：术语翻译 + 批量翻译。"""

import json
import os

import db
from conftest import make_project, make_excel
from config import PROJECTS_DIR
from excel_utils import read_excel
import glossary
from translate import run_term_translation, run_translation


def test_run_translation_uses_large_batches(monkeypatch, tmp_path):
    """60 行短文本 × 2 语种：每个语种只发 1 次请求（共 2 次），
    而不是按 20 行/批拆成 6 次 —— 直接验证翻译提速。"""
    calls = []

    def fake(base_url, api_key, model, messages, **kwargs):
        calls.append(messages[-1]['content'])
        content = messages[-1]['content']
        start, end = content.find('['), content.rfind(']')
        items = json.loads(content[start:end + 1])
        return [{'id': it['id'], 'translation': 'T:' + str(it['text'])} for it in items]

    monkeypatch.setattr('translate.chat_json', fake)

    project = make_project()
    pid = project['id']
    rows = [{'中文': f'词语{i}'} for i in range(60)]
    path = make_excel(os.path.join(tmp_path, 'big.xlsx'), rows)

    task = _task('big')
    run_translation(task, pid, project, path,
                    [{'lang': 'English', 'source_col': '中文', 'source_lang': 'zh',
                      'limit_col': None},
                     {'lang': '日本語', 'source_col': '中文', 'source_lang': 'zh',
                      'limit_col': None}],
                    {'base_url': 'x', 'api_key': 'k', 'model': 'm'})

    assert task['status'] == 'done'
    assert len(calls) == 2, f'期望每个语种 1 次请求（共 2），实际 {len(calls)} 次'
    assert task['result']['total_rows'] == 60


def _task(task_id='t1'):
    return {'task_id': task_id, 'status': 'starting', 'progress': 0,
            'phase': '', 'result': None, 'error': None}


# ── 术语翻译 ────────────────────────────────────────────────────────────

def test_run_term_translation(mock_llm_translate):
    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕', '金币'])

    task = _task()
    run_term_translation(task, pid, '中文', ['English', '日本語'],
                         {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})

    assert task['status'] == 'done'
    rows = task['result']['rows']
    assert len(rows) == 4  # 2术语 × 2语种
    by_key = {(r['id'], r['lang']): r['translation'] for r in rows}
    assert by_key[(1, 'English')] == 'TR:护腕'
    assert by_key[(1, '日本語')] == 'TR:护腕'
    assert by_key[(2, 'English')] == 'TR:金币'


def test_run_term_translation_waves_zh_before_en(monkeypatch):
    """术语翻译也应先处理源=中文的语种，再处理源=英文的语种（需求5）。"""
    import threading
    seq = []
    lock = threading.Lock()

    def fake(base_url, api_key, model, messages, **kwargs):
        content = messages[-1]['content']
        is_en_src = '从「中文」翻译为' in content  # 术语翻译统一从中文术语翻译
        # 用目标语种区分：法语（en 源）应最后
        with lock:
            seq.append('fr' if 'Français' in content else 'ja')
        start, end = content.find('['), content.rfind(']')
        items = json.loads(content[start:end + 1])
        return [{'id': it['id'], 'translation': 'TR:' + str(it['text'])} for it in items]

    monkeypatch.setattr('translate.chat_json', fake)

    project = make_project('排序', '中文', [
        {'name': '日本語', 'source_lang': 'zh'},
        {'name': 'Français', 'source_lang': 'en'},
    ])
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕', '金币', '战弓'])
    task = _task('term_order')
    run_term_translation(task, pid, '中文', ['日本語', 'Français'],
                         {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'},
                         'zh', {'日本語': 'zh', 'Français': 'en'})
    assert task['status'] == 'done'
    # 日语（zh 源）全部先于法语（en 源）
    ja_pos = [i for i, x in enumerate(seq) if x == 'ja']
    fr_pos = [i for i, x in enumerate(seq) if x == 'fr']
    assert ja_pos and fr_pos, f'请求序列异常: {seq}'
    assert max(ja_pos) < min(fr_pos), f'zh 源语种未先于 en 源语种: {seq}'


def test_run_term_translation_none_untranslated(mock_llm_translate):
    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕'])
    glossary.update_translations(pid, [
        {'id': 1, 'lang': 'English', 'text': 'Bracer'},
        {'id': 1, 'lang': '日本語', 'text': 'ブレスレット'},
    ])
    task = _task()
    run_term_translation(task, pid, '中文', ['English', '日本語'],
                         {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})
    assert task['result']['total'] == 0


# ── 批量翻译 ────────────────────────────────────────────────────────────

def test_run_translation_glossary_and_placeholder(mock_llm_translate, tmp_path):
    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕'])
    glossary.update_translations(pid, [{'id': 1, 'lang': 'English', 'text': 'Bracer'}])

    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [
        {'中文': '护腕'},                       # 精确命中术语 → 直接替换
        {'中文': '获得 {0} 个金币'},            # 占位符需保留
        {'中文': '你好'},                       # 普通文本
    ])

    task = _task('tr1')
    run_translation(task, pid, project, path,
                    [{'lang': 'English', 'source_col': '中文',
                      'source_lang': 'zh', 'limit_col': None}],
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})

    assert task['status'] == 'done'
    result = task['result']
    assert result['total_rows'] == 3

    out_path = os.path.join(PROJECTS_DIR, str(pid), 'translations', result['file'])
    df = read_excel(out_path)
    assert 'English' in df.columns
    assert df.loc[0, 'English'] == 'Bracer'                    # 术语直接替换
    assert df.loc[1, 'English'] == 'TR:获得 {0} 个金币'        # 占位符保留
    assert df.loc[2, 'English'] == 'TR:你好'


def test_run_translation_multiple_langs_dependency_order(mock_llm_translate, tmp_path):
    project = make_project('多语种', '中文', [
        {'name': 'English', 'source_lang': 'zh'},
        {'name': 'Deutsch', 'source_lang': 'en'},   # 源=英文，后处理
    ])
    pid = project['id']
    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [
        {'中文': '你好', 'English': 'Hello'},
    ])
    task = _task('tr2')
    run_translation(task, pid, project, path,
                    [{'lang': 'English', 'source_col': '中文', 'source_lang': 'zh',
                      'limit_col': None},
                     {'lang': 'Deutsch', 'source_col': 'English', 'source_lang': 'en',
                      'limit_col': None}],
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})
    assert task['status'] == 'done'
    result = task['result']
    assert set(result['languages']) == {'English', 'Deutsch'}

    out_path = os.path.join(PROJECTS_DIR, str(pid), 'translations', result['file'])
    df = read_excel(out_path)
    assert df.loc[0, 'English'] == 'TR:你好'
    assert df.loc[0, 'Deutsch'] == 'TR:Hello'


def test_run_translation_with_limit_col(mock_llm_translate, tmp_path):
    project = make_project()
    pid = project['id']
    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [
        {'中文': '这是一段比较长的中文文本', '上限': 200},
    ])
    task = _task('tr3')
    run_translation(task, pid, project, path,
                    [{'lang': 'English', 'source_col': '中文',
                      'source_lang': 'zh', 'limit_col': '上限'}],
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})
    assert task['status'] == 'done'


# ── 需求5：空译文/缺失条目重试 ──────────────────────────────────────────

def test_run_translation_retries_empty_results(monkeypatch, tmp_path):
    """模型第一轮返回空译文时，应自动补翻拿到结果（修「翻译返回为空」）。"""
    state = {'n': 0}

    def fake(base_url, api_key, model, messages, **kwargs):
        content = messages[-1]['content']
        start, end = content.find('['), content.rfind(']')
        items = json.loads(content[start:end + 1])
        state['n'] += 1
        if state['n'] == 1:
            return [{'id': it['id'], 'translation': ''} for it in items]  # 全部为空
        return [{'id': it['id'], 'translation': 'T:' + str(it['text'])} for it in items]

    monkeypatch.setattr('translate.chat_json', fake)

    project = make_project()
    pid = project['id']
    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [
        {'中文': '你好'}, {'中文': '再见'},
    ])
    task = _task('retry')
    run_translation(task, pid, project, path,
                    [{'lang': 'English', 'source_col': '中文',
                      'source_lang': 'zh', 'limit_col': None}],
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})

    assert task['status'] == 'done'
    assert state['n'] == 2  # 初次 + 一次补翻
    out_path = os.path.join(PROJECTS_DIR, str(pid), 'translations', task['result']['file'])
    df = read_excel(out_path)
    assert df['English'].tolist() == ['T:你好', 'T:再见']


def test_run_term_translation_retries_empty(monkeypatch, tmp_path):
    """术语翻译首轮为空也应补翻。"""
    state = {'n': 0}

    def fake(base_url, api_key, model, messages, **kwargs):
        content = messages[-1]['content']
        start, end = content.find('['), content.rfind(']')
        items = json.loads(content[start:end + 1])
        state['n'] += 1
        if state['n'] == 1:
            return [{'id': it['id'], 'translation': ''} for it in items]
        return [{'id': it['id'], 'translation': 'TR:' + str(it['text'])} for it in items]

    monkeypatch.setattr('translate.chat_json', fake)

    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕'])
    task = _task('trm')
    run_term_translation(task, pid, '中文', ['English'],
                         {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})
    assert task['status'] == 'done'
    rows = task['result']['rows']
    assert rows and rows[0]['translation'] == 'TR:护腕'


# ── en 源语种：术语库应用（English 列作源键） ────────────────────────────

def test_run_translation_en_source_uses_english_glossary(monkeypatch, tmp_path):
    """en 源语种（Deutsch）应使用术语库 English 列命中术语直接替换。"""
    calls = []

    def fake(base_url, api_key, model, messages, **kwargs):
        calls.append(messages[-1]['content'])
        content = messages[-1]['content']
        start, end = content.find('['), content.rfind(']')
        items = json.loads(content[start:end + 1])
        return [{'id': it['id'], 'translation': 'T:' + str(it['text'])} for it in items]

    monkeypatch.setattr('translate.chat_json', fake)

    project = make_project('混合源', '中文', [
        {'name': 'English', 'source_lang': 'zh'},
        {'name': 'Deutsch', 'source_lang': 'en'},
    ])
    pid = project['id']
    # 术语库：中文『你好』→ English『Hello』→ Deutsch『Hallo』
    glossary.add_terms(pid, '中文', ['你好'])
    glossary.update_translations(pid, [
        {'id': 1, 'lang': 'English', 'text': 'Hello'},
        {'id': 1, 'lang': 'Deutsch', 'text': 'Hallo'},
    ])

    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [
        {'中文': '你好', 'English': 'Hello'},        # Deutsch 源文本=Hello → 命中术语 Hallo
        {'中文': '再见', 'English': 'Goodbye'},      # 未命中 → 走 LLM
    ])
    task = _task('en1')
    run_translation(task, pid, project, path,
                    [{'lang': 'Deutsch', 'source_col': 'English',
                      'source_lang': 'en', 'limit_col': None}],
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})

    assert task['status'] == 'done'
    out_path = os.path.join(PROJECTS_DIR, str(pid), 'translations', task['result']['file'])
    df = read_excel(out_path)
    assert df.loc[0, 'Deutsch'] == 'Hallo'                 # 术语直接替换，无 LLM 调用
    assert df.loc[1, 'Deutsch'] == 'T:Goodbye'
    assert len(calls) == 1                                  # 只有第二行触发了一次请求


# ── 术语一致性：条目级定向重译 + 安全词边界替换 ──────────────────────────

def test_run_translation_terminology_item_retry(monkeypatch, tmp_path):
    """源含术语但首轮漏译 → 条目级定向重译应补上库内译法（需求3）。"""
    def fake(base_url, api_key, model, messages, **kwargs):
        content = messages[-1]['content']
        start, end = content.find('['), content.rfind(']')
        items = json.loads(content[start:end + 1])
        if '术语译文' in content:
            # 定向重译轮：按术语译文重译
            return [{'id': it['id'], 'translation': 'Get the Bracer'} for it in items]
        # 首轮：漏译术语（译成别的词）
        return [{'id': it['id'], 'translation': 'Get the item'} for it in items]

    monkeypatch.setattr('translate.chat_json', fake)

    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕'])
    glossary.update_translations(pid, [{'id': 1, 'lang': 'English', 'text': 'Bracer'}])

    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [
        {'中文': '获得护腕'},
    ])
    task = _task('trm1')
    run_translation(task, pid, project, path,
                    [{'lang': 'English', 'source_col': '中文',
                      'source_lang': 'zh', 'limit_col': None}],
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})
    assert task['status'] == 'done'
    out_path = os.path.join(PROJECTS_DIR, str(pid), 'translations', task['result']['file'])
    df = read_excel(out_path)
    assert df.loc[0, 'English'] == 'Get the Bracer'


def test_run_translation_terminology_safe_replace(monkeypatch, tmp_path):
    """短场景（源=单个术语带标点）重译仍漏译时，安全词边界替换写入库内译法。"""
    def fake(base_url, api_key, model, messages, **kwargs):
        content = messages[-1]['content']
        start, end = content.find('['), content.rfind(']')
        items = json.loads(content[start:end + 1])
        # 无论哪轮都漏译（故意返回错的），验证安全替换兜底
        return [{'id': it['id'], 'translation': 'Wrong'} for it in items]

    monkeypatch.setattr('translate.chat_json', fake)

    project = make_project()
    pid = project['id']
    glossary.add_terms(pid, '中文', ['护腕'])
    glossary.update_translations(pid, [{'id': 1, 'lang': 'English', 'text': 'Bracer'}])

    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), [
        {'中文': '护腕。'},      # 去除标点后=单个术语，可安全替换
        {'中文': '获得护腕'},    # 长句嵌入式，不做盲替
    ])
    task = _task('trm2')
    run_translation(task, pid, project, path,
                    [{'lang': 'English', 'source_col': '中文',
                      'source_lang': 'zh', 'limit_col': None}],
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})
    assert task['status'] == 'done'
    out_path = os.path.join(PROJECTS_DIR, str(pid), 'translations', task['result']['file'])
    df = read_excel(out_path)
    assert df.loc[0, 'English'] == 'Bracer'          # 短场景被安全替换
    assert df.loc[1, 'English'] == 'Wrong'            # 长句不做盲替（保持重译结果）


# ── 需求4/9：并发但依赖顺序（zh 源先于 en 源） ──────────────────────────

def test_translation_waves_zh_before_en(monkeypatch, tmp_path):
    """zh 源语种的所有请求必须先于 en 源语种发起（即使并发）。"""
    import threading
    seq = []
    lock = threading.Lock()

    def fake(base_url, api_key, model, messages, **kwargs):
        content = messages[-1]['content']
        is_en = '「英文」' in content
        start, end = content.find('['), content.rfind(']')
        items = json.loads(content[start:end + 1])
        with lock:
            seq.append('en' if is_en else 'zh')
        return [{'id': it['id'], 'translation': 'T:' + str(it['text'])} for it in items]

    monkeypatch.setattr('translate.chat_json', fake)

    project = make_project('混合源', '中文', [
        {'name': 'English', 'source_lang': 'zh'},
        {'name': 'Deutsch', 'source_lang': 'en'},
    ])
    pid = project['id']
    # 120 行 → 每个语种 2 批（100+20），zh 两批、en 两批
    rows = [{'中文': f'词{i}', 'English': f'Word{i}'} for i in range(120)]
    path = make_excel(os.path.join(tmp_path, 'src.xlsx'), rows)
    task = _task('wave')
    run_translation(task, pid, project, path,
                    [{'lang': 'English', 'source_col': '中文', 'source_lang': 'zh',
                      'limit_col': None},
                     {'lang': 'Deutsch', 'source_col': 'English', 'source_lang': 'en',
                      'limit_col': None}],
                    {'base_url': 'http://x', 'api_key': 'k', 'model': 'm'})

    assert task['status'] == 'done'
    zh_pos = [i for i, x in enumerate(seq) if x == 'zh']
    en_pos = [i for i, x in enumerate(seq) if x == 'en']
    assert len(zh_pos) == 2 and len(en_pos) == 2, f'请求序列异常: {seq}'
    assert max(zh_pos) < min(en_pos), f'zh 源请求未全部先于 en 源请求: {seq}'
    out_path = os.path.join(PROJECTS_DIR, str(pid), 'translations', task['result']['file'])
    df = read_excel(out_path)
    assert len(df) == 120
    assert df.loc[119, 'English'] == 'T:词119'
    assert df.loc[119, 'Deutsch'] == 'T:Word119'
