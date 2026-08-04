"""翻译校验引擎（功能四）。

相对参考 LQA 的三点优化：
  1. 术语校对直接使用项目术语库（不再要求重新上传术语库文件）；
  2. 不保存/加载旧项目的语言对配置，使用新项目体系；
  3. 不手动设置语言对 —— 根据上传文件列名自动判定语种。

校验项（9 项）：completeness / placeholder / punctuation / length /
terminology / numbers / escape / whitespace / spell（取消敏感词）。
"""

import json
import logging
import os
import uuid

from checkers import (
    CompletenessChecker,
    PlaceholderChecker,
    PunctuationChecker,
    LengthChecker,
    TerminologyChecker,
    NumbersChecker,
    EscapeChecker,
    WhitespaceChecker,
    SpellChecker,
)
from config import PROJECTS_DIR
from excel_utils import read_excel, write_excel
from glossary import build_glossary_map
from languages import glossary_source_key

logger = logging.getLogger(__name__)

LANG_NAMES = {
    'zh-cn': '中文(简体)', 'zh-tw': '中文(繁体)', 'zh-hk': '中文(香港)',
    'ja': '日本語', 'ko': '한국어', 'en': 'English', 'en-us': 'English (US)',
    'en-gb': 'English (UK)', 'de': 'Deutsch', 'fr': 'Français',
    'es': 'Español', 'pt': 'Português', 'pt-br': 'Português (BR)',
    'ru': 'Русский', 'tr': 'Türkçe', 'it': 'Italiano',
    'nl': 'Nederlands', 'pl': 'Polski', 'sv': 'Svenska',
    'da': 'Dansk', 'ar': 'العربية', 'th': 'ไทย',
    'vi': 'Tiếng Việt', 'id': 'Bahasa Indonesia', 'ms': 'Bahasa Melayu',
}

# 语言显示名 -> 语言代码（用于校验器）。大小写不敏感。
LANG_CODE_MAP = {v.lower(): k for k, v in LANG_NAMES.items()}
LANG_CODE_MAP.update({
    '中文': 'zh-cn', '中文(简体)': 'zh-cn', '简体中文': 'zh-cn', '简体': 'zh-cn',
    'chinese': 'zh-cn', 'chinese simplified': 'zh-cn', 'chinese(simplified)': 'zh-cn',
    'chinese (simplified)': 'zh-cn', 'simplified chinese': 'zh-cn',
    'chinese traditional': 'zh-tw', 'chinese (traditional)': 'zh-tw',
    'traditional chinese': 'zh-tw',
    'english': 'en', 'english (us)': 'en-us', 'english (uk)': 'en-gb',
    'japanese': 'ja', 'korean': 'ko', 'deutsch': 'de', 'german': 'de',
    'french': 'fr', 'français': 'fr', 'spanish': 'es', 'español': 'es',
    'portuguese': 'pt', 'português': 'pt', 'russian': 'ru', 'русский': 'ru',
    'turkish': 'tr', 'türkçe': 'tr', 'italian': 'it', 'italiano': 'it',
    'dutch': 'nl', 'nederlands': 'nl', 'polish': 'pl', 'polski': 'pl',
    'swedish': 'sv', 'svenska': 'sv', 'danish': 'da', 'dansk': 'da',
    'arabic': 'ar', 'العربية': 'ar', 'thai': 'th', 'ไทย': 'th',
    'vietnamese': 'vi', 'tiếng việt': 'vi', 'indonesian': 'id',
    'bahasa indonesia': 'id', 'malay': 'ms', 'bahasa melayu': 'ms',
})

CHECK_LABELS = {
    'completeness': '翻译语言空或翻成其他语言判定', 'placeholder': '占位符/标签',
    'punctuation': '标点规范', 'length': '长度限制', 'terminology': '术语一致性',
    'numbers': '数字一致性', 'escape': '转义符', 'whitespace': '首尾空格',
    'spell': '拼写检查',
}

