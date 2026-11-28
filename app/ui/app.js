let activeSession = null;
let token = ""; // Intentionally only in JS memory; not localStorage.
let sessionKey = 'edis.activeSession';
let accountEnabled = false;
let conversationId = null;
const conversationIdPattern = /^[a-f0-9]{32}$/;
const runningOperations = new Map();
const operationLabels = {
  'upload-btn': 'Uploading and indexing documents',
  'dataset-btn': 'Loading dataset',
  'ask-btn': 'Preparing a reply',
  'analyze-btn': 'Generating summary',
  'compare-btn': 'Comparing documents',
};
const $ = id => document.getElementById(id);
const headers = () => token ? { Authorization: `Bearer ${token}` } : {};
async function request(url, options = {}, { includeHeaders = false } = {}) {
  const response = await fetch(url, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
  if (response.status === 401 && accountEnabled) window.location.replace('/login');
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('; ') : data.detail;
    const error = new Error(detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return includeHeaders ? { data, headers: response.headers } : data;
}
function show(id, value) { $(id).textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
function busy(id, state) { $(id).disabled = state; }
function processing(id, state, label = operationLabels[id]) {
  const button = $(id);
  button.setAttribute('aria-busy', String(state));
  busy(id, state);
  if (id === 'ask-btn') busy('new-chat-btn', state);
  if (state) runningOperations.set(id, label);
  else runningOperations.delete(id);

  const labels = [...runningOperations.values()];
  $('processing-status').hidden = labels.length === 0;
  show('processing-message', labels.length > 1 ? `${labels.length} operations in progress` : labels[0] || '');
}
if ($('token-save')) {
  $('token-save').addEventListener('click', () => { token = $('token').value.trim(); $('token').value = ''; alert(token ? 'Token set for this browser tab.' : 'Token cleared.'); });
}
const workspaceTabs = [...document.querySelectorAll('.tab')];
function selectWorkspaceTab(tab) {
  for (const candidate of workspaceTabs) {
    const selected = candidate === tab;
    candidate.classList.toggle('active', selected);
    candidate.setAttribute('aria-selected', String(selected));
  }
  for (const panel of document.querySelectorAll('.panel')) {
    panel.classList.toggle('active', panel.id === tab.dataset.tab);
  }
}
const initialTab = new URLSearchParams(window.location.search).get('tab');
const requestedTab = workspaceTabs.find(tab => tab.dataset.tab === initialTab);
if (requestedTab) selectWorkspaceTab(requestedTab);
for (const tab of workspaceTabs) {
  tab.addEventListener('click', () => {
    selectWorkspaceTab(tab);
    const url = new URL(window.location.href);
    if (tab.dataset.tab === 'chat') url.searchParams.delete('tab');
    else url.searchParams.set('tab', tab.dataset.tab);
    window.history.replaceState(null, '', url);
  });
}
$('upload-btn').addEventListener('click', async () => {
  const files = $('chat-files').files;
  if (!files.length) return show('session-status', 'Choose files first.');
  setIndexBusy(true);
  processing('upload-btn', true);
  show('session-status', 'Uploading and indexing your documents…');
  try {
    const session = await request('/api/sessions', { method: 'POST' });
    const body = new FormData();
    [...files].forEach(file => body.append('files', file));
    const result = await request(`/api/sessions/${session.session_id}/documents`, { method: 'POST', body });
    activateSession(session.session_id);
    show('session-status', `Uploaded and indexed ${result.added_documents} documents / ${result.added_chunks} chunks.`);
  } catch (e) { showIndexError(e); }
  finally { processing('upload-btn', false); setIndexBusy(false); }
});
$('dataset-btn').addEventListener('click', async () => {
  setIndexBusy(true);
  processing('dataset-btn', true);
  show('session-status', 'Checking for a saved dataset index…');
  try {
    const { session: saved } = await request('/api/dataset/session');
    if (saved) {
      showRestoredSession(saved);
      return;
    }
    show('session-status', 'Loading and indexing the server dataset…');
    const session = await request('/api/sessions', { method: 'POST' });
    const result = await request(`/api/sessions/${session.session_id}/dataset`, { method: 'POST' });
    activateSession(session.session_id);
    const skipped = result.skipped_files.length ? ` ${result.skipped_files.length} files skipped.` : '';
    show('session-status', `Dataset ready: ${result.added_documents} documents / ${result.added_chunks} chunks indexed.${skipped}`);
  } catch (e) { showIndexError(e); }
  finally { processing('dataset-btn', false); setIndexBusy(false); }
});
function setIndexBusy(state) {
  setSourceButtons(state);
  busy('ask-btn', state);
  busy('new-chat-btn', state);
  if (state) chatStatus('Indexing documents…');
}
function setSourceButtons(state) {
  busy('upload-btn', state);
  busy('dataset-btn', state);
}
function chatStatus(message, error = false) {
  $('chat-status').dataset.error = String(error);
  show('chat-status', message);
}
function showIndexError(error) {
  show('session-status', `Error: ${error.message}`);
  chatStatus(`Could not index documents: ${error.message}`, true);
}
function conversationKey() {
  return `${sessionKey}.conversation.${activeSession ?? 'general'}`;
}
function savedConversationId() {
  let id;
  try { id = sessionStorage.getItem(conversationKey()); } catch {}
  return conversationIdPattern.test(id) ? id : null;
}
function startConversation(id) {
  conversationId = id ?? [...crypto.getRandomValues(new Uint8Array(16))]
    .map(byte => byte.toString(16).padStart(2, '0')).join('');
  try { sessionStorage.setItem(conversationKey(), conversationId); } catch {}
}
function activateSession(sessionId, history = []) {
  const currentId = activeSession === sessionId ? conversationId : null;
  activeSession = sessionId;
  try { sessionStorage.setItem(sessionKey, sessionId); } catch {}
  const latestId = history.findLast(turn => conversationIdPattern.test(turn.conversation_id))?.conversation_id;
  startConversation(currentId ?? savedConversationId() ?? latestId);
  show('chat-intro', 'AI can make mistakes. Verify important information.');
  renderConversation(history.filter(turn => turn.conversation_id === conversationId));
  chatStatus('');
}
function showRestoredSession(session) {
  activateSession(session.session_id, session.history);
  show('session-status', `Reused saved index for ${session.documents.length} documents.`);
}
async function restoreGeneralChat() {
  const savedId = savedConversationId();
  startConversation(savedId);
  if (savedId) {
    const { history } = await request(`/api/chat/history/${savedId}`);
    renderConversation(history);
  }
  chatStatus('');
}
async function restoreSession() {
  processing('ask-btn', true, 'Checking for saved documents');
  setSourceButtons(true);
  chatStatus('Checking for saved documents…');
  try {
    const account = await request('/api/auth/session');
    accountEnabled = account.enabled;
    if (accountEnabled) {
      if (!account.account_id) {
        window.location.replace('/login');
        return;
      }
      sessionKey = `edis.activeSession.${account.account_id}`;
      $('sign-out').hidden = false;
    }
    let sessionId;
    try { sessionId = sessionStorage.getItem(sessionKey); } catch {}
    if (sessionId && conversationIdPattern.test(sessionId)) {
      try {
        const session = await request(`/api/sessions/${sessionId}`);
        if (session.documents.length) {
          showRestoredSession(session);
          return;
        }
      } catch (error) {
        if (error.status !== 404) throw error;
      }
      try { sessionStorage.removeItem(sessionKey); } catch {}
    }
    const { session } = await request('/api/dataset/session');
    if (session) showRestoredSession(session);
    else await restoreGeneralChat();
  } catch (error) {
    chatStatus(`Could not restore your session: ${error.message}`, true);
  } finally {
    if (!conversationId) startConversation(savedConversationId());
    processing('ask-btn', false);
    setSourceButtons(false);
  }
}
$('sign-out').addEventListener('click', async () => {
  busy('sign-out', true);
  $('sign-out-error').hidden = true;
  try {
    await request('/api/auth/logout', { method: 'POST' });
    window.location.replace('/login');
  } catch (error) {
    show('sign-out-error', `Could not sign out: ${error.message}`);
    $('sign-out-error').hidden = false;
    busy('sign-out', false);
  }
});
window.addEventListener('pageshow', event => {
  if (event.persisted && accountEnabled) window.location.reload();
});
function chatMessage(role) {
  const isUser = role === 'user';
  const bubble = resultNode('article', `chat-message chat-message-${isUser ? 'user' : 'assistant'}`);
  bubble.append(resultNode('strong', 'chat-author', isUser ? 'You' : 'EDIS'));
  return bubble;
}
function fillChatMessage(bubble, text, sources = []) {
  bubble.querySelector('.typing-indicator')?.remove();
  bubble.append(resultNode('p', 'chat-message-text', text));
  if (!sources.length) return;
  const citations = resultNode('div', 'sources');
  for (const source of sources) {
    const card = resultNode('details', 'source');
    card.append(
      resultNode('summary', null, `${source.document} · Page ${source.page}`),
      resultNode('p', null, source.excerpt)
    );
    citations.append(card);
  }
  bubble.append(citations);
}
function scrollChatToBottom() {
  const transcript = $('chat-history');
  transcript.scrollTop = transcript.scrollHeight;
}
function renderConversation(history) {
  const transcript = $('chat-history');
  const intro = $('chat-intro');
  transcript.replaceChildren(intro);
  for (const turn of history) {
    const bubble = chatMessage(turn.role);
    fillChatMessage(bubble, turn.content, turn.sources);
    transcript.append(bubble);
  }
  intro.hidden = history.length > 0;
  scrollChatToBottom();
}
$('new-chat-btn').addEventListener('click', () => {
  if ($('new-chat-btn').disabled) return;
  startConversation();
  renderConversation([]);
  chatStatus('');
  $('question').value = '';
  $('question').focus();
});
async function askQuestion() {
  if ($('ask-btn').disabled) return;
  const input = $('question');
  const question = input.value.trim();
  if (question.length < 2 || question.length > 2000) {
    return chatStatus('Enter a question between 2 and 2,000 characters.', true);
  }
  processing('ask-btn', true);
  setSourceButtons(true);
  chatStatus('Preparing your reply…');
  input.value = '';
  input.focus();
  $('chat-intro').hidden = true;
  const userMessage = chatMessage('user');
  fillChatMessage(userMessage, question);
  const reply = chatMessage('assistant');
  const typing = resultNode('div', 'typing-indicator');
  typing.setAttribute('aria-hidden', 'true');
  typing.append(document.createElement('i'), document.createElement('i'), document.createElement('i'));
  reply.append(typing);
  const transcript = $('chat-history');
  transcript.append(userMessage, reply);
  scrollChatToBottom();
  try {
    const url = activeSession ? `/api/sessions/${activeSession}/chat` : '/api/chat';
    const result = await request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, top_k: 4, conversation_id: conversationId }),
    });
    const atBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 24;
    fillChatMessage(reply, result.answer, result.sources);
    if (atBottom) transcript.scrollTop += reply.getBoundingClientRect().top - transcript.getBoundingClientRect().top;
    chatStatus('');
  } catch (e) {
    reply.remove();
    chatStatus(`Could not answer: ${e.message}`, true);
    if (!input.value) {
      input.value = question;
      userMessage.remove();
      $('chat-intro').hidden = Boolean(transcript.querySelector('.chat-message'));
    } else {
      userMessage.append(resultNode('p', 'chat-status', 'Reply failed. Send this message again to retry.'));
    }
  } finally {
    processing('ask-btn', false);
    setSourceButtons(false);
  }
}
$('chat-form').addEventListener('submit', event => {
  event.preventDefault();
  askQuestion();
});
restoreSession();
$('question').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    askQuestion();
  }
});

