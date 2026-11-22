let activeSession = null;
let token = ""; // Intentionally only in JS memory; not localStorage.
const $ = id => document.getElementById(id);
const headers = () => token ? { Authorization: `Bearer ${token}` } : {};
async function request(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}
function show(id, value) { $(id).textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
function busy(id, state) { $(id).disabled = state; }
if ($('token-save')) {
  $('token-save').addEventListener('click', () => { token = $('token').value.trim(); $('token').value = ''; alert(token ? 'Token set for this browser tab.' : 'Token cleared.'); });
}
for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    for (const t of document.querySelectorAll('.tab')) { const on = t === tab; t.classList.toggle('active', on); t.setAttribute('aria-selected', String(on)); }
    for (const panel of document.querySelectorAll('.panel')) panel.classList.toggle('active', panel.id === tab.dataset.tab);
  });
}
$('upload-btn').addEventListener('click', async () => {
  const files = $('chat-files').files;
  if (!files.length) return show('session-status', 'Choose files first.');
  setIndexBusy(true);
  try {
    const session = await request('/api/sessions', { method: 'POST' });
    const body = new FormData();
    [...files].forEach(file => body.append('files', file));
    const result = await request(`/api/sessions/${session.session_id}/documents`, { method: 'POST', body });
    activateSession(session.session_id);
    show('session-status', `Uploaded and indexed ${result.added_documents} documents / ${result.added_chunks} chunks.`);
  } catch (e) { show('session-status', `Error: ${e.message}`); }
  finally { setIndexBusy(false); }
});
$('dataset-btn').addEventListener('click', async () => {
  setIndexBusy(true);
  show('session-status', 'Loading and indexing the server dataset…');
  try {
    const session = await request('/api/sessions', { method: 'POST' });
    const result = await request(`/api/sessions/${session.session_id}/dataset`, { method: 'POST' });
    activateSession(session.session_id);
    const skipped = result.skipped_files.length ? ` ${result.skipped_files.length} files skipped.` : '';
    show('session-status', `Dataset ready: ${result.added_documents} documents / ${result.added_chunks} chunks indexed.${skipped}`);
  } catch (e) { show('session-status', `Error: ${e.message}`); }
  finally { setIndexBusy(false); }
});
function setIndexBusy(state) {
  busy('upload-btn', state);
  busy('dataset-btn', state);
}
function activateSession(sessionId) {
  activeSession = sessionId;
  show('session-id', sessionId);
  show('answer', 'Knowledge base ready. Ask your first question.');
  show('sources', 'No citations yet.');
}
$('ask-btn').addEventListener('click', async () => {
  if (!activeSession) return show('answer', 'Index documents first.');
  const question = $('question').value.trim();
  if (!question) return show('answer', 'Type a question.');
  busy('ask-btn', true);
  try {
    const result = await request(`/api/sessions/${activeSession}/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question, top_k: 4 })
    });
    show('answer', result.answer);
    const sources = $('sources'); sources.replaceChildren();
    for (const source of result.sources) {
      const node = document.createElement('div'); node.className = 'source';
      const title = document.createElement('strong'); title.textContent = `${source.document} · page ${source.page} · similarity ${source.score}`;
      const desc = document.createElement('p'); desc.textContent = source.excerpt;
      node.append(title, desc); sources.appendChild(node);
    }
  } catch (e) { show('answer', `Error: ${e.message}`); }
  finally { busy('ask-btn', false); }
});
$('analyze-btn').addEventListener('click', async () => {
  const file = $('analyze-file').files[0]; if (!file) return show('analyze-result', 'Choose a document.');
  busy('analyze-btn', true);
  try { const body = new FormData(); body.append('file', file); show('analyze-result', await request('/api/analyze', { method: 'POST', body })); }
  catch(e) { show('analyze-result', `Error: ${e.message}`); }
  finally { busy('analyze-btn', false); }
});
$('compare-btn').addEventListener('click', async () => {
  const a = $('reference-file').files[0], b = $('updated-file').files[0];
  if (!a || !b) return show('compare-result', 'Choose both documents.');
  busy('compare-btn', true);
  try { const body = new FormData(); body.append('reference', a); body.append('actual', b); show('compare-result', await request('/api/compare', { method: 'POST', body })); }
  catch(e) { show('compare-result', `Error: ${e.message}`); }
  finally { busy('compare-btn', false); }
});
