"""模型翻译：术语翻译 + 批量文本翻译。

术语翻译：把项目术语库中「未全语种翻译」的术语，逐语种交给大模型翻译，
结果供用户确认后写回术语库。不需要源列（直接读术语库源术语列）。

批量翻译：将上传 Excel 中每个目标语种对应的源语言列逐语种翻译，遵循：
  * 术语优先 —— 源文本命中术语库时直接采用术语译文；
  * 游戏本地化风格 + 代码/占位符/转义符原样保留；
  * 可选的每语种字符上限；
  * 依赖顺序 —— 源语言=中文的语种先翻，源语言=英文的后翻（分两波并发，
    每波内部并发，英文列可能由本任务中文波次先翻译产出）。
"""

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from checkers.placeholder import extract_placeholders
from config import PROJECTS_DIR
from excel_utils import read_excel, write_excel
from glossary import build_glossary_map, untranslated_terms
from languages import glossary_source_key
from llm import chat_json, LLMError

logger = logging.getLogger(__name__)

# 单批请求的行数与字符数上限：批次越大，deepseek-v4-flash 越容易漏译/返回空。
# 适度调小以保可靠性（短文本仍可一语种一批；大文件自动拆多批，并发 8 分摊）。
MAX_BATCH_ROWS = 50
MAX_BATCH_CHARS = 8000
# 单条源文本送入模型前的最大长度
MAX_SRC_LEN = 800
# 术语翻译每批条数
TERM_BATCH = 40
# 并行发送请求的固定并发数（不降模型、只并行提速；实测 8 路对
# deepseek-v4-flash 无限速、吞吐最佳）
MAX_CONCURRENCY = 8
# 空译文/缺失条目的补翻重试次数
MISSING_RETRY = 2
# 补翻重试时单次请求的条目上限：避免大批次补翻再次被模型漏译
MAX_RETRY_BATCH = 10

_LANG_NAME = {'zh': '中文', 'en': '英文'}


def _split_batches(rows: list[dict]) -> list[list[dict]]:
    """按行数与字符预算切分批次：短文本尽量一次请求，超上限再拆分。"""
    batches = []
    cur = []
    cur_chars = 0
    for r in rows:
        n = len(r['text'])
        if cur and (len(cur) >= MAX_BATCH_ROWS or cur_chars + n > MAX_BATCH_CHARS):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(r)
        cur_chars += n
    if cur:
        batches.append(cur)
    return batches


def _batch_max_tokens(items: list) -> int:
    """按批大小给足生成配额，避免大批次被截断，也避免过度预留。"""
    return max(2048, min(8192, len(items) * 80))


def _lang_label(code: str) -> str:
    return _LANG_NAME.get(code, code)


# ── 术语翻译 ────────────────────────────────────────────────────────────

TERM_SYSTEM_PROMPT = (
    '你是游戏本地化的术语翻译专家。将给定的术语从源语言翻译为目标语言。\n'
    '要求：\n'
    '1. 遵循游戏本地化风格，用词简洁、贴合游戏世界观。\n'
    '2. 专有名词的译法要统一、自然；必要时可保留英文原名或加注通用译法。\n'
    '3. 只输出 JSON 数组，每项为 {"id": 序号, "translation": "译文"}，不要输出其他内容。'
)


def _term_user_prompt(batch: list[dict], src_lang: str, tgt_lang: str) -> str:
    return (
        f'将以下术语从「{_lang_label(src_lang)}」翻译为「{tgt_lang}」：\n'
        + json.dumps([{'id': b['id'], 'text': b['source']} for b in batch],
                     ensure_ascii=False)
        + '\n\n请输出 JSON 数组。'
    )