function resultNode(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function resultMeta(entries) {
  const list = resultNode('dl', 'result-meta');
  for (const [label, value] of entries) {
    const item = resultNode('div');
    item.append(resultNode('dt', null, label), resultNode('dd', null, String(value)));
    list.append(item);
  }
  return list;
}

function resultList(title, items, ordered = false, collapsible = false) {
  const section = resultNode(collapsible ? 'details' : 'section', 'result-section');
  section.append(resultNode(collapsible ? 'summary' : 'h4', null, title));
  const list = resultNode(ordered ? 'ol' : 'ul', 'result-list');
  if (items.length) {
    for (const item of items) list.append(resultNode('li', null, item));
    section.append(list);
  } else {
    section.append(resultNode('p', 'result-text', 'None reported.'));
  }
  return section;
}

function addResultReview(output, reviewId) {
  const panel = resultNode('section', 'result-section review-panel');
  const label = resultNode('label', null, 'Review note');
  const note = resultNode('textarea');
  note.rows = 2;
  note.maxLength = 2000;
  note.placeholder = 'Required when requesting changes';
  label.append(note);

  const approve = resultNode('button', 'button button-small button-primary', 'Approve');
  const requestChanges = resultNode('button', 'button button-small button-secondary', 'Request changes');
  approve.type = requestChanges.type = 'button';
  const actions = resultNode('div', 'review-actions');
  actions.append(approve, requestChanges);
  const status = resultNode('p', 'review-status', 'Pending review');
  status.setAttribute('role', 'status');

  async function save(decision) {
    const text = note.value.trim();
    if (decision === 'changes_requested' && !text) {
      status.dataset.error = 'true';
      status.textContent = 'Describe the changes you want.';
      note.focus();
      return;
    }

    approve.disabled = requestChanges.disabled = note.disabled = true;
    status.dataset.error = 'false';
    status.textContent = 'Saving review…';
    try {
      const review = await request(`/api/reviews/${reviewId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: decision, note: text }),
      });
      note.value = review.note;
      status.textContent = review.status === 'approved' ? 'Approved' : 'Changes requested';
    } catch (error) {
      status.dataset.error = 'true';
      status.textContent = `Could not save review: ${error.message}`;
    } finally {
      approve.disabled = requestChanges.disabled = note.disabled = false;
    }
  }

  approve.addEventListener('click', () => save('approved'));
  requestChanges.addEventListener('click', () => save('changes_requested'));
  panel.append(resultNode('h4', null, 'Review result'), label, actions, status);
  output.append(panel);
}

function renderAnalysis(result, filename) {
  const title = resultNode('h3', 'result-title', result.title || filename);
  const summary = resultNode('section');
  summary.append(
    resultNode('h4', null, 'Summary'),
    resultNode('p', 'result-text', result.summary || 'No summary returned.')
  );
  const topics = resultNode('section');
  topics.append(resultNode('h4', null, 'Key topics'));
  if (result.keywords.length) {
    const tags = resultNode('ul', 'result-tags');
    for (const keyword of result.keywords) tags.append(resultNode('li', null, keyword));
    topics.append(tags);
  } else {
    topics.append(resultNode('p', 'result-text', 'No topics returned.'));
  }
  const coverage = /\.pdf$/i.test(filename) ? ['PDF pages', result.page_count] : ['Format', 'Unpaginated'];
  $('analyze-result').replaceChildren(
    title, resultMeta([['Language', result.language || 'Unknown'], coverage]), summary, topics
  );
}

function renderComparison(result, referenceName, actualName) {
  const files = resultNode('div', 'compare-files');
  files.append(
    resultNode('span', null, `Reference: ${referenceName}`),
    resultNode('span', null, `Second document: ${actualName}`)
  );
  const output = $('compare-result');
  if (result.comparison_kind === 'sections' && result.overview) {
    const { overview, differences, similarities } = result.overview;
    output.replaceChildren(
      resultNode('h3', 'result-title', 'Document comparison'),
      files,
      resultMeta([['Reference sections', result.reference_sections.length], ['Second document sections', result.actual_sections.length]]),
      resultNode('p', 'result-text', overview),
      resultList('Key differences', differences),
      resultList('Common ground', similarities),
      resultList('Reference section summaries', result.reference_sections, true, true),
      resultList('Second document section summaries', result.actual_sections, true, true)
    );
    return;
  }

  const byPage = result.comparison_kind === 'pages';
  const changed = result.changes.filter(item => item.changed);
  const title = changed.length ? 'Differences found' : 'No text differences found';
  const children = [resultNode('h3', 'result-title', title), files];
  if (byPage) children.push(resultMeta([
    ['Reference pages', result.reference_pages],
    ['Second document pages', result.actual_pages],
    ['Pages with differences', changed.length],
  ]));
  for (const item of changed) {
    const section = resultNode('section', 'result-section');
    let heading = 'Key differences';
    if (byPage) {
      const { reference_page: reference, actual_page: actual } = item;
      heading = `Page ${item.page}`;
      if (reference != null && actual != null && reference !== actual) {
        heading = `Reference page ${reference} → second document page ${actual}`;
      } else if (reference == null && actual != null) {
        heading = `Second document page ${actual} added`;
      } else if (reference != null && actual == null) {
        heading = `Reference page ${reference} removed`;
      }
    }
    section.append(
      resultNode('h4', null, heading),
      resultNode('p', 'result-text', item.summary)
    );
    children.push(section);
  }
  output.replaceChildren(...children);
}

const instructionEditors = ['analysis', 'compare'].map(workflow => {
  const id = `${workflow}-instructions`;
  const editor = { workflow, id, input: $(id), edited: false };
  editor.input.addEventListener('input', () => { editor.edited = true; });
  $(`${id}-save`).addEventListener('click', async () => {
    editor.edited = true;
    const instructions = editor.input.value.trim();
    const button = $(`${id}-save`);
    button.disabled = true;
    instructionStatus(editor, 'Saving default…');
    try {
      await request(`/api/instructions/${workflow}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instructions }),
      });
      instructionStatus(editor, instructions ? 'Default saved.' : 'Default cleared.');
    } catch (error) {
      instructionStatus(editor, `Could not save default: ${error.message}`, true);
    } finally {
      button.disabled = false;
    }
  });
  return editor;
});

