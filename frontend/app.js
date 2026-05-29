/* ── DataHarmonizer — frontend logic ───────────────────────────────────── */

const API = '';  // same-origin — Flask serves both static + API

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  sessionId: null,
  pendingFiles: [],      // File objects staged for upload
  uploadedFiles: [],     // {filename, rows, columns}
  searchResults: [],     // i14y dataset cards
  matchResults: null,    // comparison result
  step: 0,              // 1 = discover, 2 = match, 3 = export
};

// ── DOM refs ───────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const chatMessages  = $('chatMessages');
const chatScroll    = $('chatScroll');
const msgInput      = $('msgInput');
const sendBtn       = $('sendBtn');
const fileInput     = $('fileInput');
const chips         = $('chips');
const sidebarFiles  = $('sidebarFiles');
const newChatBtn    = $('newChatBtn');

// ── Boot ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  appendWelcome();
  attachEventListeners();
});

// ── Event wiring ───────────────────────────────────────────────────────────
function attachEventListeners() {
  sendBtn.addEventListener('click', handleSubmit);
  msgInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
  });
  msgInput.addEventListener('input', autoResize);

  fileInput.addEventListener('change', () => {
    Array.from(fileInput.files).forEach(addPendingFile);
    fileInput.value = '';
  });

  newChatBtn.addEventListener('click', resetSession);
}

// ── Auto-grow textarea ─────────────────────────────────────────────────────
function autoResize() {
  msgInput.style.height = 'auto';
  msgInput.style.height = Math.min(msgInput.scrollHeight, 200) + 'px';
}

// ── Pending file chips ─────────────────────────────────────────────────────
function addPendingFile(file) {
  if (state.pendingFiles.find(f => f.name === file.name)) return;
  state.pendingFiles.push(file);

  const chip = document.createElement('div');
  chip.className = 'chip';
  chip.dataset.name = file.name;
  chip.innerHTML = `
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
      <path d="M2 2h5l3 3v5H2V2z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>
    </svg>
    ${escHtml(file.name)}
    <button class="chip-remove" title="Remove">×</button>`;
  chip.querySelector('.chip-remove').addEventListener('click', () => {
    state.pendingFiles = state.pendingFiles.filter(f => f.name !== file.name);
    chip.remove();
  });
  chips.appendChild(chip);
}

// ── Main submit handler ────────────────────────────────────────────────────
async function handleSubmit() {
  const question = msgInput.value.trim();
  const hasFiles = state.pendingFiles.length > 0;

  if (!question && !hasFiles) return;

  // Show user message
  const label = hasFiles
    ? `${state.pendingFiles.map(f => `📎 ${f.name}`).join('  ')}${question ? '\n\n' + question : ''}`
    : question;
  appendUserMessage(label);

  // Clear input
  msgInput.value = '';
  msgInput.style.height = 'auto';
  chips.innerHTML = '';
  sendBtn.disabled = true;

  const filesToUpload = [...state.pendingFiles];
  state.pendingFiles = [];

  // Show typing
  const typingId = showTyping();

  try {
    // 1. Upload files if attached
    if (filesToUpload.length) {
      await doUpload(filesToUpload);
    }

    // 2. Run i14y search
    if (question || state.uploadedFiles.length) {
      await doSearch(question);
    }
  } catch (err) {
    hideTyping(typingId);
    appendAssistantMessage(`<span class="text-danger">⚠ ${escHtml(err.message)}</span>`);
  } finally {
    sendBtn.disabled = false;
  }
}

// ── Step 1 — Upload ────────────────────────────────────────────────────────
async function doUpload(files) {
  const form = new FormData();
  if (state.sessionId) form.append('session_id', state.sessionId);
  files.forEach(f => form.append('files', f));

  const res = await fetchJSON('/api/upload', { method: 'POST', body: form });
  state.sessionId = res.session_id;
  state.uploadedFiles.push(...res.uploaded);
  updateSidebarFiles();
}

// ── Step 1 — Search ────────────────────────────────────────────────────────
async function doSearch(question) {
  setStep(1);
  const res = await fetchJSON('/api/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: state.sessionId, question }),
  });
  state.sessionId = res.session_id || state.sessionId;
  state.searchResults = res.results;
  hideAllTyping();
  appendSearchResults(res.results, res.query, res.intro || '');
}

// ── Step 2 — Compare ───────────────────────────────────────────────────────
async function doCompare(datasetId, datasetTitle, sourceFilename) {
  setStep(2);
  const typingId = showTyping();
  disableAllCompareButtons();

  try {
    const res = await fetchJSON('/api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.sessionId,
        dataset_id: datasetId,
        source_filename: sourceFilename,
      }),
    });
    state.matchResults = res;
    hideTyping(typingId);
    appendMatchResults(res, datasetTitle);
    setStep(3);
    appendExportBlock();
  } catch (err) {
    hideTyping(typingId);
    appendAssistantMessage(`<span class="text-danger">⚠ ${escHtml(err.message)}</span>`);
  }
}

