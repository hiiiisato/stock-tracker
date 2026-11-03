(function () {
  'use strict';
  const data = JSON.parse(document.getElementById('wl-initial-data').textContent);
  const $ = id => document.getElementById(id);
  const make = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text != null) el.textContent = text; return el; };
  const byCode = new Map(data.stocks.map(s => [s.code, s]));
  const listNames = new Map(data.lists.map(l => [l.id, l.name]));
  const params = new URLSearchParams(location.search);
  const state = { list: params.get('list') || 'all', code: '', query: params.get('q') || '', sort: params.get('sort') || 'added', period: '6M', interval: 'day', mas: new Set([25]), chartCache: new Map(), chartRevision: 0, settings: {}, add: { code: '', query: '', memo: '', ids: [], rows: [], active: -1, revision: 0 }, expanded: false };
  const detail = $('wl-detail'), dialog = $('wl-add-dialog'), addForm = $('wl-add-form');
  const fmt = (v, suffix = '', digits = null) => v == null ? '—' : Number(v).toLocaleString('ja-JP', { minimumFractionDigits: digits ?? 0, maximumFractionDigits: digits ?? 4 }) + suffix;
  const change = value => value == null ? '—' : (value >= 0 ? '+' : '') + Number(value).toFixed(2) + '%';
  const color = value => value == null ? 'wl-muted' : value >= 0 ? 'wl-positive' : 'wl-negative';
  const inList = (stock, list) => list === 'all' || (list === 'unclassified' ? !stock.lists.length : stock.lists.includes(Number(list)));
  function currentURL(code = state.code) { const p = new URLSearchParams({ list: state.list }); if (code) p.set('code', code); if (state.query) p.set('q', state.query); if (state.sort !== 'added') p.set('sort', state.sort); return '/watchlist?' + p; }
  function updateURL(push) { history[push ? 'pushState' : 'replaceState']({}, '', currentURL()); detail.querySelectorAll('input[name=next]').forEach(el => { el.value = currentURL(); }); }
  function renderPills() {
    const nav = document.querySelector('.wl-pills'); nav.replaceChildren();
    [{ id: 'all', name: 'すべて' }, ...data.lists, { id: 'unclassified', name: '未分類' }].forEach(list => {
      const button = make('button', 'wl-pill' + (String(list.id) === state.list ? ' active' : ''), list.name); button.type = 'button';
      button.setAttribute('aria-pressed', String(String(list.id) === state.list)); button.append(make('span', '', data.stocks.filter(s => inList(s, String(list.id))).length));
      button.onclick = () => { state.list = String(list.id); renderPills(); renderList(); updateURL(true); }; nav.append(button);
    });
  }
  function visibleStocks() {
    const q = state.query.normalize('NFKC').toLocaleLowerCase();
    return data.stocks.filter(s => inList(s, state.list) && [s.code, s.name, s.last_note].join(' ').normalize('NFKC').toLocaleLowerCase().includes(q)).sort((a, b) => {
      if (state.sort.startsWith('change')) { if (a.change_pct == null) return b.change_pct == null ? 0 : 1; if (b.change_pct == null) return -1; return state.sort === 'change_desc' ? b.change_pct - a.change_pct : a.change_pct - b.change_pct; }
      if (state.sort === 'notes') return b.note_count - a.note_count || a.code.localeCompare(b.code);
      if (state.sort === 'code') return a.code.localeCompare(b.code);
      return String(b[state.sort === 'updated' ? 'last_note_at' : 'added_at']).localeCompare(String(a[state.sort === 'updated' ? 'last_note_at' : 'added_at'])) || a.code.localeCompare(b.code);
    });
  }
  function renderList() {
    const rows = visibleStocks(), list = $('wl-stock-list'); list.replaceChildren(); $('wl-result-count').textContent = rows.length + '銘柄';
    if (!rows.some(s => s.code === state.code)) select(rows[0] ? rows[0].code : '');
    rows.forEach(stock => {
      const row = make('button', 'wl-stock-row' + (stock.code === state.code ? ' active' : '')); row.type = 'button'; row.dataset.code = stock.code; row.setAttribute('aria-pressed', String(stock.code === state.code));
      const top = make('div', 'wl-row-top'), name = make('div'), quote = make('div', 'wl-row-price');
      name.append(make('span', 'wl-row-name', (stock.name || stock.code).normalize('NFKC')), make('span', 'wl-row-code', stock.code));
      quote.append(make('b', '', fmt(stock.close, '円')), make('span', color(stock.change_pct), change(stock.change_pct))); top.append(name, quote);
      const meta = make('div', 'wl-row-meta'); meta.append(make('span', '', '記録 ' + stock.note_count + '件'));
      if (stock.alerts.some(a => a.event_id || a.status === 'split_review')) meta.append(make('span', 'wl-hit', '● 価格通知あり'));
      row.append(top, make('p', 'wl-row-note', stock.last_note || 'メモ・参考URLを追加できます'), meta);
      row.onclick = () => { select(stock.code); renderList(); updateURL(true); if (matchMedia('(max-width:768px)').matches) { detail.scrollIntoView({ block: 'start', behavior: 'smooth' }); detail.focus({ preventScroll: true }); } };
      list.append(row);
    });
    if (!rows.length) list.append(make('p', 'wl-empty', '該当する銘柄がありません'));
  }
  const notes = window.StockNotes.mount(document.querySelector('.sn-root'), (code, rows) => {
    const stock = byCode.get(code); if (!stock) return;
    stock.note_count = rows.length; stock.last_note = rows.length ? rows[0].title || rows[0].content : ''; stock.last_note_at = rows.length ? rows[0].created_at : '';
    renderList(); updateURL(false);
  });
  function checks(container, selected, onChange) {
    container.replaceChildren();
    if (!data.lists.length) { container.append(make('span', 'wl-muted', 'リストは「リストを管理」で作成できます')); return; }
    data.lists.forEach(list => { const label = make('label'), input = make('input'); input.type = 'checkbox'; input.name = 'list_ids'; input.value = list.id; input.checked = selected.includes(list.id); input.onchange = () => onChange(list.id, input.checked); label.append(input, document.createTextNode(list.name)); container.append(label); });
  }
  function renderSettings(stock) {
    const settings = state.settings[stock.code] || (state.settings[stock.code] = { ids: stock.lists.slice(), upper: '', lower: '' });
    ['upper', 'lower'].forEach(direction => { const a = stock.alerts.find(a => a.direction === direction); if (!(direction + 'Loaded' in settings)) { settings[direction] = a && ['active', 'split_review'].includes(a.status) ? a.target : ''; settings[direction + 'Loaded'] = true; } });
    detail.querySelectorAll('input[name=code]').forEach(el => { el.value = stock.code; });
    checks($('wl-memberships'), settings.ids, (id, checked) => { settings.ids = checked ? [...settings.ids, id] : settings.ids.filter(v => v !== id); });
    const form = $('wl-alert-form');
    ['upper', 'lower'].forEach(direction => {
      const a = stock.alerts.find(a => a.direction === direction); form.elements[direction].value = settings[direction]; form.elements[direction + '_status'].value = a ? a.status : '';
      form.elements[direction].placeholder = a && a.status === 'triggered' ? '到達済み・価格入力で再開' : direction === 'upper' ? '指定価格以上' : '指定価格以下';
      form.elements[direction].oninput = e => { settings[direction] = e.target.value; };
    });
    const active = stock.alerts.filter(a => a.status === 'active');
    $('wl-alert-summary').textContent = active.map(a => (a.direction === 'upper' ? '↑' : '↓') + fmt(a.target, '円')).join(' / ') || '未設定';
    $('wl-effective').textContent = active.map(a => (a.direction === 'upper' ? '上値' : '下値') + '：' + a.effective + 'から判定').join(' / ');
    const flags = $('wl-alert-flags'); flags.replaceChildren();
    stock.alerts.forEach(alert => {
      if (alert.status === 'split_review') flags.append(make('div', '', '株式分割後の価格通知を確認してください。価格を設定し直すまで通知を停止しています。'));
      if (alert.event_id) {
        const line = make('div', '', (alert.direction === 'upper' ? '上値' : '下値') + '到達 · ' + alert.event_date + ' · 終値 ' + fmt(alert.price, '円'));
        const ack = make('form'); ack.method = 'post'; ack.action = '/watchlist/alert/ack';
        const values = { code: stock.code, event_id: alert.event_id, csrf: (addForm.elements.csrf?.value || ''), next: currentURL() };
        Object.entries(values).forEach(([name, value]) => { const input = make('input'); input.type = 'hidden'; input.name = name; input.value = value; ack.append(input); });
        const button = make('button', 'btn-sm', '確認済み'); ack.append(button); line.append(ack); flags.append(line);
      }
    });
  }
  function select(code) {
    detail.hidden = !code; $('wl-no-selection').hidden = !!code;
    if (code === state.code) return;
    state.code = code;
    if (!code) { ++state.chartRevision; return; }
    const s = byCode.get(code); $('wl-detail-name').textContent = (s.name || code).normalize('NFKC'); $('wl-detail-code').textContent = code; $('wl-stock-link').href = '/stock/' + code;
    $('wl-price').textContent = fmt(s.close, '円'); $('wl-change').textContent = change(s.change_pct); $('wl-change').className = color(s.change_pct); $('wl-price-date').textContent = s.price_date ? s.price_date + ' 終値' : '価格データなし';
    $('wl-per').textContent = fmt(s.per, '倍', 1); $('wl-pbr').textContent = fmt(s.pbr, '倍', 2); $('wl-yield').textContent = fmt(s.div_yield, '%', 2);
    $('wl-detail-lists').replaceChildren(...(s.lists.length ? s.lists.map(id => make('span', 'wl-badge', listNames.get(id) || '')) : [make('span', 'wl-muted', '未分類')]));
    renderSettings(s); notes.select(code); loadChart(code);
  }
  detail.querySelectorAll('.wl-settings-grid form').forEach(form => {
    const feedback = make('p', 'wl-settings-feedback'); feedback.setAttribute('role', 'status'); form.append(feedback);
    form.addEventListener('submit', async event => {
      event.preventDefault(); const code = state.code, stock = byCode.get(code), settings = state.settings[code];
      const button = form.querySelector('button'); if (button.disabled) return;
      const memberships = form.action.endsWith('/memberships');
      const values = { code, csrf: addForm.elements.csrf?.value || '', next: currentURL() };
      if (!memberships) ['upper', 'lower'].forEach(direction => { values[direction] = settings[direction]; values[direction + '_status'] = stock.alerts.find(a => a.direction === direction)?.status || ''; });
      const ids = settings.ids.slice(), body = new URLSearchParams(values); if (memberships) ids.forEach(id => body.append('list_ids', id));
      button.disabled = true; feedback.textContent = '保存中…';
      try {
        const response = await fetch(form.action, { method: 'POST', headers: { Accept: 'application/json' }, body });
        if (response.redirected) throw new Error('編集にはパスコードでログインしてください。入力は保持しています。');
        const result = await response.json().catch(() => ({})); if (!response.ok) throw new Error(result.error || '保存できませんでした。もう一度お試しください。');
        if (memberships) { stock.lists = ids; renderPills(); if (state.code === code) $('wl-detail-lists').replaceChildren(...ids.map(id => make('span', 'wl-badge', listNames.get(id) || ''))); renderList(); }
        else {
          ['upper', 'lower'].forEach(direction => {
            if (!String(values[direction]).trim() && values[direction + '_status'] === 'triggered') return;
            stock.alerts = stock.alerts.filter(a => a.direction !== direction);
            if (String(values[direction]).trim()) stock.alerts.push({ direction, target: Number(String(values[direction]).replaceAll(',', '')), status: 'active', effective: result.effective });
          });
          if (state.code === code) renderSettings(stock);
        }
        feedback.textContent = '保存しました';
      } catch (error) { feedback.textContent = error.message; } finally { button.disabled = false; }
    });
  });
  function aggregate(prices) {
    if (state.interval === 'day') return prices;
    const result = [];
    prices.forEach(p => {
      const day = new Date(p.date + 'T00:00:00Z');
      if (state.interval === 'week') day.setUTCDate(day.getUTCDate() - (day.getUTCDay() + 6) % 7); else day.setUTCDate(1);
      const key = day.toISOString().slice(0, 10), old = result[result.length - 1];
      if (old && old.key === key) { old.high = Math.max(old.high, p.high); old.low = Math.min(old.low, p.low); old.close = p.close; old.volume += p.volume; }
      else result.push({ ...p, key });
    }); return result;
  }
  function drawChart() {
    const all = aggregate((state.chartCache.get(state.code) || []).filter(p => [p.open, p.high, p.low, p.close].every(v => v != null && Number.isFinite(v))));
    if (!window.Plotly) { $('wl-chart-status').textContent = 'チャートを読み込めませんでした。ページを再読み込みしてください。'; return; }
    if (!all.length) { Plotly.purge('wl-chart'); $('wl-chart-status').textContent = 'ローソク足データがありません'; return; }
    const from = new Date(all[all.length - 1].date + 'T00:00:00Z'); from.setUTCMonth(from.getUTCMonth() - ({ '1M': 1, '3M': 3, '6M': 6, '1Y': 12, '3Y': 36 }[state.period]));
    const cutoff = from.toISOString().slice(0, 10), prices = all.filter(p => p.date >= cutoff), dates = prices.map(p => p.date);
    const traces = [{ type: 'candlestick', name: '株価', x: dates, open: prices.map(p => p.open), high: prices.map(p => p.high), low: prices.map(p => p.low), close: prices.map(p => p.close), increasing: { line: { color: '#E84040' }, fillcolor: '#E84040' }, decreasing: { line: { color: '#3A9FE0' }, fillcolor: '#3A9FE0' } }, { type: 'bar', name: '出来高', x: dates, y: prices.map(p => p.volume), yaxis: 'y2', showlegend: false, marker: { color: prices.map(p => p.close >= p.open ? '#E8404077' : '#3A9FE077') }, hovertemplate: '%{x}<br>出来高 %{y:,.0f}株<extra></extra>' }];
    const colors = { 5: '#ffa657', 25: '#58a6ff', 75: '#a371f7' };
    state.mas.forEach(n => { const x = [], y = []; let sum = 0; all.forEach((p, i) => { sum += p.close; if (i >= n) sum -= all[i - n].close; if (i >= n - 1 && p.date >= cutoff) { x.push(p.date); y.push(sum / n); } }); traces.push({ type: 'scatter', mode: 'lines', name: 'MA' + n, x, y, line: { color: colors[n], width: 1.4 }, hovertemplate: '%{y:,.2f}円<extra>MA' + n + '</extra>' }); });
    $('wl-chart-status').textContent = prices[0].date + ' 〜 ' + prices[prices.length - 1].date + ' · ' + ({ day: '日足', week: '週足', month: '月足' }[state.interval]);
    Plotly.react('wl-chart', traces, { paper_bgcolor: '#161b22', plot_bgcolor: '#161b22', font: { color: '#c9d1d9', size: 11 }, height: $('wl-chart').clientHeight || 420, margin: { l: 8, r: 60, t: 20, b: 34 }, dragmode: 'zoom', xaxis: { type: 'category', rangeslider: { visible: false }, nticks: matchMedia('(max-width:768px)').matches ? 4 : 8, gridcolor: '#21262d', showspikes: true, spikemode: 'across' }, yaxis: { side: 'right', domain: [.25, 1], gridcolor: '#21262d', tickformat: ',.0f', automargin: true, showspikes: true }, yaxis2: { side: 'right', domain: [0, .18], tickformat: '~s', nticks: 3, fixedrange: true }, showlegend: true, legend: { orientation: 'h', y: 1.1, x: 0, font: { size: 11 } } }, { responsive: true, displaylogo: false, displayModeBar: 'hover', scrollZoom: false, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'] });
  }
  async function loadChart(code) {
    const revision = ++state.chartRevision;
    if (window.Plotly) Plotly.purge('wl-chart'); $('wl-chart-status').textContent = '株価を読み込み中…';
    try {
      if (!state.chartCache.has(code)) { const response = await fetch('/api/watchlist/chart/' + encodeURIComponent(code)); if (!response.ok) throw new Error('株価を取得できませんでした'); state.chartCache.set(code, await response.json()); }
      if (revision === state.chartRevision && code === state.code) drawChart();
    } catch (error) { if (revision !== state.chartRevision) return; $('wl-chart-status').replaceChildren(document.createTextNode(error.message + ' ')); const retry = make('button', 'btn-sm', '再試行'); retry.onclick = () => loadChart(code); $('wl-chart-status').append(retry); }
  }
  function chartButtons(id, field) { $(id).querySelectorAll('button').forEach(b => b.onclick = () => { state[field] = b.dataset[field]; $(id).querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b)); drawChart(); }); }
  chartButtons('wl-periods', 'period'); chartButtons('wl-intervals', 'interval');
  $('wl-mas').querySelectorAll('button').forEach(b => b.onclick = () => { const n = Number(b.dataset.ma); state.mas.has(n) ? state.mas.delete(n) : state.mas.add(n); b.classList.toggle('active', state.mas.has(n)); drawChart(); });
  function expandChart(expanded) { state.expanded = expanded; document.querySelector('.wl-chart-panel').classList.toggle('expanded', expanded); $('wl-expand-chart').textContent = expanded ? '閉じる' : '拡大'; $('wl-expand-chart').setAttribute('aria-expanded', String(expanded)); drawChart(); }
  $('wl-expand-chart').onclick = () => expandChart(!state.expanded);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && state.expanded) expandChart(false); });
  $('wl-filter').oninput = e => { state.query = e.target.value; renderList(); updateURL(false); };
  $('wl-sort').onchange = e => { state.sort = e.target.value; renderList(); updateURL(false); };
  let searchTimer;
  function renderCandidates() {
    const list = $('wl-search-results'); list.replaceChildren();
    state.add.rows.forEach((stock, i) => { const button = make('button', i === state.add.active ? 'active' : ''); button.type = 'button'; button.id = 'wl-candidate-' + i; button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(i === state.add.active)); button.append(make('b', '', stock.code), make('span', '', stock.name.normalize('NFKC'))); if (byCode.has(stock.code)) button.append(make('small', '', '保存済み')); button.onclick = () => chooseCandidate(stock); list.append(button); });
    $('wl-stock-search').setAttribute('aria-expanded', String(!!state.add.rows.length));
    if (state.add.active >= 0) $('wl-stock-search').setAttribute('aria-activedescendant', 'wl-candidate-' + state.add.active); else $('wl-stock-search').removeAttribute('aria-activedescendant');
  }
  function chooseCandidate(stock) {
    state.add.code = stock.code; state.add.query = stock.name + ' (' + stock.code + ')'; $('wl-stock-search').value = state.add.query;
    ++state.add.revision; clearTimeout(searchTimer); state.add.rows = []; state.add.active = -1; renderCandidates();
    $('wl-selected-stock').textContent = stock.name + ' (' + stock.code + ') を選択中'; $('wl-search-status').textContent = '';
    $('wl-add-submit').disabled = false; $('wl-add-submit').textContent = byCode.has(stock.code) ? '保存済みの銘柄を開く' : '選択した銘柄を追加';
  }
  $('wl-stock-search').oninput = e => {
    state.add.query = e.target.value; state.add.code = ''; state.add.rows = []; state.add.active = -1;
    $('wl-add-submit').disabled = true; $('wl-selected-stock').textContent = ''; renderCandidates(); clearTimeout(searchTimer);
    const q = state.add.query.trim(), revision = ++state.add.revision;
    $('wl-search-status').textContent = q ? '検索中…' : '候補を選択して追加します';
    if (!q) return;
    searchTimer = setTimeout(async () => {
      try { const response = await fetch('/api/search?q=' + encodeURIComponent(q)); if (!response.ok) throw new Error(); const rows = await response.json(); if (revision !== state.add.revision) return; state.add.rows = rows; renderCandidates(); $('wl-search-status').textContent = rows.length ? rows.length + '件の候補 · ↑↓で移動、Enterで選択' : '候補がありません。別の銘柄名・コードで検索してください。'; }
      catch (_) { if (revision === state.add.revision) $('wl-search-status').textContent = '検索できませんでした。もう一度入力してください。'; }
    }, 180);
  };
  $('wl-stock-search').onkeydown = e => {
    if (['ArrowDown', 'ArrowUp'].includes(e.key) && state.add.rows.length) { e.preventDefault(); state.add.active = (state.add.active + (e.key === 'ArrowDown' ? 1 : -1) + state.add.rows.length) % state.add.rows.length; renderCandidates(); $('wl-candidate-' + state.add.active).scrollIntoView({ block: 'nearest' }); }
    if (e.key === 'Enter') { e.preventDefault(); const stock = state.add.rows[state.add.active] || (state.add.rows.length === 1 ? state.add.rows[0] : null); if (stock) chooseCandidate(stock); }
  };
  $('wl-open-add').onclick = () => { state.add.ids = /^\d+$/.test(state.list) ? [Number(state.list)] : []; checks($('wl-add-lists'), state.add.ids, (id, checked) => { state.add.ids = checked ? [...state.add.ids, id] : state.add.ids.filter(v => v !== id); }); dialog.showModal(); $('wl-stock-search').focus(); };
  $('wl-close-add').onclick = () => dialog.close();
  $('wl-add-memo').oninput = e => { state.add.memo = e.target.value; };
  addForm.onsubmit = async e => {
    e.preventDefault(); if (!state.add.code) return;
    if (byCode.has(state.add.code)) { state.list = 'all'; state.query = ''; $('wl-filter').value = ''; select(state.add.code); renderPills(); renderList(); updateURL(true); dialog.close(); return; }
    $('wl-add-submit').disabled = true; $('wl-add-error').textContent = '';
    try {
      const body = new URLSearchParams({ code: state.add.code, memo: state.add.memo, csrf: (addForm.elements.csrf?.value || ''), memberships_present: '1' }); state.add.ids.forEach(id => body.append('list_ids', id));
      const response = await fetch('/watchlist/add', { method: 'POST', headers: { Accept: 'application/json' }, body });
      if (response.redirected) throw new Error('追加するにはパスコードでログインしてください。入力は保持しています。');
      const result = await response.json().catch(() => ({})); if (!response.ok) throw new Error(result.error || '保存できませんでした。もう一度お試しください。');
      location.href = currentURL(state.add.code);
    } catch (error) { $('wl-add-error').textContent = error.message; $('wl-add-submit').disabled = false; }
  };
  function restoreControls() { $('wl-filter').value = state.query; $('wl-sort').value = state.sort; $('wl-stock-search').value = state.add.query; $('wl-add-memo').value = state.add.memo; if (state.code) renderSettings(byCode.get(state.code)); }
  window.addEventListener('pageshow', restoreControls);
  window.addEventListener('popstate', () => { const p = new URLSearchParams(location.search); state.list = p.get('list') || 'all'; state.query = p.get('q') || ''; state.sort = p.get('sort') || 'added'; select(byCode.has(p.get('code')) ? p.get('code') : ''); restoreControls(); renderPills(); renderList(); });
  if (!['all', 'unclassified', ...data.lists.map(l => String(l.id))].includes(state.list)) state.list = 'all';
  const initial = params.get('code') || location.hash.replace('#wl-', '');
  if (byCode.has(initial) && inList(byCode.get(initial), state.list)) select(initial);
  restoreControls(); renderPills(); renderList(); updateURL(false);
  if (!data.stocks.length) { detail.hidden = true; $('wl-no-selection').hidden = false; }
})();