function instructionStatus(editor, message, error = false) {
  $(`${editor.id}-status`).dataset.error = String(error);
  show(`${editor.id}-status`, message);
}

async function loadInstructionDefaults() {
  try {
    const defaults = await request('/api/instructions');
    for (const editor of instructionEditors) {
      const saved = defaults[editor.workflow];
      if (!editor.edited && saved) {
        editor.input.value = saved;
        $(`${editor.id}-panel`).open = true;
      }
    }
  } catch (error) {
    for (const editor of instructionEditors) {
      if (!editor.edited) instructionStatus(editor, `Could not load your saved default: ${error.message}`, true);
    }
  }
}

const instructionDefaultsReady = loadInstructionDefaults();

$('analyze-btn').addEventListener('click', async () => {
  const file = $('analyze-file').files[0]; if (!file) return show('analyze-result', 'Choose a document.');
  processing('analyze-btn', true);
  show('analyze-result', 'Generating your summary. Large files can take several minutes.');
  try {
    await instructionDefaultsReady;
    const body = new FormData();
    body.append('file', file);
    body.append('custom_instructions', $('analysis-instructions').value.trim());
    const { data, headers } = await request('/api/analyze', { method: 'POST', body }, { includeHeaders: true });
    renderAnalysis(data, file.name);
    addResultReview($('analyze-result'), headers.get('X-Review-ID'));
  }
  catch(e) { show('analyze-result', `Error: ${e.message}`); }
  finally { processing('analyze-btn', false); }
});
$('compare-btn').addEventListener('click', async () => {
  const a = $('reference-file').files[0], b = $('updated-file').files[0];
  if (!a || !b) return show('compare-result', 'Choose both documents.');
  processing('compare-btn', true);
  show('compare-result', 'Comparing documents. Large files can take several minutes.');
  try {
    await instructionDefaultsReady;
    const body = new FormData();
    body.append('reference', a);
    body.append('actual', b);
    body.append('custom_instructions', $('compare-instructions').value.trim());
    const { data, headers } = await request('/api/compare', { method: 'POST', body }, { includeHeaders: true });
    renderComparison(data, a.name, b.name);
    addResultReview($('compare-result'), headers.get('X-Review-ID'));
  }
  catch(e) { show('compare-result', `Error: ${e.message}`); }
  finally { processing('compare-btn', false); }
});