// ── Step 3 — Export ────────────────────────────────────────────────────────
function doExport() {
  if (!state.sessionId) return;
  window.location.href = `${API}/api/export/${encodeURIComponent(state.sessionId)}`;
}

// ── Message renderers ──────────────────────────────────────────────────────
function appendWelcome() {
  appendAssistantMessage(`
    <div class="msg-content">
      <p><strong>Welcome to DataHarmonizer 👋</strong></p>
      <p class="text-muted" style="margin-top:4px">
        Upload one or more datasets (CSV or XML) and ask a question — I'll search
        <strong>I14Y</strong> for related datasets, let you compare schemas using
        <strong>Valentine</strong>, and export a ready-to-upload mapping table.
      </p>
      <p class="text-muted" style="margin-top:8px; font-size:12px">
        Example: <em>"Do other cantonal business registries exist?"</em> or
        <em>"Is there a communal version of this dataset?"</em>
      </p>
    </div>`);
}

function appendUserMessage(text) {
  const div = document.createElement('div');
  div.className = 'msg msg-user';
  div.innerHTML = `<div class="msg-bubble">${escHtml(text).replace(/\n/g, '<br>')}</div>`;
  chatMessages.appendChild(div);
  scrollToBottom();
}

function appendAssistantMessage(html) {
  const div = document.createElement('div');
  div.className = 'msg msg-assistant';
  div.innerHTML = `<div class="msg-bubble">${html}</div>`;
  chatMessages.appendChild(div);
  scrollToBottom();
  return div;
}

function appendSearchResults(results, query, intro) {
  if (!results || results.length === 0) {
    appendAssistantMessage(`<span class="text-muted">No datasets found on I14Y for <em>"${escHtml(query)}"</em>. Try a different question.</span>`);
    return;
  }

  const sourceFile = state.uploadedFiles.length ? state.uploadedFiles[0].filename : null;

  const cardsHtml = results.map((ds, i) => {
    const i14yUrl = `https://www.i14y.admin.ch/de/catalog/datasets/${encodeURIComponent(ds.id || '')}`;
    return `
    <div class="dataset-card" data-id="${escAttr(ds.id)}" data-title="${escAttr(ds.title || ds.id)}" data-source="${escAttr(sourceFile || '')}">
      <div class="card-header">
        <div style="flex:1;min-width:0">
          <div class="card-title">
            <a href="${escAttr(i14yUrl)}" target="_blank" rel="noopener" class="card-title-link">${escHtml(ds.title || 'Untitled dataset')}</a>
          </div>
          ${ds.publisher ? `<div class="card-publisher">${escHtml(ds.publisher)}</div>` : ''}
        </div>
        ${sourceFile ? `
        <label class="card-checkbox-wrap" title="Select for comparison">
          <input type="checkbox" class="card-checkbox" data-id="${escAttr(ds.id)}">
        </label>` : ''}
      </div>
      ${ds.description ? `<div class="card-desc">${escHtml(ds.description)}</div>` : ''}
      ${ds.llm_reason ? `<div class="card-reason">✦ ${escHtml(ds.llm_reason)}</div>` : ''}
      <div class="card-tags">
        ${ds.has_download ? `<span class="tag tag-dl">↓ downloadable</span>` : ''}
        ${ds.format ? `<span class="tag tag-fmt">${escHtml(ds.format)}</span>` : ''}
        ${ds.structure_url ? `<span class="tag">structure</span>` : ''}
        ${(ds.themes || []).map(t => {
          const label = typeof t === 'string' ? t : (t.name?.en || t.name?.de || t.name?.fr || t.name?.it || t.code || JSON.stringify(t));
          return `<span class="tag">${escHtml(label)}</span>`;
        }).join('')}
      </div>
      ${!sourceFile ? `<p class="text-muted" style="font-size:12px; margin-top:4px">Upload a dataset to compare schemas.</p>` : ''}
    </div>`;
  }).join('');

  const introHtml = intro ? `<p class="llm-intro">${escHtml(intro)}</p>` : `<p>Found <strong>${results.length}</strong> relevant dataset${results.length !== 1 ? 's' : ''} on I14Y for <em>"${escHtml(query)}"</em>:</p>`;

  const compareBarHtml = sourceFile ? `
    <div class="compare-bar" style="display:none">
      <span class="compare-bar-label">1 dataset selected</span>
      <button class="btn btn-primary btn-run-compare">Compare schemas</button>
    </div>` : '';

  const div = appendAssistantMessage(`
    <div class="msg-content">
      ${introHtml}
      <div class="dataset-cards">${cardsHtml}</div>
      ${compareBarHtml}
    </div>`);

  if (!sourceFile) return;

  const bar = div.querySelector('.compare-bar');
  const barLabel = div.querySelector('.compare-bar-label');
  const runBtn = div.querySelector('.btn-run-compare');

  // Update bar visibility when checkboxes change
  div.querySelectorAll('.card-checkbox').forEach(cb => {
    cb.addEventListener('change', () => {
      const checked = [...div.querySelectorAll('.card-checkbox:checked')];
      if (checked.length === 0) {
        bar.style.display = 'none';
      } else {
        bar.style.display = 'flex';
        barLabel.textContent = `${checked.length} dataset${checked.length > 1 ? 's' : ''} selected`;
      }
      // Highlight selected cards
      div.querySelectorAll('.dataset-card').forEach(card => {
        const cardCb = card.querySelector('.card-checkbox');
        card.classList.toggle('card-selected', cardCb?.checked || false);
      });
    });
  });

  // Run compare for each checked dataset
  runBtn.addEventListener('click', () => {
    const checked = [...div.querySelectorAll('.card-checkbox:checked')];
    checked.forEach(cb => {
      const card = cb.closest('.dataset-card');
      doCompare(card.dataset.id, card.dataset.title, card.dataset.source);
      cb.checked = false;
      card.classList.remove('card-selected');
    });
    bar.style.display = 'none';
  });
}