def _parse_mapping(data):
    """把模型返回的 JSON 归一成 (id→translation, id→{术语:模型译文})。

    兼容多种结构：数组 / {translations:...} / {data:...} / 单条 / id→译文。
    terms 字段：模型报告每个 ⟦术语⟧ 译成了什么，用于程序级术语一致性替换。
    """
    if isinstance(data, dict):
        if any(k in data for k in ('translations', 'data', 'results')):
            data = data.get('translations') or data.get('data') or data.get('results')
        elif 'translation' in data or 'terms' in data:
            data = [data]  # 单条
    mapping, terms_map = {}, {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                mapping[item.get('id')] = item.get('translation', '')
                t = item.get('terms')
                if isinstance(t, dict):
                    terms_map[item.get('id')] = t
            elif isinstance(item, str):
                mapping[len(mapping)] = item
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                mapping[k] = v.get('translation', '')
                t = v.get('terms')
                if isinstance(t, dict):
                    terms_map[k] = t
            else:
                mapping[k] = v
    return mapping, terms_map


def _translate_term_batch(llm_cfg: dict, chunk: list[dict], src_lang: str,
                          tgt_lang: str) -> dict:
    """翻译一个术语批次，空译文条目独立补翻（最多 MISSING_RETRY 次）。"""
    messages = [
        {'role': 'system', 'content': TERM_SYSTEM_PROMPT},
        {'role': 'user', 'content': _term_user_prompt(chunk, src_lang, tgt_lang)},
    ]
    mapping, _ = _parse_mapping(chat_json(
        llm_cfg['base_url'], llm_cfg['api_key'], llm_cfg['model'],
        messages, temperature=0.2, max_tokens=4096, timeout=None))

    missing = [j for j in chunk
               if not (mapping.get(j['id']) or mapping.get(str(j['id'])) or '').strip()]
    for _ in range(MISSING_RETRY):
        if not missing:
            break
        retry_messages = messages + [{
            'role': 'user',
            'content': ('以下术语译文为空，必须补全，不得遗漏，只输出这些术语的 JSON 数组：'
                        + json.dumps([{'id': m['id'], 'text': m['source']}
                                      for m in missing], ensure_ascii=False)),
        }]
        try:
            mapping.update(_parse_mapping(chat_json(
                llm_cfg['base_url'], llm_cfg['api_key'], llm_cfg['model'],
                retry_messages, temperature=0.2, max_tokens=4096, timeout=None))[0])
        except LLMError:
            break
        missing = [j for j in missing
                   if not (mapping.get(j['id']) or mapping.get(str(j['id'])) or '').strip()]
    return mapping


def run_term_translation(task: dict, project_id: int, source_col_name: str,
                         lang_names: list[str], llm_cfg: dict,
                         source_lang: str = 'zh',
                         lang_source: dict | None = None) -> None:
    """Translate every untranslated term into every project language.

    Result rows: [{id, lang, source, translation, status}].
    不需要源列 —— 直接读术语库源术语列。
    依赖顺序：zh 源语种先并发翻译完成，en 源语种后并发翻译（与批量翻译一致）。
    lang_source: {语种名: 'zh'|'en'} —— 项目配置中每个语种的源语言。
    """
    terms = untranslated_terms(project_id, lang_names)
    if not terms:
        task.update({
            'status': 'done', 'progress': 100, 'phase': '没有需要翻译的术语',
            'result': {'rows': [], 'total': 0},
        })
        return

    # 需要翻译的 (id, source, lang) 组合
    jobs = []
    for t in terms:
        for lang in t['missing_langs']:
            jobs.append({'id': t['id'], 'source': t['source'], 'lang': lang})

    # 分批：每个 (语种, TERM_BATCH 条) 一次请求
    by_lang = {}
    for j in jobs:
        by_lang.setdefault(j['lang'], []).append(j)

    batches = []
    for lang, jlist in by_lang.items():
        for i in range(0, len(jlist), TERM_BATCH):
            batches.append((lang, jlist[i:i + TERM_BATCH]))

    def _wave_key(b):
        ls = (lang_source or {}).get(b[0], source_lang)
        return 0 if ls == 'zh' else 1

    total = len(batches)
    task.update({'status': 'running', 'progress': 0,
                 'phase': f'准备翻译 {len(jobs)} 条术语...'})

    lock = threading.Lock()
    results = {}          # (id, lang) -> translation
    done = [0]
    errors = []

    def work(item):
        lang, chunk = item
        try:
            mapping = _translate_term_batch(llm_cfg, chunk, source_lang, lang)
            with lock:
                for j in chunk:
                    trans = mapping.get(j['id']) or mapping.get(str(j['id'])) or ''
                    results[(j['id'], lang)] = (trans or '').strip()
                done[0] += 1
                task.update({
                    'progress': round(done[0] / total * 100, 1),
                    'phase': f'正在翻译术语（{lang}，{done[0]}/{total} 批）...',
                })
        except Exception as e:
            with lock:
                errors.append(f'术语翻译失败（{lang}）: {e}')

    def run_wave(wave):
        if not wave:
            return
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as ex:
            list(ex.map(work, wave))

    # 第一波：zh 源语种；第二波：en 源语种
    run_wave([b for b in batches if _wave_key(b) == 0])
    run_wave([b for b in batches if _wave_key(b) != 0])

    if errors:
        raise ValueError(errors[0])

    rows = [{'id': j['id'], 'source': j['source'], 'lang': j['lang'],
             'translation': results[(j['id'], j['lang'])]}
            for j in jobs]
    task.update({
        'status': 'done', 'progress': 100, 'phase': '术语翻译完成',
        'result': {'rows': rows, 'total': len(rows)},
    })


# ── 批量翻译 ────────────────────────────────────────────────────────────

ROW_SYSTEM_PROMPT = (
    '你是游戏本地化翻译专家。将给定的游戏文本从源语言翻译为目标语言。\n'
    '要求：\n'
    '1. 遵循游戏本地化风格，用词简洁自然，贴合游戏世界观，不要过度意译。\n'
    '2. 必须原样保留所有代码、占位符、标签、转义符（如 {0}、%s、<color=#fff>、\\n 等），'
    '一个都不能少，也不得多出。\n'
    '3. 源文本中若有用 ⟦术语⟧ 标注的受控术语：请正常翻译其内容（译文不要包含 ⟦⟧），'
    '并尽量在每项输出的 terms 字段报告其译文；没有标注术语时 terms 可为空对象。\n'
    '4. 只输出 JSON 数组，每项为 {"id": 序号, "translation": "译文", '
    '"terms": {"术语": "其译文"}}，不要输出其他内容。'
)


def _placeholder_list(text: str) -> list[str]:
    """Unique placeholder strings found in a source text."""
    found = extract_placeholders(text)
    seen = []
    for label, matches in found.items():
        for m in matches:
            if m not in seen:
                seen.append(m)
    return seen


def _verify(src: str, translation: str, placeholders: list[str],
            glossary: dict, limit: int | None) -> list[str]:
    problems = []
    for ph in placeholders:
        if ph and ph not in translation:
            problems.append(f'缺少占位符 {ph}')
    if limit is not None and limit > 0 and len(translation) > limit:
        problems.append(f'长度为 {len(translation)}，超过上限 {limit}')
    if glossary:
        src_l = src.lower()
        trans_l = translation.lower()
        for term, tgt in glossary.items():
            if term and term.lower() in src_l and tgt and tgt.lower() not in trans_l:
                problems.append(f'术语「{term}」应译为「{tgt}」')
    return problems


def _row_user_prompt(items: list[dict], src_lang: str, tgt_lang: str,
                     glossary: list[tuple], limits: dict) -> str:
    parts = [
        f'将以下文本从「{_lang_label(src_lang)}」翻译为「{tgt_lang}」：\n',
        json.dumps([{'id': it['id'], 'text': it['text']} for it in items],
                   ensure_ascii=False),
    ]
    limited = {it['id']: it['limit'] for it in items if it.get('limit')}
    if limited:
        parts.append('\n以下 id 的译文长度不得超过指定字符数: '
                     + json.dumps(limited, ensure_ascii=False))
    parts.append('\n\n请输出 JSON 数组（每项含 translation 与 terms 字段）。')
    return ''.join(parts)


_STRIP_TERM = re.compile(
    r'[\s　，。！？；：、（）《》'
    r'「」『』【】.,!?;:()"\'«»—\-⟦⟧]+')


def _mark_terms(text: str, gloss_terms) -> tuple[str, list[str]]:
    """把源文中的术语包上 ⟦⟧ 标注（按长度降序，避免 废土旧物市场 被 废土 抢先）。

    返回 (标注后文本, 命中的术语列表)。标注≠遮蔽：术语原文仍可见，模型整句
    正常翻译，术语留在语境里，流畅度/语序不受影响。
    """
    if not gloss_terms or not text:
        return text, []
    terms = sorted((t for t in gloss_terms if t), key=len, reverse=True)
    if not terms:
        return text, []
    pattern = re.compile('|'.join(re.escape(t) for t in terms))
    found = []

    def _repl(m):
        found.append(m.group(0))
        return f'⟦{m.group(0)}⟧'

    marked = pattern.sub(_repl, text)
    return marked, found


def _replace_rendering(translation: str, rendering: str, agreed: str) -> str:
    """把译文里模型对术语的渲染（rendering）替换为库内译法（agreed）。

    大小写不敏感；渲染未找到则原样返回（保持不动，不破坏句子）。
    """
    if not rendering or not agreed or not translation:
        return translation
    idx = translation.lower().find(rendering.lower())
    if idx == -1:
        return translation
    return translation[:idx] + agreed + translation[idx + len(rendering):]


def _strip_markers(text: str) -> str:
    """剥离译文里残留的 ⟦⟧ 标注（模型有时会把 ⟦term⟧ 原样带进译文）。"""
    if not text:
        return text or ''
    return text.replace('⟦', '').replace('⟧', '')


def _safe_term_replacement(source: str, translation: str, glossary: dict,
                           placeholders: list | None) -> str | None:
    """安全词边界替换：源去除占位符/标点后恰好是单个术语、且译文缺库内译法。

    仅在无占位符、源内容可确认=单个术语时整格替换，避免破坏长句译文（长句
    嵌入式术语无源↔译文对齐信息，不做盲替，靠条目级定向重译保证）。
    """
    if not glossary or not source or not translation or placeholders:
        return None
    stripped = _STRIP_TERM.sub('', source)
    if not stripped:
        return None
    trans_l = translation.lower()
    for term, tgt in glossary.items():
        if not term or not tgt:
            continue
        if stripped == term.strip() and tgt.lower() not in trans_l:
            return tgt
    return None


def _apply_term_consistency(out: dict, terms_map: dict, items: list[dict],
                            gloss_dict: dict) -> None:
    """用模型自报的 terms，把每条译文里与库内译法不一致的术语渲染替换为库内译法。

    术语已在源文中用 ⟦⟧ 标注（it['text'] 含 ⟦term⟧）。模型正常整句翻译，仅当
    其自报渲染 ≠ 库内译法时，把该渲染在译文里精确替换（外科式，不破坏句子）。
    """
    if not gloss_dict:
        return
    for it in items:
        trans = (out.get(it['id']) or out.get(str(it['id'])) or '').strip()
        if not trans:
            continue
        reported = terms_map.get(it['id']) or {}
        new_trans = trans
        for term, agreed in gloss_dict.items():
            if not term or not agreed or term not in it['text']:
                continue
            if agreed.lower() in new_trans.lower():
                continue                      # 已一致，无需动
            rendering = reported.get(term) or reported.get(f'⟦{term}⟧') or ''
            if rendering:
                new_trans = _replace_rendering(new_trans, rendering, agreed)
        out[it['id']] = new_trans


def _term_missing(it: dict, gloss_dict: dict, out: dict) -> list[tuple]:
    """该条中标注过、但库内译法仍未出现在当前译文里的术语对。"""
    if not gloss_dict:
        return []
    trans = (out.get(it['id']) or out.get(str(it['id'])) or '').lower()
    return [(t, g) for t, g in gloss_dict.items()
            if t and g and t in it['text'] and g.lower() not in trans]


def _chat_batch(llm_cfg: dict, items: list[dict], src_lang: str, tgt_lang: str,
                glossary: list[tuple], limits: dict) -> dict:
    """Translate one batch. 术语一致性由「标注 + 模型自报 terms + 程序替换」保证；
    另含占位符/超长纠错、术语兜底重译、缺失补翻与安全替换。"""
    messages = [
        {'role': 'system', 'content': ROW_SYSTEM_PROMPT},
        {'role': 'user', 'content': _row_user_prompt(
            items, src_lang, tgt_lang, glossary, limits)},
    ]
    try:
        data = chat_json(
            llm_cfg['base_url'], llm_cfg['api_key'], llm_cfg['model'],
            messages, temperature=0.2, max_tokens=_batch_max_tokens(items),
            timeout=None,  # 不设读超时上限，等模型返回
        )
    except LLMError:
        # 模型对大请求偶发空返回/超时：拆半重试（小请求通常可用），避免整批失败
        if len(items) <= 1:
            raise
        half = len(items) // 2
        left = _chat_batch(llm_cfg, items[:half], src_lang, tgt_lang,
                           glossary, limits)
        right = _chat_batch(llm_cfg, items[half:], src_lang, tgt_lang,
                            glossary, limits)
        left.update(right)
        return left
    out, terms_map = _parse_mapping(data)
    gloss_dict = dict(glossary) if glossary else {}

    # 1) 术语一致性：程序替换（主机制，不依赖模型自觉）
    _apply_term_consistency(out, terms_map, items, gloss_dict)

    # 2) 通用纠错（一次整批）：占位符缺失 / 超长（术语由上面程序替换保证，不再判术语）
    failed = []
    for it in items:
        trans = (out.get(it['id']) or out.get(str(it['id'])) or '').strip()
        problems = _verify(it['text'], trans, it.get('placeholders', []),
                           {}, it.get('limit'))
        if problems and trans:
            failed.append({'id': it['id'], 'text': it['text'], 'problems': problems})

    if failed:
        fix_messages = messages + [{
            'role': 'user',
            'content': ('以下条目的译文不合要求，请修正后重新输出这些条目的 JSON 数组：'
                        + json.dumps(
                            [{'id': f['id'], 'text': f['text'], '问题': f['problems']}
                             for f in failed], ensure_ascii=False)),
        }]
        try:
            merged, fix_terms = _parse_mapping(chat_json(
                llm_cfg['base_url'], llm_cfg['api_key'], llm_cfg['model'],
                fix_messages, temperature=0.2, max_tokens=_batch_max_tokens(failed),
                timeout=None,
            ))
            out.update(merged)
            terms_map.update(fix_terms)
            _apply_term_consistency(out, terms_map, items, gloss_dict)
        except LLMError:
            pass

    # 3) 术语兜底：标注了但库内译法仍未出现（模型漏 terms/替换未命中）→ 定向重译（≤2次）
    term_failed = [it for it in items if _term_missing(it, gloss_dict, out)]
    for _ in range(MISSING_RETRY):
        if not term_failed:
            break
        term_items = [{'id': it['id'], 'text': it['text'],
                       '术语译文': {t: g for t, g in _term_missing(it, gloss_dict, out)}}
                      for it in term_failed]
        term_messages = messages + [{
            'role': 'user',
            'content': ('以下条目的译文必须严格使用指定术语译文，不得用其他词，'
                        '请逐条重译，只输出这些条目的 JSON 数组：\n'
                        + json.dumps(term_items, ensure_ascii=False)),
        }]
        try:
            merged, fix_terms = _parse_mapping(chat_json(
                llm_cfg['base_url'], llm_cfg['api_key'], llm_cfg['model'],
                term_messages, temperature=0.2, max_tokens=_batch_max_tokens(term_failed),
                timeout=None,
            ))
            out.update(merged)
            terms_map.update(fix_terms)
            _apply_term_consistency(out, terms_map, items, gloss_dict)
        except LLMError:
            break
        term_failed = [it for it in term_failed if _term_missing(it, gloss_dict, out)]

    # 4) 空译文/缺失条目：独立小请求补翻（拆成小批次，模型对大批次易漏译）
    missing = [it for it in items
               if not (out.get(it['id']) or out.get(str(it['id'])) or '').strip()]
    for _ in range(MISSING_RETRY):
        if not missing:
            break
        for i in range(0, len(missing), MAX_RETRY_BATCH):
            chunk = missing[i:i + MAX_RETRY_BATCH]
            retry_messages = messages + [{
                'role': 'user',
                'content': ('以下条目必须翻译，不得遗漏或留空，只输出这些条目的 JSON 数组：'
                            + json.dumps([{'id': m['id'], 'text': m['text']}
                                          for m in chunk], ensure_ascii=False)),
            }]
            try:
                merged, _ = _parse_mapping(chat_json(
                    llm_cfg['base_url'], llm_cfg['api_key'], llm_cfg['model'],
                    retry_messages, temperature=0.2, max_tokens=_batch_max_tokens(chunk),
                    timeout=None,
                ))
                out.update(merged)
            except LLMError:
                pass  # 该小块失败则留待下轮
        missing = [it for it in missing
                   if not (out.get(it['id']) or out.get(str(it['id'])) or '').strip()]

    # 5) 安全词边界替换：可安全定位的短场景（源=单个术语）直接写入库内译法
    for it in items:
        trans = (out.get(it['id']) or out.get(str(it['id'])) or '').strip()
        if trans and _term_missing(it, gloss_dict, out):
            repl = _safe_term_replacement(it['text'], trans,
                                          gloss_dict, it.get('placeholders'))
            if repl:
                out[it['id']] = repl

    # 6) 剥离译文里残留的 ⟦⟧ 标注（模型有时会把 ⟦term⟧ 原样带进译文）
    for it in items:
        if out.get(it['id']) is not None:
            out[it['id']] = _strip_markers(str(out[it['id']]))

    return out


def _run_batch_wave(batches: list[tuple], glossaries: dict, out_df: pd.DataFrame,
                    task: dict, total: int, done: list, errors: list, lock,
                    llm_cfg: dict) -> None:
    """并发处理一波批次（zh 源或 en 源）。worker 只计算译文，主线程统一写表。"""

    def work(item):
        bi, (t, rows) = item
        lang = t['lang']
        gloss_items = glossaries[lang]
        try:
            items = [{'id': r['idx'], 'text': r['text'],
                      'placeholders': r['placeholders'], 'limit': r['limit']} for r in rows]
            # 精确命中术语 → 直接使用术语译文；其余标注术语后交模型整句翻译
            direct = {}
            rest = []
            for it in items:
                if gloss_items and it['text'].strip() in gloss_items:
                    direct[it['id']] = gloss_items[it['text'].strip()]
                else:
                    marked, _found = _mark_terms(it['text'],
                                                 set(gloss_items.keys()) if gloss_items else None)
                    it['text'] = marked
                    rest.append(it)

            if rest:
                # 只把当前批次内真实出现的术语放进提示词，缩小请求体
                joined = '\n'.join(x['text'] for x in rest).lower()
                rel_glossary = [(term, tgt) for term, tgt in gloss_items.items()
                                if term.lower() in joined][:200]
                out_map = _chat_batch(llm_cfg, rest, t.get('source_lang', 'zh'),
                                      lang, rel_glossary, {})
            else:
                out_map = {}
            out_map.update(direct)

            with lock:
                for idx, val in out_map.items():
                    out_df.at[idx, lang] = str(val or '').strip()
                done[0] += 1
                task.update({
                    'progress': round(done[0] / total * 100, 1),
                    'phase': f'正在翻译「{lang}」（第 {bi}/{total} 行）...',
                })
        except Exception as e:
            with lock:
                errors.append(f'翻译失败（{lang}，第 {bi}/{total} 行）: {e}')

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as ex:
        futures = [ex.submit(work, (bi, batch))
                   for bi, batch in enumerate(batches, 1)]
        for f in futures:
            f.result()


def run_translation(task: dict, project_id: int, project: dict, filepath: str,
                    targets: list[dict], llm_cfg: dict) -> None:
    """Batch-translate each target language from its resolved source column.

    targets: [{lang, source_col, source_lang, limit_col}] —— source_col /
    source_lang 已由项目配置在 app 层解析确定。
    依赖顺序：zh 源语种全部先并发翻译完成，en 源语种后并发翻译（en 列可能由本任务先产出）。
    Writes data/projects/<pid>/translations/<task_id>.xlsx and stores the
    first 50 rows as a preview in the task result.
    """
    df = read_excel(filepath, header=0)
    for t in targets:
        if t.get('source_col') and t['source_col'] not in df.columns:
            raise ValueError(f'源语言列「{t["source_col"]}」不存在')
        if t.get('limit_col') and t['limit_col'] not in df.columns:
            raise ValueError(f'字符上限列「{t["limit_col"]}」不存在')

    # 依赖顺序：源=中文 先，源=英文 后
    ordered = sorted(targets, key=lambda t: 0 if t.get('source_lang') == 'zh' else 1)

    out_df = df.copy()
    for t in targets:
        if t['lang'] not in out_df.columns:
            out_df[t['lang']] = ''

    # 每语种术语表只构建一次；zh 源用中文术语列，en 源用术语库 English 列
    glossaries = {}
    for t in targets:
        glossaries[t['lang']] = build_glossary_map(
            project_id, t['lang'], glossary_source_key(project, t.get('source_lang')))

    # 逐行翻译：每行一个请求（不做批次）——模型对稍大批次易漏译/空返回，
    # 单行请求最可靠；并发 8 + zh/en 两波分摊。
    def _build_batches(targets, from_df):
        out = []
        for t in targets:
            for idx, row in from_df.iterrows():
                src = str(row.get(t.get('source_col') or '', '')).strip()
                if not src or src == 'nan':
                    continue
                limit = None
                if t.get('limit_col') and t['limit_col'] in from_df.columns:
                    try:
                        limit = int(float(str(row[t['limit_col']]).strip()))
                    except (ValueError, TypeError):
                        limit = None
                out.append((t, [{
                    'idx': idx, 'text': src[:MAX_SRC_LEN],
                    'placeholders': _placeholder_list(src), 'limit': limit,
                }]))
        return out

    zh_targets = [t for t in ordered if t.get('source_lang') == 'zh']
    en_targets = [t for t in ordered if t.get('source_lang') != 'zh']

    zh_batches = _build_batches(zh_targets, df)
    # 先用 df 估算 en 批次数，作为总进度分母（en 实际从 out_df 读）
    en_est = _build_batches(en_targets, df)
    total = len(zh_batches) + len(en_est)
    task.update({'status': 'running', 'progress': 0,
                 'phase': f'准备翻译 {total} 行...'})

    lock = threading.Lock()
    done = [0]
    errors = []

    # 第一波：中文源语种（可能产出 English 列）→ 第二波：英文源语种
    if zh_batches:
        _run_batch_wave(zh_batches, glossaries, out_df, task, total, done, errors, lock,
                        llm_cfg)
        if errors:
            raise ValueError(errors[0])

    if en_targets:
        # 第二波 en 源：从 out_df 读取（若本任务先产出了 English 列，则用它，
        # 保证英→法/德 以术语库一致的英文为源，而非输入文件里可能与术语库不符的英文）
        en_batches = _build_batches(en_targets, out_df)
        if en_batches:
            _run_batch_wave(en_batches, glossaries, out_df, task, total, done, errors, lock,
                            llm_cfg)
            if errors:
                raise ValueError(errors[0])

    # 保存结果文件
    proj_dir = os.path.join(PROJECTS_DIR, str(project_id), 'translations')
    os.makedirs(proj_dir, exist_ok=True)
    out_path = os.path.join(proj_dir, f'{task["task_id"]}.xlsx')
    write_excel(out_path, out_df)

    # 预览用项目源列（中文）作为 source 展示
    preview_source_col = project['source_col_name'] if project['source_col_name'] in out_df.columns \
        else (targets[0].get('source_col') if targets else '')
    preview = []
    for idx, row in out_df.head(50).iterrows():
        preview.append({
            'idx': int(idx) + 2,  # 表头占1行
            'source': str(row.get(preview_source_col, '')),
            'translations': {t['lang']: str(row.get(t['lang'], '')) for t in targets},
        })

    task.update({
        'status': 'done', 'progress': 100, 'phase': '翻译完成',
        'result': {
            'preview': preview, 'total_rows': len(out_df),
            'file': os.path.basename(out_path),
            'languages': [t['lang'] for t in targets],
            'message': f'翻译完成，共 {len(out_df)} 行，目标语种: {", ".join(t["lang"] for t in targets)}',
        },
    })
