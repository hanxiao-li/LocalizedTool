"""术语提取：通过大模型从源语言列中提取专有名词/术语。

流程：读取已上传 Excel 的源语言列 → 去重 → 分批送入 LLM → 汇总候选术语
（含出现次数），供用户在页面上逐条/批量确认后写入项目术语库。
"""

import logging

from excel_utils import read_excel
from llm import chat_json, LLMError

logger = logging.getLogger(__name__)

# 每批送入的文本条数（避免超长）
BATCH_SIZE = 300
# 单条文本送入模型前截断的字符数
MAX_TEXT_LEN = 300
# 最多处理多少条唯一文本（防御性上限）
MAX_UNIQUE = 100000

EXTRACT_SYSTEM_PROMPT = (
    '你是游戏本地化的术语提取专家。你的任务是从给定的游戏文本中提取'
    '【专有名词、专有术语、游戏特有名词、重要且反复出现的词组】。\n'
    '规则：\n'
    '1. 只输出真正需要在翻译中统一口径的术语，不要输出普通句子或常见词。\n'
    '2. 术语应为词或短语，一般不超过 10 个字。\n'
    '3. 结果去重，不要重复输出。\n'
    '4. 只输出 JSON，格式为对象 {"terms": ["术语1", "术语2", ...]}，不要输出其他内容。'
)


def _build_user_prompt(batch: list[str], source_lang: str) -> str:
    lang_desc = {'zh': '中文', 'en': '英文'}.get(source_lang, source_lang)
    lines = '\n'.join(f'- {t}' for t in batch)
    return (
        f'以下是待提取的游戏文本（{lang_desc}），每行一条：\n{lines}\n\n'
        '请按规则提取术语，输出 JSON 对象 {"terms": [...]}。'
    )


def run_extract(task: dict, filepath: str, source_col: str,
                source_lang: str, llm_cfg: dict) -> None:
    """Extract terms from the source column into task['result']['candidates']."""
    df = read_excel(filepath, header=0)
    if source_col not in df.columns:
        raise ValueError(f'源语言列「{source_col}」不存在')

    # 去重 + 计数
    counter = {}
    for v in df[source_col].dropna():
        s = str(v).strip()
        if s and s != 'nan':
            counter[s] = counter.get(s, 0) + 1

    unique = list(counter.keys())
    if len(unique) > MAX_UNIQUE:
        unique = unique[:MAX_UNIQUE]
    if not unique:
        raise ValueError('源语言列没有可提取的非空内容')

    batches = [unique[i:i + BATCH_SIZE] for i in range(0, len(unique), BATCH_SIZE)]
    total = len(batches)
    task.update({'status': 'running', 'progress': 0, 'phase': f'准备提取 {len(unique)} 条文本，共 {total} 批...'})

    candidates = {}  # term -> {'count': n, 'sources': [...]}

    for bi, batch in enumerate(batches, 1):
        messages = [
            {'role': 'system', 'content': EXTRACT_SYSTEM_PROMPT},
            {'role': 'user', 'content': _build_user_prompt(
                [t[:MAX_TEXT_LEN] for t in batch], source_lang)},
        ]
        try:
            data = chat_json(
                llm_cfg['base_url'], llm_cfg['api_key'], llm_cfg['model'],
                messages, temperature=0.2, max_tokens=4096,
            )
        except LLMError as e:
            raise ValueError(f'第 {bi}/{total} 批提取失败: {e}')

        terms = data.get('terms') if isinstance(data, dict) else data
        if not isinstance(terms, list):
            raise ValueError(f'第 {bi}/{total} 批返回格式不正确: {str(data)[:200]}')

        for term in terms:
            if not isinstance(term, str):
                continue
            term = term.strip()
            if not term or len(term) > 50:
                continue
            if term not in candidates:
                # 统计该术语在原文中出现的行数
                candidates[term] = {
                    'term': term,
                    'count': sum(1 for src, c in counter.items() if term in src),
                    'sources': [],
                }
            if len(candidates[term]['sources']) < 3:
                for src in counter:
                    if term in src:
                        candidates[term]['sources'].append(src)
                        if len(candidates[term]['sources']) >= 3:
                            break

        progress = round(bi / total * 100, 1)
        task.update({
            'progress': progress,
            'phase': f'正在提取术语（第 {bi}/{total} 批）...',
        })

    cand_list = sorted(
        candidates.values(), key=lambda c: c['count'], reverse=True)
    task.update({
        'status': 'done',
        'progress': 100,
        'phase': '术语提取完成',
        'result': {'candidates': cand_list, 'total': len(cand_list)},
    })