function appendMatchResults(res, datasetTitle) {
  const pct = Math.round((res.compatibility_score || 0) * 100);
  const { exact_match: exact, close_match: close, incompatible } = res.stats;
  const total = exact + close + incompatible;

  // Colour for the ring
  const ringColor = pct >= 75 ? '#22c55e' : pct >= 50 ? '#f59e0b' : '#ef4444';

  // Circumference for SVG circle
  const r = 28, circ = 2 * Math.PI * r;
  const dash = (pct / 100) * circ;

  // Build rows
  const rows = (res.best_matches || [])
    .sort((a, b) => (b.hybrid_score || 0) - (a.hybrid_score || 0))
    .map(row => {
      const score = +(row.hybrid_score || 0);
      const cat   = row.category || 'incompatible';
      const catClass = cat === 'exact_match' ? 'cat-exact' : cat === 'close_match' ? 'cat-close' : 'cat-incompat';
      const catLabel = cat.replace('_match', '').replace('_', ' ');
      const barColor = cat === 'exact_match' ? '#22c55e' : cat === 'close_match' ? '#f59e0b' : '#ef4444';
      const signal   = row.i14y_signal || '';

      return `
        <tr>
          <td><code style="font-size:12px">${escHtml(row.source_col || '')}</code></td>
          <td><code style="font-size:12px">${escHtml(row.target_col || '—')}</code></td>
          <td>
            <div class="score-bar-wrap">
              <div class="score-bar"><div class="score-bar-fill" style="width:${Math.round(score*100)}%;background:${barColor}"></div></div>
              <span style="font-size:12px;color:var(--text-muted);min-width:32px">${Math.round(score * 100)}%</span>
            </div>
          </td>
          <td><span class="cat-chip ${catClass}">${catLabel}</span></td>
          <td>${signal ? `<span class="i14y-signal">★ ${escHtml(signal)}</span>` : '<span class="text-muted">—</span>'}</td>
        </tr>`;
    }).join('');

  const conceptCount = Object.keys(res.col_concepts || {}).length;

  const html = `
    <div class="msg-content">
      <p>Schema matching complete: <strong>${escHtml(res.source)}</strong> vs <strong>${escHtml(datasetTitle)}</strong></p>

      <div class="compat-summary">
        <div class="compat-score-ring">
          <svg width="72" height="72" viewBox="0 0 72 72">
            <circle cx="36" cy="36" r="${r}" fill="none" stroke="var(--border)" stroke-width="6"/>
            <circle cx="36" cy="36" r="${r}" fill="none" stroke="${ringColor}" stroke-width="6"
              stroke-dasharray="${dash.toFixed(1)} ${circ.toFixed(1)}"
              stroke-linecap="round"/>
          </svg>
          <div class="compat-score-text" style="color:${ringColor}">${pct}%</div>
        </div>
        <div class="compat-details">
          <div class="compat-title">Compatibility score</div>
          <div class="compat-pills">
            <span class="pill pill-exact">✓ ${exact} exact</span>
            <span class="pill pill-close">~ ${close} close</span>
            <span class="pill pill-incompat">✗ ${incompatible} incompatible</span>
          </div>
          ${conceptCount ? `<div style="font-size:12px;color:var(--text-muted);margin-top:2px">★ ${conceptCount} columns matched to I14Y concepts</div>` : ''}
        </div>
      </div>

      <div class="match-table-wrap">
        <table class="match-table">
          <thead>
            <tr>
              <th>Source column</th>
              <th>Target column</th>
              <th>Score</th>
              <th>Category</th>
              <th>I14Y signal</th>
            </tr>
          </thead>
          <tbody>${rows || '<tr><td colspan="5" class="text-muted" style="padding:16px">No column pairs found.</td></tr>'}</tbody>
        </table>
      </div>
    </div>`;

  appendAssistantMessage(html);
}

