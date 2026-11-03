/* 銘柄詳細とウォッチリストで同じ履歴・編集UIを利用する。 */
(function () {
  'use strict';
  const node = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text != null) el.textContent = text; return el; };
  const safeURL = value => { try { const u = new URL(value); return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password ? u.href : ''; } catch (_) { return ''; } };
  window.StockNotes = { mount(root, onChange) {
    if (root._notes) return root._notes;
    const form = root.querySelector('.sn-editor'), timeline = root.querySelector('.sn-timeline'), status = root.querySelector('.sn-status');
    const csrf = (form.elements.csrf?.value || '');
    const state = { code: '', rows: [], query: '', kind: 'all', order: 'desc', expanded: new Set(), draft: {}, revision: 0, busy: false };
    const empty = () => ({ id: '', title: '', content: '', source_url: '', open: false });
    const key = code => 'stock-notes-draft:' + code;
    const hasDraft = () => !!(state.draft.title || state.draft.content || state.draft.source_url);
    function persist() { try { sessionStorage.setItem(key(state.code), JSON.stringify(state.draft)); } catch (_) {} }
    function syncForm() {
      ['title', 'content', 'source_url', 'id'].forEach(k => { form.elements[k].value = state.draft[k] || ''; });
      form.elements.code.value = state.code;
      form.hidden = !state.draft.open;
      root.querySelector('.sn-editor-title').textContent = state.draft.id ? '保存済みの情報を編集' : '新しい情報を追加';
      root.querySelector('.sn-save').textContent = state.busy ? '保存中…' : state.draft.id ? '変更を保存' : '追加する';
      root.querySelector('.sn-save').disabled = state.busy;
    }
    ['title', 'content', 'source_url'].forEach(k => form.elements[k].addEventListener('input', e => { state.draft[k] = e.target.value; persist(); }));
    function begin(entry) {
      if (state.busy) return;
      if (hasDraft() && String(state.draft.id) !== String(entry ? entry.id : '') && !confirm('入力中の下書きを置き換えますか？')) return;
      if (entry) state.draft = { id: entry.id, title: entry.title, content: entry.content, source_url: entry.source_url, open: true };
      else if (state.draft.id) state.draft = { ...empty(), open: true };
      else state.draft.open = true;
      persist(); syncForm(); form.elements.title.focus();
    }
    root.querySelector('.sn-new').addEventListener('click', () => begin(null));
    root.querySelector('.sn-cancel').addEventListener('click', () => { state.draft.open = false; persist(); syncForm(); });
    function appendText(el, content) {
      // 旧メモに直接貼られたURLも、安全な外部リンクとして開ける。
      const regex = /https?:\/\/[^\s<>"'）)]+/g; let offset = 0;
      for (const match of content.matchAll(regex)) {
        el.append(document.createTextNode(content.slice(offset, match.index)));
        const href = safeURL(match[0]);
        if (href) { const a = node('a', '', match[0]); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; el.append(a); }
        else el.append(document.createTextNode(match[0]));
        offset = match.index + match[0].length;
      }
      el.append(document.createTextNode(content.slice(offset)));
    }
    function render() {
      root.querySelector('.sn-count').textContent = state.rows.length + '件';
      let rows = state.rows.filter(r => (state.kind === 'all' || r.kind === state.kind) && [r.content, r.title, r.source_url].join(' ').toLocaleLowerCase().includes(state.query.toLocaleLowerCase()));
      if (state.order === 'asc') rows = rows.slice().reverse();
      timeline.replaceChildren();
      if (!rows.length) { timeline.append(node('p', 'sn-empty', state.rows.length ? '条件に合う情報がありません' : 'まだ記録はありません。メモや参考URLを最初の1件から残せます。')); return; }
      rows.forEach(entry => {
        const card = node('article', 'sn-entry'); card.dataset.id = entry.id;
        const head = node('div', 'sn-entry-head'); head.append(node('span', 'sn-kind', { x: 'Xの投稿', link: '参考URL', memo: 'メモ' }[entry.kind]), node('time', '', entry.created_at.slice(0, 16))); card.append(head);
        if (entry.title) card.append(node('h4', '', entry.title));
        const bodyText = entry.content === entry.source_url ? '' : entry.content;
        const content = node('div', 'sn-content'); appendText(content, bodyText);
        const long = bodyText.length > 400 || bodyText.split('\n').length > 6;
        content.classList.toggle('sn-collapsed', long && !state.expanded.has(entry.id)); card.append(content);
        if (long) { const expand = node('button', 'sn-expand'); expand.type = 'button';
          const update = () => { const open = state.expanded.has(entry.id); content.classList.toggle('sn-collapsed', !open); expand.textContent = open ? '折りたたむ' : '全文を表示'; expand.setAttribute('aria-expanded', String(open)); };
          update(); expand.onclick = () => { state.expanded.has(entry.id) ? state.expanded.delete(entry.id) : state.expanded.add(entry.id); update(); }; card.append(expand); }
        const href = safeURL(entry.source_url);
        if (href) { const a = node('a', 'sn-link', '↗ ' + entry.source_url); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; card.append(a); }
        const actions = node('div', 'sn-entry-actions'), edit = node('button', '', '編集'), del = node('button', 'sn-delete', '削除'); edit.type = del.type = 'button';
        edit.onclick = () => { begin(entry); form.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); };
        del.onclick = async () => {
          if (state.busy || !confirm('この記録を削除しますか？')) return;
          const code = state.code; del.disabled = true;
          try {
            await write('/memo/delete', { id: entry.id, code });
            if (code === state.code) {
              if (String(state.draft.id) === String(entry.id)) { state.draft = empty(); persist(); syncForm(); }
              await refresh(code);
            }
          }
          catch (error) { if (code === state.code) status.textContent = error.message; } finally { del.disabled = false; }
        };
        actions.append(edit, del); card.append(actions); timeline.append(card);
      });
    }
    async function write(url, values) {
      const response = await fetch(url, { method: 'POST', headers: { Accept: 'application/json' }, body: new URLSearchParams({ ...values, csrf }) });
      if (response.redirected) throw new Error('編集にはパスコードでのログインが必要です。下書きは保持しています。');
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || '保存できませんでした。入力を残しているので、もう一度お試しください。');
      return result;
    }
    async function refresh(code) {
      const revision = ++state.revision; status.textContent = '記録を読み込み中…';
      try {
        const response = await fetch('/api/watchlist/memos/' + encodeURIComponent(code));
        if (!response.ok) throw new Error('記録を取得できませんでした');
        const rows = await response.json();
        if (revision !== state.revision || code !== state.code) return;
        state.rows = rows; status.textContent = ''; render();
        if (onChange) onChange(code, rows);
      } catch (error) { if (revision === state.revision) { status.replaceChildren(node('span', '', error.message + ' ')); const retry = node('button', 'btn-sm', '再試行'); retry.onclick = () => refresh(code); status.append(retry); } }
    }
    form.addEventListener('submit', async e => {
      e.preventDefault(); if (state.busy) return;
      const code = state.code, draft = { ...state.draft };
      state.busy = true; root.querySelector('.sn-error').textContent = ''; syncForm();
      try {
        await write(draft.id ? '/memo/edit' : '/memo/add', { ...draft, code });
        try { sessionStorage.removeItem(key(code)); } catch (_) {}
        if (code === state.code) { state.draft = empty(); syncForm(); await refresh(code); }
      } catch (error) { if (code === state.code) root.querySelector('.sn-error').textContent = error.message; }
      finally { state.busy = false; syncForm(); }
    });
    root.querySelector('.sn-search').oninput = e => { state.query = e.target.value; render(); };
    root.querySelector('.sn-filter').onchange = e => { state.kind = e.target.value; render(); };
    root.querySelector('.sn-order').onchange = e => { state.order = e.target.value; render(); };
    function select(code) {
      if (state.code) persist();
      state.code = code; state.rows = []; state.query = ''; state.kind = 'all'; state.expanded.clear();
      state.draft = empty();
      try { state.draft = { ...empty(), ...JSON.parse(sessionStorage.getItem(key(code)) || '{}') }; } catch (_) {}
      root.querySelector('.sn-search').value = ''; root.querySelector('.sn-filter').value = 'all';
      root.querySelector('.sn-error').textContent = ''; timeline.replaceChildren(); syncForm(); refresh(code);
    }
    window.addEventListener('pageshow', () => { syncForm(); root.querySelector('.sn-search').value = state.query; root.querySelector('.sn-filter').value = state.kind; root.querySelector('.sn-order').value = state.order; });
    root._notes = { select, refresh }; return root._notes;
  } };
  document.querySelectorAll('.sn-root[data-code]').forEach(root => { if (root.dataset.code) window.StockNotes.mount(root).select(root.dataset.code); });
})();
