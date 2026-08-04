"""实测：同一文件（中→英 25 行）不同批次大小的时间 / 空返回 / 请求数 / token 量。"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('LOCALIZEDTOOL_DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))

import pandas as pd

import llm
import translate
from checkers.placeholder import extract_placeholders
from cryptography.fernet import Fernet

conn = sqlite3.connect(r'E:\WorkSpecs\Claude\LocalizedTool\data\app.db')
row = conn.execute("SELECT base_url, model, api_key_enc FROM user_llm_settings LIMIT 1").fetchone()
conn.close()
fernet = Fernet(open(r'E:\WorkSpecs\Claude\LocalizedTool\data\.secret_key', 'rb').read().strip())
cfg = {'base_url': row[0], 'model': row[1],
       'api_key': fernet.decrypt(row[2].encode()).decode()}

SRC = r"C:\Users\Administrator\Downloads\【旧物市场活动】- 龙思睿 - 英俄繁日德法韩葡西 - 7月20号-EN-en-de-ru-fr-pt-es-T-C.xlsx"
df = pd.read_excel(SRC, dtype=str).fillna('')
texts = [str(v).strip() for v in df['中文(简体)'] if str(v).strip() and str(v) != 'nan']
print('源行数:', len(texts))

GLOSS = [('废土旧物市场', 'Wasteland Flea Market'), ('幸存者', 'Survivor'),
         ('抗丧宁', 'Anti-plague'), ('随行主宰自选箱', 'Companion Overlord Selector Box'),
         ('指挥官', 'Commander')]
GLOSS_KEYS = set(k for k, _ in GLOSS)

# 统计真实请求
orig_chat = llm.chat
stats = {'n': 0, 'in_chars': 0, 'out_chars': 0}


def counting_chat(base_url, api_key, model, messages, **kw):
    stats['n'] += 1
    for m in messages:
        stats['in_chars'] += len(str(m.get('content', '')))
    r = orig_chat(base_url, api_key, model, messages, **kw)
    stats['out_chars'] += len(str(r or ''))
    return r


llm.chat = counting_chat


def run_batch_size(N):
    stats['n'] = stats['in_chars'] = stats['out_chars'] = 0
    t0 = time.time()
    out = {}
    for i in range(0, len(texts), N):
        chunk = texts[i:i + N]
        items = []
        for j, t in enumerate(chunk, start=i):
            marked, _ = translate._mark_terms(t[:800], GLOSS_KEYS)
            items.append({'id': j, 'text': marked,
                          'placeholders': list(extract_placeholders(t).keys()),
                          'limit': None})
        res = translate._chat_batch(cfg, items, 'zh', 'English', GLOSS, {})
        for k, v in res.items():
            out[k] = v
    dt = time.time() - t0
    empty = sum(1 for v in out.values() if not str(v).strip())
    reqs = stats['n']
    print('批%2d行: 请求%2d次 | 输入%6d字符 输出%6d字符 | %.1fs | 空%d/%d'
          % (N, reqs, stats['in_chars'], stats['out_chars'], dt, empty, len(out)))
    return empty


print('\n=== 各批次大小实测（中→英，25 行） ===')
for N in (1, 5, 10, 15, 25):
    try:
        run_batch_size(N)
    except Exception as e:
        print('批%d行: 失败 %s' % (N, str(e)[:70]))