_ALL_CHECKS = list(CHECK_LABELS.keys())


def lang_name_to_code(name: str) -> str:
    """Best-effort language display name -> standard code (default en)."""
    key = (name or '').strip().lower()
    return LANG_CODE_MAP.get(key, 'en')


def source_lang_code(source_lang: str) -> str:
    """Project source_lang ('zh'/'en') -> checker code ('zh-cn'/'en')."""
    return {'zh': 'zh-cn', 'en': 'en'}.get(source_lang, source_lang)


def auto_detect_pairs(df_columns: list[str], source_col_name: str,
                      lang_names: list[str]) -> dict:
    """Detect source + target columns from file column names.

    精确名称优先；再按语言代码匹配（兼容『中文』→『中文(简体)』等别名），
    无法识别的列不会误判为英文。
    """
    from languages import known_lang_code, lang_name

    source_col = source_col_name if source_col_name in df_columns else None
    normalized = [lang_name(known_lang_code(n) or '') for n in lang_names]
    target_pairs = []  # [{col, lang}]
    seen = set()

    for col in df_columns:
        col_s = str(col)
        if col_s == source_col_name:
            source_col = col_s
            continue
        if col_s in normalized:
            target_pairs.append({'col': col_s, 'lang': col_s})
            seen.add(col_s)

    # 按语言代码匹配别名（未精确匹配到的列）
    for col in df_columns:
        col_s = str(col)
        if col_s == source_col_name or col_s in seen:
            continue
        code = known_lang_code(col_s)
        if code:
            for lname in normalized:
                if known_lang_code(lname) == code:
                    target_pairs.append({'col': col_s, 'lang': lname})
                    seen.add(col_s)
                    break

    # 源列未精确命中 → 按代码找
    if source_col is None:
        src_code = known_lang_code(source_col_name)
        for col in df_columns:
            col_s = str(col)
            if src_code and known_lang_code(col_s) == src_code:
                source_col = col_s
                break
    # 仍无源列：取第一个未被识别为目标语的列
    if source_col is None:
        for col in df_columns:
            if str(col) not in seen:
                source_col = str(col)
                break
    return {'source_col': source_col, 'pairs': target_pairs}


def _get_limit_col(length_limits: dict, target_col: str) -> str | None:
    for item in (length_limits or []):
        if item.get('column') == target_col:
            return item.get('limit_column') or None
    return None


