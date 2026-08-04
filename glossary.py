"""Project-level terminology library (术语库).

Stored as one Excel file per project:
    data/projects/<project_id>/glossary.xlsx
Columns: ID (auto-increment) | <source column name> | <lang1 name> | <lang2> | ...

The source column name and language names come from the project definition
(db.projects / db.project_languages), so the glossary format is project-fixed.
"""

import os

import pandas as pd

from config import PROJECTS_DIR
from excel_utils import read_excel, write_excel


def glossary_path(project_id: int) -> str:
    return os.path.join(PROJECTS_DIR, str(project_id), 'glossary.xlsx')


def ensure_glossary(project_id: int, source_col_name: str,
                    lang_names: list[str]) -> None:
    """Create the glossary file with the project's columns if it's missing."""
    path = glossary_path(project_id)
    if os.path.exists(path):
        return
    cols = ['ID', source_col_name] + [n for n in lang_names]
    write_excel(path, pd.DataFrame(columns=cols))


def read_glossary(project_id: int) -> pd.DataFrame:
    """Current glossary as a DataFrame (all cells strings, ID as int)."""
    path = glossary_path(project_id)
    if not os.path.exists(path):
        return pd.DataFrame(columns=['ID'])
    df = read_excel(path, header=0)
    if 'ID' in df.columns:
        df['ID'] = df['ID'].apply(
            lambda v: int(float(str(v))) if str(v).strip().lstrip('-').isdigit() else None)
    return df


def _next_id(df: pd.DataFrame) -> int:
    ids = pd.to_numeric(df['ID'], errors='coerce').dropna()
    return int(ids.max()) + 1 if len(ids) else 1


def add_terms(project_id: int, source_col_name: str, terms: list[str]) -> dict:
    """Append new source terms (deduplicated) with auto-increment IDs.

    Returns {'added': n, 'skipped': m} for reporting.
    """
    df = read_glossary(project_id)
    existing = set()
    if source_col_name in df.columns:
        existing = {str(v).strip() for v in df[source_col_name].dropna() if str(v).strip()}

    added = 0
    skipped = 0
    next_id = _next_id(df)
    for term in terms:
        term = (term or '').strip()
        if not term or term in existing:
            skipped += 1
            continue
        row = {'ID': next_id, source_col_name: term}
        for col in df.columns:
            if col not in row:
                row[col] = ''
        df.loc[len(df)] = row
        existing.add(term)
        next_id += 1
        added += 1

    if added:
        write_excel(glossary_path(project_id), df)
    return {'added': added, 'skipped': skipped}


def update_translations(project_id: int, updates: list[dict]) -> int:
    """Write per-language translations back into the glossary.

    updates: [{'id': int, 'lang': str, 'text': str}, ...]. Only rows whose
    'text' differs are rewritten. Returns the number of cells written.
    """
    if not updates:
        return 0
    df = read_glossary(project_id)
    if df.empty or 'ID' not in df.columns:
        return 0
    by_id = {int(r['ID']): i for i, r in df.iterrows() if pd.notna(r['ID'])}

    changed = 0
    for upd in updates:
        try:
            rid = int(upd['id'])
        except (TypeError, ValueError, KeyError):
            continue
        if rid not in by_id:
            continue
        lang = (upd.get('lang') or '').strip()
        text = (upd.get('text') or '').strip()
        if not lang or lang not in df.columns:
            continue
        row_idx = by_id[rid]
        if str(df.at[row_idx, lang]) != text:
            df.at[row_idx, lang] = text
            changed += 1

    if changed:
        write_excel(glossary_path(project_id), df)
    return changed


def overwrite_glossary(project_id: int, source_col_name: str,
                       lang_names: list[str], uploaded_path: str) -> dict:
    """Replace the glossary with an uploaded file.

    Normalizes the uploaded file: renumbers ID sequentially, keeps the source
    column, and (re)adds every project language column empty if missing.
    Returns {'rows': n, 'message': ...}.
    """
    df = read_excel(uploaded_path, header=0)

    # Keep only meaningful columns: ID + source col + project language cols.
    keep = ['ID']
    if source_col_name in df.columns:
        keep.append(source_col_name)
    keep += [n for n in lang_names if n in df.columns]
    df = df[[c for c in keep if c in df.columns]]

    # Ensure source + all language columns exist.
    for col in [source_col_name] + list(lang_names):
        if col not in df.columns:
            df[col] = ''

    df = df[[c for c in ['ID', source_col_name] + list(lang_names) if c in df.columns]]
    df = df.reset_index(drop=True)
    df['ID'] = range(1, len(df) + 1)
    df = df.fillna('')

    write_excel(glossary_path(project_id), df)
    return {'rows': len(df), 'message': f'术语库已覆盖，共 {len(df)} 条'}


