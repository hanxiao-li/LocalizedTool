/* LocalizedTool — main frontend logic (no framework, no CDN). */

(function () {
  'use strict';

  const state = {
    user: null,
    projects: [],
    currentProject: null,
    candidates: [],
    translateTargets: [],
    checkLangs: [],
    lastTranslateTask: null,
    lastCheckResultsId: null,
    langNames: [],
    extractColumns: [],
    editingProjectId: null,
  };

  // ── Utilities ─────────────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function api(path, { method = 'GET', body = null, isForm = false } = {}) {
    const headers = isForm ? {} : { 'Content-Type': 'application/json' };
    let payload;
    if (body == null) payload = undefined;
    else if (isForm) payload = body;
    else payload = JSON.stringify(body);

    const res = await fetch(path, { method, headers, body: payload });
    // 登录接口本身会返回 401（用户名/密码错误），不能当成"会话过期"跳转刷新
    if (res.status === 401 && path !== '/api/login') {
      location.href = '/login'; throw new Error('未登录');
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || '请求失败(' + res.status + ')');
    return data;
  }

  function toast(msg, isError = false) {
    const box = $('#toastBox');
    if (!box) return alert(msg);
    const el = document.createElement('div');
    el.className = 'toast ' + (isError ? 'toast-error' : '');
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  function openModal(id) { $('#' + id).hidden = false; }
  function closeModal(id) { $('#' + id).hidden = true; }

  function fillSelect(sel, options, value) {
    sel.innerHTML = '';
    options.forEach((o) => {
      const opt = document.createElement('option');
      opt.value = o;
      opt.textContent = o;
      sel.appendChild(opt);
    });
    if (value != null) sel.value = value;
  }

  function showTaskError(msg) {
    $('#taskErrorText').textContent = msg || '任务执行失败，请查看服务端日志';
    openModal('modalTaskError');
  }

  function pollTask(taskId, onUpdate, onDone) {
    const iv = setInterval(async () => {
      try {
        const t = await api('/api/tasks/' + taskId);
        onUpdate(t);
        if (t.status === 'done' || t.status === 'error') {
          clearInterval(iv);
          if (t.status === 'error') showTaskError(t.error);
          onDone(t);
        }
      } catch (e) { clearInterval(iv); toast(e.message, true); }
    }, 1000);
  }

  function setProgress(barSel, phaseSel, t) {
    const bar = $(barSel);
    if (bar) {
      bar.style.width = (t.progress || 0) + '%';
      bar.textContent = (t.progress || 0) + '%';
    }
    const phase = $(phaseSel);
    if (phase) phase.textContent = t.phase || '';
  }

  // ── Global / login ────────────────────────────────────────────────────

  async function refreshMe() {
    const me = await api('/api/me');
    state.user = me.user;
    state.projects = me.user.projects;
    $('#userBadge').textContent = state.user.username + (state.user.is_admin ? '（管理员）' : '');
    const settingsAdminTab = $('#settingsAdminTab');
    if (settingsAdminTab) settingsAdminTab.hidden = !state.user.is_admin;

    // 项目下拉
    const sel = $('#projectSelect');
    sel.innerHTML = '<option value="">-- 选择项目 --</option>';
    state.projects.forEach((p) => {
      const opt = document.createElement('option');
      opt.value = p.id; opt.textContent = p.name;
      sel.appendChild(opt);
    });
    const cur = await api('/api/project/current');
    if (cur.project) {
      state.currentProject = cur.project;
      sel.value = cur.project.id;
      $('#projectSelect').dataset.ready = '1';
    }
    updateProjectGate();
  }

  function updateProjectGate() {
    const hasProject = !!state.currentProject;
    $('#noProjectNotice').hidden = hasProject;
    $$('.tab-panel').forEach((p) => { p.hidden = !hasProject; });
  }

  async function selectProjectById(pid) {
    const data = await api('/api/project/select', { method: 'POST', body: { project_id: pid } });
    state.currentProject = data.project;
    state.candidates = [];
    state.lastTranslateTask = null;
    state.lastCheckResultsId = null;
    updateProjectGate();
    await refreshAll();
    toast('已切换到项目「' + data.project.name + '」');
  }

  // ── Glossary ──────────────────────────────────────────────────────────

  async function refreshGlossary() {
    const data = await api('/api/glossary');
    const head = $('#glossaryHead'), body = $('#glossaryBody');
    head.innerHTML = '<tr>' + data.columns.map((c) => '<th>' + esc(c) + '</th>').join('') + '</tr>';
    if (!data.rows.length) {
      body.innerHTML = '<tr><td colspan="' + Math.max(data.columns.length, 1) +
        '" class="muted">暂无术语，可先在上方进行术语提取并确认。</td></tr>';
    } else {
      body.innerHTML = data.rows.map((r) =>
        '<tr>' + r.map((c) => '<td>' + esc(c) + '</td>').join('') + '</tr>').join('');
    }
    $('#glossaryInfo').textContent = '共 ' + data.total + ' 条 · 源语言列「' +
      data.source_col_name + '」 · 语种: ' + (data.languages.join(', ') || '无');
  }

  // ── Extract tab ───────────────────────────────────────────────────────

  function findZhColumn(cols) {
    const names = ['中文(简体)', '中文', '简体中文', '中文简体',
      'Chinese', 'chinese', 'Chinese (Simplified)', 'chinese(simplified)',
      'Simplified Chinese'];
    for (const n of names) {
      const hit = (cols || []).find((c) => String(c).trim() === n);
      if (hit) return hit;
    }
    return (cols || []).find((c) => /中文/.test(String(c))) || null;
  }

  async function uploadExtract() {
    const file = $('#extractFile').files[0];
    if (!file) return toast('请选择 Excel 文件', true);
    const fd = new FormData();
    fd.append('file', file); fd.append('tab', 'extract');
    try {
      const data = await api('/api/upload', { method: 'POST', body: fd, isForm: true });
      state.extractColumns = data.columns;
      const zh = findZhColumn(data.columns);
      $('#extractSourceInfo').textContent = zh
        ? '自动识别: ' + zh
        : '⚠️ 未找到中文（简体）列，提取将无法进行';
      $('#extractUploadInfo').textContent = data.message;
      $('#extractResultCard').hidden = true;
      toast('上传成功');
    } catch (e) { toast(e.message, true); }
  }

  async function startExtract() {
    if (!findZhColumn(state.extractColumns)) {
      return toast('请先上传包含中文（简体）列的文件', true);
    }
    try {
      const data = await api('/api/extract/start', { method: 'POST', body: {} });
      $('#extractProgress').hidden = false;
      $('#btnExtractStart').disabled = true;
      pollTask(data.task_id,
        (t) => setProgress('#extractBar', '#extractPhase', t),
        (t) => {
          $('#btnExtractStart').disabled = false;
          $('#extractProgress').hidden = true;
          if (t.status === 'error') { return; }
          state.candidates = (t.result && t.result.candidates) || [];
          renderCandidates(state.candidates);
          $('#extractResultCard').hidden = !state.candidates.length;
          toast(t.result.message || '提取完成');
        });
    } catch (e) { toast(e.message, true); }
  }

  function renderCandidates(cands) {
    const body = $('#termsBody');
    body.innerHTML = cands.map((c, i) => {
      const sources = (c.sources || []).map((s) => esc(s)).join('；');
      return '<tr><td><input type="checkbox" class="term-check" data-i="' + i + '" checked></td>' +
        '<td>' + esc(c.term) + '</td><td>' + c.count + '</td><td>' + sources + '</td></tr>';
    }).join('');
    $('#extractCount').textContent = cands.length + ' 条候选';
    $('#termsCheckAll').checked = true;
  }

  async function confirmTerms() {
    const terms = [];
    $$('#termsBody .term-check').forEach((cb) => {
      if (cb.checked) terms.push(state.candidates[+cb.dataset.i].term);
    });
    if (!terms.length) return toast('请至少选择一条术语', true);
    try {
      const data = await api('/api/terms/confirm', { method: 'POST', body: { terms } });
      toast(data.message);
      // 从候选列表移除已确认项
      const checked = new Set(terms);
      state.candidates = state.candidates.filter((c) => !checked.has(c.term));
      renderCandidates(state.candidates);
      $('#extractResultCard').hidden = !state.candidates.length;
      refreshGlossary();
    } catch (e) { toast(e.message, true); }
  }

  async function glossaryDownload() {
    window.location.href = '/api/glossary/download';
  }

  function glossaryUploadPick() {
    $('#glossaryFile').click();
  }

  async function glossaryOverwriteConfirm() {
    const file = $('#glossaryFile').files[0];
    if (!file) return toast('请选择术语库文件', true);
    $('#goFileName').textContent = file.name;
    openModal('modalGlossaryOverwrite');
    $('#glossaryDiff').innerHTML = '<span class="muted">正在比对...</span>';
    const fd = new FormData();
    fd.append('file', file);
    try {
      const diff = await api('/api/glossary/diff', { method: 'POST', body: fd, isForm: true });
      renderGlossaryDiff(diff);
    } catch (e) {
      $('#glossaryDiff').innerHTML = '<span class="warn">⚠️ ' + esc(e.message) + '</span>';
    }
  }

  function renderGlossaryDiff(d) {
    const parts = [];
    parts.push('<div class="diff-line">当前共 <b>' + d.old_total + '</b> 条 → 新文件共 <b>' + d.new_total + '</b> 条</div>');
    parts.push('<div class="diff-line diff-add">➕ 新增 ' + d.added_count + ' 条</div>');
    if (d.added.length) {
      parts.push('<div class="diff-line">' + d.added.slice(0, 10).map((t) => esc(t)).join('、') +
        (d.added.length > 10 ? ' 等' : '') + '</div>');
    }
    parts.push('<div class="diff-line diff-del">➖ 删除 ' + d.deleted_count + ' 条</div>');
    if (d.deleted.length) {
      parts.push('<div class="diff-line">' + d.deleted.slice(0, 10).map((t) => esc(t)).join('、') +
        (d.deleted.length > 10 ? ' 等' : '') + '</div>');
    }
    parts.push('<div class="diff-line diff-mod">✏️ 修改 ' + d.modified_count + ' 条</div>');
    d.modified.slice(0, 8).forEach((m) => {
      const ch = m.changes.map((c) => esc(c.lang) + ': 「' + esc(c.old) + '」→「' + esc(c.new) + '」').join('；');
      parts.push('<div class="diff-line">' + esc(m.term) + ' — ' + ch + '</div>');
    });
    if (d.modified_count > 8) parts.push('<div class="diff-line">... 其余 ' + (d.modified_count - 8) + ' 条略</div>');
    parts.push('<div class="diff-line">保持不变 ' + d.unchanged_count + ' 条</div>');
    $('#glossaryDiff').innerHTML = parts.join('');
  }

  async function doGlossaryOverwrite() {
    const file = $('#glossaryFile').files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const data = await api('/api/glossary/upload', { method: 'POST', body: fd, isForm: true });
      closeModal('modalGlossaryOverwrite');
      $('#glossaryFile').value = '';
      toast(data.message);
      refreshGlossary();
    } catch (e) { toast(e.message, true); }
  }

  // ── Translate tab ─────────────────────────────────────────────────────

  async function refreshTranslateTab() {
    const st = await api('/api/translate/status');
    $('#translateUploadInfo').textContent = st.has_file ? '已上传: ' + st.filename : '';
    renderTargetRows(st.languages, st.columns);

    const banner = $('#termBanner');
    if (st.untranslated_count > 0) {
      $('#termBannerCount').textContent = st.untranslated_count;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
    $('#targetConfig').hidden = !st.has_file;
  }

  function renderTargetRows(languages, columns) {
    state.translateTargets = languages.map((l, i) => ({
      lang: l.name, projectSourceLang: l.source_lang,
      sourceCol: l.source_col || '', sourceColFound: !!l.source_col_found,
      enabled: i === 0 && !!l.source_col_found, limitCol: '',
    }));
    const body = $('#targetRows');
    body.innerHTML = languages.map((l, i) => {
      const srcTxt = l.source_col_found
        ? '<span class="ok">✓ ' + esc(l.source_col) + '</span>'
        : '<span class="warn">⚠️ 缺少源列</span>';
      const limits = '<option value="">-- 无 --</option>' + columns.map((c) =>
        '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('');
      return '<tr>' +
        '<td><input type="checkbox" class="tt-enabled" data-i="' + i + '"' +
          (state.translateTargets[i].enabled ? ' checked' : '') +
          (l.source_col_found ? '' : ' disabled') + '></td>' +
        '<td>' + esc(l.name) + '<div class="muted">源:' + (l.source_lang === 'zh' ? '中文' : 'English') + '</div></td>' +
        '<td>' + srcTxt + '</td>' +
        '<td><select class="select tt-limit" data-i="' + i + '">' + limits + '</select></td>' +
        '</tr>';
    }).join('');
    const warn = $('#translateColumnWarn');
    const missingLangs = languages.filter((l) => !l.source_col_found).map((l) => l.name);
    if (missingLangs.length) {
      warn.textContent = '⚠️ 以下语种缺少对应的源语言列，无法翻译：' + missingLangs.join('、') +
        '（中文源语种需「中文(简体)」列，英文源语种需「English」列）';
      warn.hidden = false;
    } else {
      warn.hidden = true;
    }
  }

  async function uploadTranslate() {
    const file = $('#translateFile').files[0];
    if (!file) return toast('请选择 Excel 文件', true);
    const fd = new FormData();
    fd.append('file', file); fd.append('tab', 'translate');
    try {
      const data = await api('/api/upload', { method: 'POST', body: fd, isForm: true });
      $('#translateUploadInfo').textContent = data.message;
      $('#targetConfig').hidden = false;
      $('#translateResultCard').hidden = true;
      await refreshTranslateTab();
      toast('上传成功');
    } catch (e) { toast(e.message, true); }
  }

  function syncTranslateTargets() {
    const rows = $$('#targetRows tr');
    state.translateTargets.forEach((t, i) => {
      const tr = rows[i];
      if (!tr) return;
      const cb = tr.querySelector('.tt-enabled');
      t.enabled = cb && cb.checked;
      t.limitCol = tr.querySelector('.tt-limit').value;
    });
  }

  async function startTranslate() {
    syncTranslateTargets();
    const targets = state.translateTargets
      .filter((t) => t.enabled && t.sourceColFound)
      .map((t) => ({ lang: t.lang, limit_col: t.limitCol }));
    if (!targets.length) return toast('请至少勾选一个可翻译的目标语种（需已识别源列）', true);
    try {
      const data = await api('/api/translate/start', {
        method: 'POST',
        body: { targets },
      });
      state.lastTranslateTask = data.task_id;
      $('#translateProgress').hidden = false;
      $('#btnTranslateStart').disabled = true;
      pollTask(data.task_id,
        (t) => setProgress('#translateBar', '#translatePhase', t),
        (t) => {
          $('#btnTranslateStart').disabled = false;
          $('#translateProgress').hidden = true;
          if (t.status === 'error') { return; }
          renderTranslateResult(t.result);
          toast('翻译完成');
        });
    } catch (e) { toast(e.message, true); }
  }

  function renderTranslateResult(result) {
    $('#translateResultCard').hidden = false;
    $('#translateResultInfo').textContent = result.message;
    const langs = result.languages || [];
    const head = $('#translateResultHead');
    head.innerHTML = '<tr><th>行</th><th>源文本</th>' +
      langs.map((l) => '<th>' + esc(l) + '</th>').join('') + '</tr>';
    const body = $('#translateResultBody');
    body.innerHTML = result.preview.map((row) =>
      '<tr><td>' + row.idx + '</td><td>' + esc(row.source) + '</td>' +
      langs.map((l) => '<td>' + esc((row.translations || {})[l]) + '</td>').join('') + '</tr>').join('');
  }

  function translateDownload() {
    if (state.lastTranslateTask) window.location.href = '/api/translate/download/' + state.lastTranslateTask;
  }

  async function goCheck() {
    if (!state.lastTranslateTask) return toast('请先完成翻译', true);
    try {
      await api('/api/check/use-result', { method: 'POST', body: { task_id: state.lastTranslateTask } });
      switchTab('check');
      toast('已载入翻译结果，可在校验页进行校验');
    } catch (e) { toast(e.message, true); }
  }

  // ── Term translation modal ────────────────────────────────────────────

  function openTermModal() {
    openModal('modalTermTranslate');
    $('#termTableWrap').hidden = true;
    $('#btnTermConfirmAll').hidden = true;
    $('#termModalInfo').textContent = '点击按钮调用大模型为未翻译的术语生成译文，生成后可手动修改并批量保存。';
  }

  async function runTermTranslate() {
    try {
      const data = await api('/api/translate/terms/start', { method: 'POST', body: {} });
      $('#termProgress').hidden = false;
      $('#btnTermTranslateRun').disabled = true;
      pollTask(data.task_id,
        (t) => setProgress('#termBar', '#termPhase', t),
        (t) => {
          $('#btnTermTranslateRun').disabled = false;
          $('#termProgress').hidden = true;
          if (t.status === 'error') { return; }
          renderTermTable((t.result && t.result.rows) || []);
          toast('术语翻译完成，请确认');
        });
    } catch (e) { toast(e.message, true); }
  }

  function renderTermTable(rows) {
    if (!rows.length) {
      $('#termTableWrap').hidden = true;
      $('#btnTermConfirmAll').hidden = true;
      $('#termModalInfo').textContent = '没有需要翻译的术语。';
      return;
    }
    const byId = {};
    rows.forEach((r) => {
      if (!byId[r.id]) byId[r.id] = { id: r.id, source: r.source, cells: {} };
      byId[r.id].cells[r.lang] = r.translation;
    });
    const langs = [...new Set(rows.map((r) => r.lang))];
    $('#termTableHead').innerHTML = '<tr><th>源术语</th>' +
      langs.map((l) => '<th>' + esc(l) + '</th>').join('') + '</tr>';
    $('#termTableBody').innerHTML = Object.values(byId).map((item) =>
      '<tr><td>' + esc(item.source) + '</td>' +
      langs.map((l) => '<td><input class="input term-input" data-id="' + item.id +
        '" data-lang="' + esc(l) + '" value="' + esc(item.cells[l] || '') + '"></td>').join('') +
      '</tr>').join('');
    $('#termTableWrap').hidden = false;
    $('#btnTermConfirmAll').hidden = false;
  }

  async function confirmTermTranslations() {
    const rows = [];
    $$('.term-input').forEach((inp) => {
      rows.push({ id: inp.dataset.id, lang: inp.dataset.lang, text: inp.value });
    });
    try {
      const data = await api('/api/translate/terms/confirm', { method: 'POST', body: { rows } });
      closeModal('modalTermTranslate');
      toast(data.message);
      refreshTranslateTab();
      refreshGlossary();
    } catch (e) { toast(e.message, true); }
  }

  // ── Check tab ─────────────────────────────────────────────────────────

  async function refreshCheckTab() {
    try {
      const data = await api('/api/check/preview');
      $('#checkUploadInfo').textContent = '已载入文件: ' + data.filename;
      renderCheckLangs(data);
      $('#checkConfig').hidden = false;
    } catch (e) {
      $('#checkConfig').hidden = true;
      $('#checkUploadInfo').textContent = '';
    }
  }

  function renderCheckLangs(data) {
    state.checkLangs = (data.languages || []).map((l, i) => ({
      name: l.name, source_lang: l.source_lang,
      source_col: l.source_col || '', target_col: l.target_col || '',
      missing: l.missing || [], ok: !!l.ok,
      enabled: !!l.ok && i === 0, limitCol: '',
    }));
    const body = $('#checkPairRows');
    body.innerHTML = state.checkLangs.map((l, i) => {
      const cols = l.ok
        ? '<span class="ok">源:' + esc(l.source_col) + ' → 目标:' + esc(l.target_col) + '</span>'
        : '<span class="warn">⚠️ 缺少 ' + esc(l.missing.join('、')) + '</span>';
      const limits = '<option value="">-- 无 --</option>' + (data.columns || []).map((c) =>
        '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('');
      return '<tr>' +
        '<td><input type="checkbox" class="cp-enabled" data-i="' + i + '"' +
          (l.enabled ? ' checked' : '') + (l.ok ? '' : ' disabled') + '></td>' +
        '<td>' + esc(l.name) + '<div class="muted">源:' + (l.source_lang === 'zh' ? '中文' : 'English') + '</div></td>' +
        '<td>' + cols + '</td>' +
        '<td><select class="select cp-limit" data-i="' + i + '">' + limits + '</select></td>' +
        '</tr>';
    }).join('');
    const warn = $('#checkColumnWarn');
    const missingLangs = (data.languages || []).filter((l) => !l.ok)
      .map((l) => l.name + '（缺少' + l.missing.join('、') + '）');
    if (missingLangs.length) {
      warn.textContent = '⚠️ 以下语种无法校验（列名缺失）：' + missingLangs.join('；') +
        '。请确认上传文件的列名与项目配置一致';
      warn.hidden = false;
    } else {
      warn.hidden = true;
    }
  }

  function syncCheckLangs() {
    $$('#checkPairRows tr').forEach((tr, i) => {
      const l = state.checkLangs[i];
      if (!l) return;
      const cb = tr.querySelector('.cp-enabled');
      l.enabled = cb && cb.checked;
      l.limitCol = tr.querySelector('.cp-limit').value;
    });
  }

  async function uploadCheck() {
    const file = $('#checkFile').files[0];
    if (!file) return toast('请选择 Excel 文件', true);
    const fd = new FormData();
    fd.append('file', file); fd.append('tab', 'check');
    try {
      const data = await api('/api/upload', { method: 'POST', body: fd, isForm: true });
      $('#checkUploadInfo').textContent = data.message;
      await refreshCheckTab();
      toast('上传成功');
    } catch (e) { toast(e.message, true); }
  }

  async function startCheck() {
    syncCheckLangs();
    const languages = state.checkLangs.filter((l) => l.enabled).map((l) => l.name);
    if (!languages.length) return toast('请至少勾选一个可校验的语种', true);
    const length_limits = state.checkLangs
      .filter((l) => l.enabled && l.limitCol)
      .map((l) => ({ column: l.target_col, limit_column: l.limitCol }));
    const enabled_checks = $$('#checkToggles input:checked').map((cb) => cb.dataset.check);
    try {
      const data = await api('/api/check/start', {
        method: 'POST',
        body: { languages, length_limits, enabled_checks },
      });
      $('#checkProgress').hidden = false;
      $('#btnCheckStart').disabled = true;
      pollTask(data.task_id,
        (t) => setProgress('#checkBar', '#checkPhase', t),
        (t) => {
          $('#btnCheckStart').disabled = false;
          $('#checkProgress').hidden = true;
          if (t.status === 'error') { return; }
          renderCheckResult(t.result);
        });
    } catch (e) { toast(e.message, true); }
  }

  function renderCheckResult(result) {
    $('#checkResultCard').hidden = false;
    state.lastCheckResultsId = result.results_id;
    const stats = (result.pair_stats || []).map((p) =>
      esc(p.target_lang) + ': ' + p.total + ' (' + p.errors + '误/' + p.warnings + '警)').join(' · ');
    $('#checkSummary').innerHTML =
      '<p class="muted">' + esc(result.message) + '</p>' +
      '<div class="muted">' + stats + '</div>';
    const body = $('#checkBody');
    if (!result.results.length) {
      body.innerHTML = '<tr><td colspan="8" class="muted">未发现问题 🎉</td></tr>';
      return;
    }
    body.innerHTML = result.results.map((r) =>
      '<tr><td>' + r.row + '</td><td>' + esc(r.target_language) + '</td>' +
      '<td>' + esc(r.check_label) + '</td>' +
      '<td class="sev-' + r.severity + '">' + esc(r.severity) + '</td>' +
      '<td>' + esc(r.issue) + '</td><td class="muted">' + esc(r.details) + '</td>' +
      '<td>' + esc(r.source_text) + '</td><td>' + esc(r.target_text) + '</td></tr>').join('');
  }

  function checkDownload() {
    if (state.lastCheckResultsId) window.location.href = '/api/check/download/' + state.lastCheckResultsId;
  }

  // ── Settings ──────────────────────────────────────────────────────────

  async function openSettings() {
    // 先取数据并回填，再显示弹窗，避免用户输入被异步回填覆盖
    const data = await api('/api/settings/llm');
    $('#llmBaseUrl').value = data.base_url;
    $('#llmModel').value = data.model;
    $('#llmApiKey').value = '';
    $('#llmTestInfo').textContent = data.has_key ? '已配置 API Key（留空保存将保持不变）' : '未配置 API Key';
    if (state.user.is_admin) {
      await loadAdminData();
      $('#settingsAdminTab').hidden = false;
    } else {
      $('#settingsAdminTab').hidden = true;
    }
    openModal('modalSettings');
  }

  async function saveLlm() {
    const base_url = $('#llmBaseUrl').value.trim();
    const model = $('#llmModel').value.trim();
    const api_key = $('#llmApiKey').value.trim();
    try {
      const data = await api('/api/settings/llm', {
        method: 'POST', body: { base_url, model, api_key },
      });
      toast(data.message);
      closeModal('modalSettings');
      await refreshMe();
    } catch (e) { toast(e.message, true); }
  }

  async function testLlm() {
    const base_url = $('#llmBaseUrl').value.trim();
    const model = $('#llmModel').value.trim();
    const api_key = $('#llmApiKey').value.trim();
    $('#llmTestInfo').textContent = '测试中...';
    try {
      const data = await api('/api/settings/llm/test', {
        method: 'POST', body: { base_url, model, api_key },
      });
      $('#llmTestInfo').textContent = (data.ok ? '✅ ' : '❌ ') + data.message;
      $('#llmTestInfo').style.color = data.ok ? '#059669' : '#dc2626';
    } catch (e) { $('#llmTestInfo').textContent = '❌ ' + e.message; }
  }

  async function changePassword() {
    const old_password = $('#pwOld').value;
    const new_password = $('#pwNew').value;
    const new2 = $('#pwNew2').value;
    if (!old_password || !new_password) return toast('请填写当前密码和新密码', true);
    if (new_password !== new2) return toast('两次输入的新密码不一致', true);
    try {
      const data = await api('/api/password', {
        method: 'POST', body: { old_password, new_password },
      });
      toast(data.message);
      $('#pwOld').value = $('#pwNew').value = $('#pwNew2').value = '';
    } catch (e) { toast(e.message, true); }
  }

  // ── Admin ─────────────────────────────────────────────────────────────

  async function loadAdminData() {
    const [uData, pData] = await Promise.all([
      api('/api/admin/users'),
      api('/api/admin/projects'),
    ]);
    renderAdminUsers(uData.users, pData.projects);
    renderAdminProjects(pData.projects);
    renderProjectCheckboxes('#nuProjects', pData.projects, []);
  }

  function renderAdminUsers(users, projects) {
    const body = $('#adminUsersBody');
    body.innerHTML = users.map((u) => {
      const boxes = projects.map((p) =>
        '<label class="chk" style="margin-right:8px"><input type="checkbox" class="user-proj" data-uid="' + u.id +
        '" value="' + p.id + '"' + (u.project_ids.includes(p.id) ? ' checked' : '') + '>' +
        esc(p.name) + '</label>').join('');
      return '<tr><td>' + u.id + '</td><td>' + esc(u.username) + '</td>' +
        '<td>' + (u.is_admin ? '✅' : '') + '</td>' +
        '<td>' + (u.is_admin ? '<span class="muted">全部</span>' : boxes) + '</td>' +
        '<td>' + (u.is_admin ? '' :
          '<button class="btn btn-outline btn-sm" data-action="save-proj" data-uid="' + u.id + '">保存权限</button> ' +
          '<button class="btn btn-outline btn-sm" data-action="del-user" data-uid="' + u.id + '">删除</button>') +
        '</td></tr>';
    }).join('');
  }

  function renderAdminProjects(projects) {
    const body = $('#adminProjectsBody');
    body.innerHTML = projects.map((p) => {
      const langs = (p.languages || []).map((l) =>
        esc(l.name) + '(' + (l.source_lang === 'zh' ? '中' : '英') + ')').join(', ');
      return '<tr><td>' + p.id + '</td><td>' + esc(p.name) + '</td>' +
        '<td>' + esc(p.source_col_name) + '（' + (p.source_lang === 'zh' ? '中文' : 'English') + '）</td>' +
        '<td>' + langs + '</td>' +
        '<td><button class="btn btn-outline btn-sm" data-action="edit-project" data-pid="' + p.id + '">编辑</button> ' +
        '<button class="btn btn-outline btn-sm" data-action="del-project" data-pid="' + p.id + '">删除</button></td></tr>';
    }).join('');
  }

  function renderProjectCheckboxes(containerSel, projects, selected) {
    const c = $(containerSel);
    c.innerHTML = projects.map((p) =>
      '<label class="chk"><input type="checkbox" value="' + p.id + '"' +
      (selected.includes(p.id) ? ' checked' : '') + '>' + esc(p.name) + '</label>').join('');
  }

  async function saveUserProjects(uid) {
    const project_ids = $$('#adminUsersBody .user-proj[data-uid="' + uid + '"]:checked')
      .map((cb) => +cb.value);
    try {
      const data = await api('/api/admin/users/' + uid + '/projects', {
        method: 'POST', body: { project_ids },
      });
      toast(data.message + '（可访问项目：' + (project_ids.join('、') || '无') + '）');
      await loadAdminData();  // 刷新管理表，让勾选状态可视化
    } catch (e) { toast(e.message, true); }
  }

  async function deleteUser(uid) {
    if (!confirm('确认删除该用户？')) return;
    try {
      const data = await api('/api/admin/users/' + uid, { method: 'DELETE' });
      toast(data.message);
      await loadAdminData();
    } catch (e) { toast(e.message, true); }
  }

  function openNewUser() {
    $('#nuUsername').value = ''; $('#nuPassword').value = ''; $('#nuIsAdmin').checked = false;
    openModal('modalNewUser');
    // 使用全部可见项目（管理员=全部项目；普通用户=已分配项目），
    // 不能依赖 currentProject —— 管理员建用户时可能尚未选择项目。
    renderProjectCheckboxes('#nuProjects', state.projects || [], []);
  }

  async function createUser() {
    const username = $('#nuUsername').value.trim();
    const password = $('#nuPassword').value;
    const is_admin = $('#nuIsAdmin').checked;
    const project_ids = $$('#nuProjects input:checked').map((cb) => +cb.value);
    if (!username || !password) return toast('用户名和密码不能为空', true);
    try {
      const data = await api('/api/admin/users', {
        method: 'POST', body: { username, password, is_admin, project_ids },
      });
      closeModal('modalNewUser');
      toast(data.message);
      await loadAdminData();
    } catch (e) { toast(e.message, true); }
  }

  // 新建项目（语种全部用下拉选择，统一规范名）
  const DEFAULT_LANGS = ['中文(简体)', 'English', '日本語', 'Deutsch', 'Français'];

  function addProjectLangRow(name, srcLang) {
    const div = document.createElement('div');
    div.className = 'row np-lang-row';
    div.style.marginBottom = '8px';
    const names = state.langNames.length ? state.langNames : DEFAULT_LANGS;
    const opts = names.map((n) =>
      '<option value="' + esc(n) + '"' + (n === (name || '') ? ' selected' : '') + '>' + esc(n) + '</option>').join('');
    div.innerHTML = '<div class="col"><select class="select np-lang-name">' + opts + '</select></div>' +
      '<div class="col-auto"><select class="select np-lang-src">' +
      '<option value="zh"' + (srcLang !== 'en' ? ' selected' : '') + '>源: 中文</option>' +
      '<option value="en"' + (srcLang === 'en' ? ' selected' : '') + '>源: English</option>' +
      '</select></div>' +
      '<div class="col-auto"><button class="btn btn-outline btn-sm np-lang-del">×</button></div>';
    $('#npLangRows').appendChild(div);
    div.querySelector('.np-lang-del').addEventListener('click', () => div.remove());
  }

  function openNewProject() {
    state.editingProjectId = null;
    $('#modalNewProjectTitle').textContent = '新建项目';
    $('#npName').value = '';
    $('#npDesc').value = '';
    $('#npLangRows').innerHTML = '';
    addProjectLangRow('English', 'zh');
    openModal('modalNewProject');
  }

  function openEditProject(pid) {
    const p = (state.projects || []).find((x) => x.id === pid);
    if (!p) return;
    state.editingProjectId = pid;
    $('#modalNewProjectTitle').textContent = '编辑项目';
    $('#npName').value = p.name || '';
    $('#npDesc').value = p.description || '';
    $('#npLangRows').innerHTML = '';
    (p.languages && p.languages.length ? p.languages : []).forEach((l) => {
      addProjectLangRow(l.name, l.source_lang);
    });
    openModal('modalNewProject');
  }

  async function createProject() {
    const name = $('#npName').value.trim();
    const description = $('#npDesc').value.trim();
    const languages = $$('.np-lang-row').map((r) => ({
      name: r.querySelector('.np-lang-name').value.trim(),
      source_lang: r.querySelector('.np-lang-src').value,
    })).filter((l) => l.name);
    if (!languages.length) return toast('请至少添加一个目标语种', true);
    try {
      const data = state.editingProjectId
        ? await api('/api/admin/projects/' + state.editingProjectId, {
            method: 'PUT', body: { name, description, languages },
          })
        : await api('/api/admin/projects', {
            method: 'POST', body: { name, description, languages },
          });
      closeModal('modalNewProject');
      state.editingProjectId = null;
      toast(data.message);
      await refreshMe();
      await loadAdminData();
    } catch (e) { toast(e.message, true); }
  }

  async function deleteProject(pid) {
    if (!confirm('确认删除该项目？项目术语库和翻译结果将一并删除！')) return;
    try {
      const data = await api('/api/admin/projects/' + pid, { method: 'DELETE' });
      toast(data.message);
      await refreshMe();
      await loadAdminData();
    } catch (e) { toast(e.message, true); }
  }

  // ── Tabs ──────────────────────────────────────────────────────────────

  function switchTab(name) {
    $$('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + name));
    if (name === 'translate') refreshTranslateTab();
    if (name === 'check') refreshCheckTab();
  }

  function bindTabs() {
    $$('.tab').forEach((btn) => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
  }

  async function refreshAll() {
    if (!state.currentProject) return;
    refreshGlossary();
    refreshTranslateTab();
    refreshCheckTab();
  }

  // ── Event binding ─────────────────────────────────────────────────────

  function bindGlobal() {
    $('#btnLogout').addEventListener('click', async () => {
      await api('/api/logout', { method: 'POST' });
      location.href = '/login';
    });
    $('#btnSettings').addEventListener('click', openSettings);
    $('#projectSelect').addEventListener('change', (e) => {
      if (e.target.value) selectProjectById(+e.target.value);
    });

    // Settings tabs
    $$('#settingsTabs .tab-inline').forEach((btn) => {
      btn.addEventListener('click', () => {
        $$('#settingsTabs .tab-inline').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        $('#stab-llm').hidden = btn.dataset.stab !== 'llm';
        $('#stab-account').hidden = btn.dataset.stab !== 'account';
        $('#stab-admin').hidden = btn.dataset.stab !== 'admin';
      });
    });
    // 管理页子页签：用户 / 项目
    $$('#adminTabs .tab-inline').forEach((btn) => {
      btn.addEventListener('click', () => {
        $$('#adminTabs .tab-inline').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        $('#adminUsersBlock').hidden = btn.dataset.atab !== 'users';
        $('#adminProjectsBlock').hidden = btn.dataset.atab !== 'projects';
      });
    });
    $('#btnSaveLlm').addEventListener('click', saveLlm);
    $('#btnTestLlm').addEventListener('click', testLlm);
    $('#btnChangePassword').addEventListener('click', changePassword);

    // Extract
    $('#btnExtractUpload').addEventListener('click', uploadExtract);
    $('#btnExtractStart').addEventListener('click', startExtract);
    $('#btnTermsConfirm').addEventListener('click', confirmTerms);
    $('#btnTermsConfirmAll').addEventListener('click', () => { $('#termsCheckAll').checked = true; $$('#termsBody .term-check').forEach((c) => c.checked = true); });
    $('#btnTermsDeselectAll').addEventListener('click', () => { $('#termsCheckAll').checked = false; $$('#termsBody .term-check').forEach((c) => c.checked = false); });
    $('#termsCheckAll').addEventListener('change', (e) => $$('#termsBody .term-check').forEach((c) => c.checked = e.target.checked));

    // Glossary
    $('#btnGlossaryDownload').addEventListener('click', glossaryDownload);
    $('#btnGlossaryUpload').addEventListener('click', glossaryUploadPick);
    $('#glossaryFile').addEventListener('change', glossaryOverwriteConfirm);
    $('#btnGoConfirm').addEventListener('click', doGlossaryOverwrite);

    // Translate
    $('#btnTranslateUpload').addEventListener('click', uploadTranslate);
    $('#btnTranslateStart').addEventListener('click', startTranslate);
    $('#btnTranslateDownload').addEventListener('click', translateDownload);
    $('#btnGoCheck').addEventListener('click', goCheck);
    $('#btnOpenTermModal').addEventListener('click', openTermModal);
    $('#btnTermTranslateRun').addEventListener('click', runTermTranslate);
    $('#btnTermConfirmAll').addEventListener('click', confirmTermTranslations);

    // Check
    $('#btnCheckUpload').addEventListener('click', uploadCheck);
    $('#btnCheckStart').addEventListener('click', startCheck);
    $('#btnCheckDownload').addEventListener('click', checkDownload);

    // Admin
    $('#btnAddUser').addEventListener('click', openNewUser);
    $('#btnNuSave').addEventListener('click', createUser);
    $('#btnAddProject').addEventListener('click', openNewProject);
    $('#btnNpAddLang').addEventListener('click', () => addProjectLangRow('', 'zh'));
    $('#btnNpSave').addEventListener('click', createProject);

    // Delegate admin table actions
    $('#adminUsersBody').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      if (btn.dataset.action === 'save-proj') saveUserProjects(btn.dataset.uid);
      if (btn.dataset.action === 'del-user') deleteUser(btn.dataset.uid);
    });
    $('#adminProjectsBody').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      if (btn.dataset.action === 'edit-project') openEditProject(+btn.dataset.pid);
      if (btn.dataset.action === 'del-project') deleteProject(btn.dataset.pid);
    });

    // Modal close buttons
    $$('.modal-close').forEach((btn) => {
      btn.addEventListener('click', () => closeModal(btn.dataset.close));
    });

    // Toast box
    const box = document.createElement('div');
    box.id = 'toastBox';
    box.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:999;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(box);
  }

  // ── Init ──────────────────────────────────────────────────────────────

  async function init() {
    try {
      bindGlobal();
      bindTabs();
      const meta = await api('/api/meta');
      state.langNames = meta.lang_names || [];
      await refreshMe();
      if (state.currentProject) await refreshAll();
    } catch (e) {
      if (e.message !== '未登录') console.error(e);
    }
  }

  // ── Login page ────────────────────────────────────────────────────────

  function initLogin() {
    $('#loginForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const err = $('#loginError');
      err.hidden = true;
      try {
        const data = await api('/api/login', {
          method: 'POST',
          body: { username: $('#username').value, password: $('#password').value },
        });
        location.href = '/app';
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
      }
    });
  }

  window.App = { init, login: { init: initLogin } };
})();