function appendExportBlock() {
  const div = appendAssistantMessage(`
    <div class="msg-content">
      <p>Results are ready to export.</p>
      <div class="export-block">
        <div class="export-title">📦 Transformation export</div>
        <div class="export-desc">
          Download a ZIP containing three files: a <strong>mapping_table.csv</strong>
          (compatible with I14Y upload format), a
          <strong>transformation_recipe.json</strong> with full field-level
          transformation rules, and an <strong>executive_summary.txt</strong>
          with a plain-language overview of all proposed transformations.
        </div>
        <div class="export-files">
          <div class="export-file">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M10 2v4h4" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
            mapping_table.csv — field-level concept mappings for I14Y
          </div>
          <div class="export-file">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M10 2v4h4" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
            transformation_recipe.json — full transformation plan
          </div>
          <div class="export-file">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M10 2v4h4" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
            executive_summary.txt — plain-language transformation overview
          </div>
        </div>
        <button class="btn btn-success" id="exportBtn">⬇ Download transformation_export.zip</button>
      </div>
    </div>`);

  div.querySelector('#exportBtn').addEventListener('click', doExport);
}

// ── Typing indicator ───────────────────────────────────────────────────────
let _typingCounter = 0;

function showTyping() {
  const id = `typing-${++_typingCounter}`;
  const div = document.createElement('div');
  div.className = 'msg msg-assistant';
  div.id = id;
  div.innerHTML = `
    <div class="typing-indicator">
      <div class="typing-dot"></div>
      <div class="typing-dot"></div>
      <div class="typing-dot"></div>
    </div>`;
  chatMessages.appendChild(div);
  scrollToBottom();
  return id;
}

function hideTyping(id) {
  const el = $(id);
  if (el) el.remove();
}

function hideAllTyping() {
  chatMessages.querySelectorAll('[id^="typing-"]').forEach(el => el.remove());
}

// ── Sidebar helpers ────────────────────────────────────────────────────────
function updateSidebarFiles() {
  if (!state.uploadedFiles.length) {
    sidebarFiles.innerHTML = '<p class="muted-hint">No files yet</p>';
    return;
  }
  sidebarFiles.innerHTML = state.uploadedFiles.map(f => `
    <div class="sidebar-file-item">
      <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
        <path d="M2 1.5h5.5l3 3v7H2v-10z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>
      </svg>
      <span class="sidebar-file-name" title="${escAttr(f.filename)}">${escHtml(f.filename)}</span>
      <span class="text-muted" style="margin-left:auto;font-size:11px">${f.rows}r</span>
    </div>`).join('');
}

function setStep(n) {
  state.step = n;
  [1, 2, 3].forEach(i => {
    const el = $(`step${i}`);
    if (!el) return;
    el.classList.remove('active', 'done');
    if (i < n)  el.classList.add('done');
    if (i === n) el.classList.add('active');
  });
}

function disableAllCompareButtons() {
  chatMessages.querySelectorAll('.btn-compare').forEach(b => {
    b.disabled = true;
    b.textContent = 'Comparing…';
  });
}

// ── Session reset ──────────────────────────────────────────────────────────
function resetSession() {
  state.sessionId     = null;
  state.pendingFiles  = [];
  state.uploadedFiles = [];
  state.searchResults = [];
  state.matchResults  = null;
  state.step          = 0;

  chatMessages.innerHTML = '';
  chips.innerHTML        = '';
  sidebarFiles.innerHTML = '<p class="muted-hint">No files yet</p>';
  [1, 2, 3].forEach(i => {
    const el = $(`step${i}`);
    if (el) el.classList.remove('active', 'done');
  });
  appendWelcome();
}

// ── Utilities ──────────────────────────────────────────────────────────────
async function fetchJSON(url, options = {}) {
  const res = await fetch(API + url, options);
  const json = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  });
}

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escAttr(s) {
  return String(s ?? '').replace(/"/g, '&quot;');
}