def diff_glossary(project_id: int, source_col_name: str,
                  lang_names: list[str], uploaded_path: str) -> dict:
    """比对上传文件与当前术语库：新增/删除/修改了哪些（按源术语对比）。

    上传文件按与 overwrite_glossary 相同的归一化规则处理。
    """
    cur = read_glossary(project_id)
    new = read_excel(uploaded_path, header=0)

    keep = ['ID']
    if source_col_name in new.columns:
        keep.append(source_col_name)
    keep += [n for n in lang_names if n in new.columns]
    new = new[[c for c in keep if c in new.columns]]
    for col in [source_col_name] + list(lang_names):
        if col not in new.columns:
            new[col] = ''
    new = new[[c for c in ['ID', source_col_name] + list(lang_names) if c in new.columns]]
    new = new.fillna('')

    def to_map(df: pd.DataFrame) -> dict:
        m = {}
        if source_col_name not in df.columns:
            return m
        for _, row in df.iterrows():
            term = str(row.get(source_col_name, '')).strip()
            if not term or term == 'nan':
                continue
            m[term] = {l: str(row.get(l, '')).strip()
                       for l in lang_names if l in df.columns}
        return m

    cur_map = to_map(cur)
    new_map = to_map(new)
    cur_terms = set(cur_map)
    new_terms = set(new_map)

    added = sorted(new_terms - cur_terms)
    deleted = sorted(cur_terms - new_terms)

    modified = []
    for term in sorted(new_terms & cur_terms):
        changes = []
        for l in lang_names:
            o = cur_map[term].get(l, '')
            n = new_map[term].get(l, '')
            if o != n:
                changes.append({'lang': l, 'old': o, 'new': n})
        if changes:
            modified.append({'term': term, 'changes': changes[:5]})

    return {
        'added': added[:30], 'added_count': len(added),
        'deleted': deleted[:30], 'deleted_count': len(deleted),
        'modified': modified[:30], 'modified_count': len(modified),
        'unchanged_count': len(new_terms & cur_terms) - len(modified),
        'old_total': len(cur_terms), 'new_total': len(new_terms),
    }


def untranslated_terms(project_id: int, lang_names: list[str]) -> list[dict]:
    """Terms with at least one empty language column (i.e. not fully translated).

    A term counts as translated only when *every* project language column is
    non-empty. Returns [{id, source, missing_langs}].
    """
    df = read_glossary(project_id)
    if df.empty or 'ID' not in df.columns:
        return []
    lang_names = [n for n in lang_names if n in df.columns]
    if not lang_names:
        return []

    result = []
    for _, row in df.iterrows():
        missing = [n for n in lang_names if not str(row.get(n, '')).strip()]
        if missing:
            result.append({
                'id': int(row['ID']) if pd.notna(row['ID']) else None,
                'source': str(row.get(df.columns[1], '')) if len(df.columns) > 1 else '',
                'missing_langs': missing,
            })
    return result


def build_glossary_map(project_id: int, lang_name: str,
                       source_col_name: str) -> dict[str, str]:
    """Map source term -> approved translation for one target language."""
    df = read_glossary(project_id)
    if df.empty or source_col_name not in df.columns or lang_name not in df.columns:
        return {}
    mapping = {}
    for _, row in df.iterrows():
        src = str(row.get(source_col_name, '')).strip()
        tgt = str(row.get(lang_name, '')).strip()
        if src and src != 'nan' and tgt and tgt != 'nan':
            mapping[src] = tgt
    return mapping


def glossary_table(project_id: int, limit: int = 200) -> dict:
    """Serialize the glossary for the UI table."""
    df = read_glossary(project_id)
    if df.empty:
        return {'columns': [], 'rows': [], 'total': 0}
    cols = [str(c) for c in df.columns]
    rows = df.head(limit).where(pd.notna(df), '').values.tolist()
    rows = [[str(v) for v in r] for r in rows]
    return {'columns': cols, 'rows': rows, 'total': len(df)}