def run_check(task: dict, filepath: str, pairs: list[dict],
              length_limits: list[dict], enabled_checks: list[str],
              project: dict) -> None:
    """Run all checks in a background task, updating progress.

    pairs: [{lang, source_col, target_col, source_lang}] — 每语种独立的
    源列/目标列（已由项目配置在 app 层解析确定）。术语一致性按语种源语言
    选择术语库源键列（zh 源→中文术语列，en 源→术语库 English 列）。
    下载结果 = 上传原表 + 每被校验语种一列「问题_<语种名>」。
    """
    df = read_excel(filepath, header=0)
    for p in pairs:
        if p.get('source_col') and p['source_col'] not in df.columns:
            raise ValueError(f'源语言列「{p["source_col"]}」不存在')
        if p.get('target_col') and p['target_col'] not in df.columns:
            raise ValueError(f'目标语言列「{p["target_col"]}」不存在')

    enabled = enabled_checks or _ALL_CHECKS
    glossary = {}
    for p in pairs:
        if 'terminology' in enabled:
            glossary[p['lang']] = build_glossary_map(
                project['id'], p['lang'],
                glossary_source_key(project, p.get('source_lang')))

    # 有效语言对
    valid_pairs = []
    total_items = 0
    for p in pairs:
        if p.get('target_col') in df.columns and p.get('source_col') in df.columns:
            total_items += len(df)
            valid_pairs.append(p)

    if not valid_pairs:
        raise ValueError('没有有效的目标语言列')

    task.update({'status': 'running', 'progress': 0, 'total': total_items,
                 'completed': 0, 'phase': '初始化检查器...'})

    all_results = []
    completed = 0

    for p in valid_pairs:
        src_col = p['source_col']
        tgt_col = p['target_col']
        src_lang_code = source_lang_code(p.get('source_lang') or project['source_lang'])
        src_lang_name = LANG_NAMES.get(src_lang_code, src_lang_code)
        tgt_lang_code = lang_name_to_code(p['lang'])
        tgt_lang_name = LANG_NAMES.get(tgt_lang_code, p['lang'])
        limit_col = _get_limit_col(length_limits, tgt_col)

        checkers = {}
        if 'completeness' in enabled:
            checkers['completeness'] = CompletenessChecker(tgt_lang_code)
        if 'placeholder' in enabled:
            checkers['placeholder'] = PlaceholderChecker(tgt_lang_code)
        if 'length' in enabled and limit_col:
            checkers['length'] = LengthChecker(tgt_lang_code)
        if 'terminology' in enabled and glossary.get(p['lang']):
            checkers['terminology'] = TerminologyChecker(tgt_lang_code, glossary[p['lang']])
        if 'numbers' in enabled:
            checkers['numbers'] = NumbersChecker(tgt_lang_code, src_lang_code)
        if 'escape' in enabled:
            checkers['escape'] = EscapeChecker(tgt_lang_code)
        if 'whitespace' in enabled:
            checkers['whitespace'] = WhitespaceChecker(tgt_lang_code)

        spell_checker = SpellChecker(tgt_lang_code, set()) if 'spell' in enabled else None
        punct_checker = PunctuationChecker(tgt_lang_code) if 'punctuation' in enabled else None

        task['phase'] = f'检查语言: {src_lang_name} → {tgt_lang_name}'

        spell_rows = []
        punct_rows = []

        for idx, row in df.iterrows():
            source_text = str(row.get(src_col, ''))
            target_text = str(row.get(tgt_col, ''))
            if (not source_text or not source_text.strip()) and \
               (not target_text or not target_text.strip()):
                completed += 1
                continue

            row_limit = None
            if limit_col and limit_col in df.columns:
                try:
                    ls = str(row.get(limit_col, '')).strip()
                    if ls and ls != 'nan':
                        row_limit = int(float(ls))
                except (ValueError, TypeError):
                    row_limit = None

            for name, checker in checkers.items():
                try:
                    if name == 'length':
                        res = checker.check(source_text, target_text, idx,
                                            source_col=src_col, target_col=tgt_col,
                                            limit_value=row_limit)
                    else:
                        res = checker.check(source_text, target_text, idx,
                                            source_col=src_col, target_col=tgt_col)
                    for r in res:
                        r.check_type = name
                        r.language = tgt_lang_name
                        r.lang_code = tgt_lang_code
                        r.source_lang = src_lang_name
                        r.column = tgt_col
                    all_results.extend(res)
                except Exception as e:
                    logger.error(f'Checker {name} failed at row {idx}: {e}')

            if spell_checker and target_text and target_text.strip():
                spell_rows.append((idx, source_text, target_text))
            if punct_checker and target_text and target_text.strip():
                punct_rows.append((idx, source_text, target_text))

            completed += 1
            if completed % 20 == 0 or completed == total_items:
                task.update({
                    'progress': round(completed / total_items * 100, 1),
                    'completed': completed,
                    'phase': f'检查中... ({completed}/{total_items})',
                })

        if spell_checker and spell_rows:
            task['phase'] = f'拼写检查: {tgt_lang_name} ({len(spell_rows)}行)...'
            try:
                for r in spell_checker.batch_check(
                        spell_rows, source_col=src_col, target_col=tgt_col):
                    r.language = tgt_lang_name
                    r.source_lang = src_lang_name
                    all_results.append(r)
            except Exception as e:
                logger.error(f'Batch spell check failed for {tgt_lang_name}: {e}')

        if punct_checker and punct_rows:
            task['phase'] = f'标点检查: {tgt_lang_name} ({len(punct_rows)}行)...'
            try:
                for r in punct_checker.batch_check(
                        punct_rows, source_col=src_col, target_col=tgt_col):
                    r.language = tgt_lang_name
                    r.source_lang = src_lang_name
                    all_results.append(r)
            except Exception as e:
                logger.error(f'Batch punct check failed for {tgt_lang_name}: {e}')

    # 构建结果
    results_data = []
    for r in all_results:
        results_data.append({
            'row': r.row,
            'target_language': getattr(r, 'language', ''),
            'source_language': getattr(r, 'source_lang', ''),
            'column': r.column,
            'check_type': r.check_type,
            'check_label': CHECK_LABELS.get(r.check_type, r.check_type),
            'issue': r.issue,
            'severity': r.severity,
            'details': r.details or '',
            'source_text': (r.source_text or '')[:200],
            'target_text': (r.target_text or '')[:200],
        })

    severity_order = {'error': 0, 'warning': 1, 'info': 2}
    results_data.sort(key=lambda x: (severity_order.get(x['severity'], 9),
                                     x['check_type'], x['row']))

    total_issues = len(results_data)
    errors = sum(1 for r in results_data if r['severity'] == 'error')
    warnings = sum(1 for r in results_data if r['severity'] == 'warning')
    infos = sum(1 for r in results_data if r['severity'] == 'info')

    pair_stats = []
    for p in valid_pairs:
        pr = [r for r in results_data if r['target_language'] == LANG_NAMES.get(
            lang_name_to_code(p['lang']), p['lang'])]
        pair_stats.append({
            'source_lang': LANG_NAMES.get(
                source_lang_code(p.get('source_lang') or project['source_lang']),
                p.get('source_lang') or project['source_lang']),
            'target_lang': p['lang'],
            'total': len(pr),
            'errors': sum(1 for r in pr if r['severity'] == 'error'),
            'warnings': sum(1 for r in pr if r['severity'] == 'warning'),
        })

    # 保存结果 JSON + 生成下载 Excel（上传原表 + 每被校验语种一列「问题_语种」）
    results_id = uuid.uuid4().hex
    proj_dir = os.path.join(PROJECTS_DIR, str(project['id']), 'check_results')
    os.makedirs(proj_dir, exist_ok=True)
    json_path = os.path.join(proj_dir, f'{results_id}.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(results_data, f, ensure_ascii=False)

    out_df = df.copy()
    issue_cols = []
    for p in valid_pairs:
        tgt_lang_name = LANG_NAMES.get(lang_name_to_code(p['lang']), p['lang'])
        col = f'问题_{tgt_lang_name}'
        if col not in out_df.columns:
            out_df[col] = ''
        issue_cols.append(col)
    for r in results_data:
        col = f'问题_{r["target_language"]}'
        if col not in out_df.columns:
            continue
        line = f'[{r["severity"]}] {r["check_label"]}: {r["issue"]}'
        if r.get('details'):
            line += f'（{r["details"]}）'
        idx = r['row'] - 1
        if idx in out_df.index:
            cur = str(out_df.at[idx, col])
            out_df.at[idx, col] = f'{cur}\n{line}' if cur else line
    xlsx_path = os.path.join(proj_dir, f'{results_id}.xlsx')
    write_excel(xlsx_path, out_df)

    task.update({
        'status': 'done', 'progress': 100, 'phase': '检查完成',
        'result': {
            'total_results': total_issues,
            'displayed': min(total_issues, 200),
            'errors': errors, 'warnings': warnings, 'infos': infos,
            'results': results_data[:200],
            'results_id': results_id,
            'pair_stats': pair_stats,
            'message': f'检查完成: {len(valid_pairs)}个语言对, 共 {total_issues} 个问题'
                       f' ({errors}错误, {warnings}警告, {infos}提示)',
        },
    })
