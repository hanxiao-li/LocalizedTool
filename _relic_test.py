"""验证 旧物/废土旧物市场 的 s 时有时无：真实术语库、真实 API。"""
import os
import sqlite3
import sys
import tempfile
import time

os.environ['LOCALIZEDTOOL_DATA_DIR'] = tempfile.mkdtemp(prefix='relic_')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import db
import glossary as gl
import translate
from config import PROJECTS_DIR
from excel_utils import read_excel
from cryptography.fernet import Fernet

db.init_db()
pid = db.create_project('RL', '中文(简体)', 'zh', '', 1)
db.set_project_languages(pid, [{'name': 'English', 'source_lang': 'zh'},
                               {'name': 'Français', 'source_lang': 'en'}])
gl.ensure_glossary(pid, '中文(简体)', ['English', 'Français'])
# 直接覆盖为真实术语库（避免临时建库 ID 错位）
gl.overwrite_glossary(pid, '中文(简体)', ['English', 'Français'],
                      r'E:\WorkSpecs\Claude\LocalizedTool\data\projects\1\glossary.xlsx')

# 打印临时术语库中的 旧物 / 废土旧物市场
g = gl.build_glossary_map(pid, 'English', '中文(简体)')
gf = gl.build_glossary_map(pid, 'Français', 'English')
print('en→fr 旧物/废土旧物市场:', {k: v for k, v in gf.items() if 'Relic' in k or 'reliqu' in v})
print('zh→en 旧物/废土旧物市场:', {k: v for k, v in g.items() if '旧物' in k})

df = pd.read_excel(r"C:\Users\Administrator\Downloads\【旧物市场活动】- 龙思睿 - 英俄繁日德法韩葡西 - 7月20号-EN-en-de-ru-fr-pt-es-T-C.xlsx",
                   dtype=str).fillna('')
rows = []
for _, r in df.iterrows():
    zh = str(r.get('中文(简体)', '')).strip()
    en = str(r.get('English', '')).strip()
    if zh and zh != 'nan':
        rows.append({'中文(简体)': zh, 'English': en if en and en != 'nan' else ''})
inp = os.path.join(tempfile.gettempdir(), 'rl.xlsx')
pd.DataFrame(rows).to_excel(inp, index=False)

conn = sqlite3.connect(r'E:\WorkSpecs\Claude\LocalizedTool\data\app.db')
row = conn.execute("SELECT base_url, model, api_key_enc FROM user_llm_settings LIMIT 1").fetchone()
conn.close()
fernet = Fernet(open(r'E:\WorkSpecs\Claude\LocalizedTool\data\.secret_key', 'rb').read().strip())
cfg = {'base_url': row[0], 'model': row[1],
       'api_key': fernet.decrypt(row[2].encode()).decode()}

task = {'task_id': 'rl', 'status': 'running', 'progress': 0, 'phase': '', 'result': None, 'error': None}
t0 = time.time()
translate.run_translation(task, pid, db.get_project(pid), inp,
                          [{'lang': 'English', 'source_col': '中文(简体)', 'source_lang': 'zh', 'limit_col': None},
                           {'lang': 'Français', 'source_col': 'English', 'source_lang': 'en', 'limit_col': None}],
                          cfg)
print('完成 %.1fs' % (time.time() - t0))
out = read_excel(os.path.join(PROJECTS_DIR, str(pid), 'translations', task['result']['file']))
print('\n=== 含 旧物/废土旧物市场 的行 ===')
for _, r in out.iterrows():
    zh = str(r.get('中文(简体)', ''))
    if '旧物' in zh or '废土旧物市场' in zh:
        print('  [源]', zh[:30])
        print('    英:', str(r.get('English', ''))[:60], '| 含 Relic(s)?', 'Relics' in str(r.get('English','')) or 'Relic' in str(r.get('English','')))
        print('    法:', str(r.get('Français', ''))[:60], '| 含 reliqu?', 'reliqu' in str(r.get('Français','')))
