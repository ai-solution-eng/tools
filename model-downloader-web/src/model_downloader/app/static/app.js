const form = document.getElementById('submit-form');
const msg = document.getElementById('msg');
const storageSelect = form.querySelector('select[name="storage"]');
const s3PathField = document.getElementById('s3-path-field');
const cacheRootField = document.getElementById('cache-root-field');
const chatTemplateField = document.getElementById('chat-template-field');
const chatTemplatePathField = document.getElementById('chat-template-path-field');
const chatTemplateContentsField = document.getElementById('chat-template-contents-field');
const chatTemplateCheckbox = chatTemplateField.querySelector('input[type="checkbox"]');
const templateSelectField = document.getElementById('template-select-field');
const templateSelect = document.getElementById('template-select');

function syncTemplateFields() {
  const s3Selected = storageSelect ? storageSelect.value === 's3' : false;
  chatTemplateField.hidden = s3Selected;
  templateSelectField.hidden = s3Selected;
  const enabled = !s3Selected && chatTemplateCheckbox.checked;
  chatTemplatePathField.hidden = !enabled;
  chatTemplateContentsField.hidden = !enabled;
}
function syncStorageFields() {
  if (storageSelect) s3PathField.hidden = storageSelect.value !== 's3';
  if (cacheRootField) cacheRootField.hidden = storageSelect ? storageSelect.value === 's3' : false;
  syncTemplateFields();
}
if (storageSelect) {
  storageSelect.addEventListener('change', syncStorageFields);
  syncStorageFields();
}
chatTemplateCheckbox.addEventListener('change', syncTemplateFields);

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(form));
  if (!body.namespace) body.namespace = form.dataset.defaultNs || '';
  if (!body.namespace) {
    msg.className = 'msg error';
    msg.textContent = 'Namespace is required.';
    return;
  }
  if (body.storage === 's3' && !/^s3:\/\/[^/\s]+/.test(body.s3_path || '')) {
    msg.className = 'msg error';
    msg.textContent = 'S3 destination must look like s3://bucket/prefix/';
    return;
  }
  if (body.storage === 's3') {
    body.chat_template_enabled = '';
    body.chat_template_path = '';
    body.chat_template_contents = '';
    body.cache_root = '';
  } else if (body.chat_template_enabled === 'on') {
    if (!body.chat_template_path || !body.chat_template_contents) {
      msg.className = 'msg error';
      msg.textContent = 'Chat template path and contents are both required.';
      return;
    }
  } else {
    body.chat_template_path = '';
    body.chat_template_contents = '';
  }
  msg.className = 'msg';
  msg.textContent = 'Submitting...';
  try {
    const r = await fetch('/api/jobs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const text = await r.text();
    const data = parseJsonSafe(text, null);
    if (!r.ok || !data) {
      msg.className = 'msg error';
      msg.textContent = 'Error: ' + (await apiErrorMessage(r, text));
      return;
    }
    msg.className = 'msg ok';
    msg.textContent = 'Submitted as ' + data.id + ' (' + data.status + ')';
    form.reset();
    syncStorageFields();
    refresh();
  } catch (e) {
    msg.className = 'msg error';
    msg.textContent = 'Error: ' + e;
  }
});

// ---- Model name search (known models + templates from the catalog) ----

let knownModels = [];
let templates = [];

function extractModelId(e) {
  const uri = e.uri || '';
  const pvc = uri.match(/^pvc:\/\/[^/?]+\/[^/]+\/([^?]+)/);
  if (pvc) return pvc[1].replace(/\/+$/, '');
  const s3 = uri.match(/^s3:\/\/[^/?]+\/(.+)$/);
  if (s3) return s3[1].replace(/\/+$/, '');
  const arg = (e.arguments || []).find(a => /^[A-Za-z0-9_.\-]+\/[A-Za-z0-9_.\-]+$/.test(a));
  return arg || '';
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

async function loadKnownData() {
  try {
    const r = await fetch('/api/catalog');
    const data = await r.json();
    const byId = new Map();
    const templatesSeen = new Map();
    for (const entries of Object.values(data.tiers || {})) {
      for (const e of entries) {
        const id = extractModelId(e);
        if (!id) continue;
        const existing = byId.get(id);
        if (!existing || (e.chat_template_path && !existing.chat_template_path)) {
          byId.set(id, e);
        }
        if (e.chat_template_path && e.chat_template_contents && !templatesSeen.has(e.chat_template_path)) {
          templatesSeen.set(e.chat_template_path, {
            path: e.chat_template_path,
            contents: e.chat_template_contents,
            label: e.name,
          });
        }
      }
    }
    knownModels = [...byId.values()];
    templates = [...templatesSeen.values()];
    renderTemplateSelect();
    applyUrlPreset();
  } catch (e) {
    console.error('catalog load failed', e);
  }
}

function renderTemplateSelect() {
  templateSelect.innerHTML = '<option value="">None (default)</option>';
  for (const t of templates) {
    const opt = document.createElement('option');
    opt.value = t.path;
    opt.textContent = t.label + ' — ' + t.path;
    templateSelect.appendChild(opt);
  }
}

function applyTemplate(path) {
  const t = templates.find(x => x.path === path);
  if (t) {
    templateSelect.value = t.path;
    chatTemplateCheckbox.checked = true;
    form.querySelector('input[name="chat_template_path"]').value = t.path;
    form.querySelector('textarea[name="chat_template_contents"]').value = t.contents;
  } else {
    templateSelect.value = '';
    chatTemplateCheckbox.checked = false;
  }
  syncTemplateFields();
}

templateSelect.addEventListener('change', () => applyTemplate(templateSelect.value));

// ---- Model name combobox ----

const modelInput = document.getElementById('model-input');
const modelToggle = document.getElementById('model-toggle');
const modelMenu = document.getElementById('model-menu');

function showModelMenu(q) {
  if (!knownModels.length) return;
  const ql = (q || '').toLowerCase();
  const matches = knownModels
    .filter(m => {
      const id = extractModelId(m).toLowerCase();
      const name = (m.name || '').toLowerCase();
      return id.includes(ql) || name.includes(ql);
    })
    .slice(0, 50);
  modelMenu.innerHTML = '';
  if (!matches.length) {
    const empty = document.createElement('div');
    empty.className = 'combo-empty';
    empty.textContent = 'No known models match — you can still type a model id manually.';
    modelMenu.appendChild(empty);
  } else {
    for (const m of matches) {
      const id = extractModelId(m);
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'combo-item';
      item.innerHTML = '<span class="combo-item-id">' + esc(id) + '</span>' +
        (m.name && m.name !== id ? '<span class="combo-item-name">' + esc(m.name) + '</span>' : '');
      item.addEventListener('click', () => pickModel(id));
      modelMenu.appendChild(item);
    }
  }
  modelMenu.hidden = false;
}

function pickModel(id) {
  modelInput.value = id;
  modelMenu.hidden = true;
  const m = knownModels.find(x => extractModelId(x) === id);
  if (m && m.chat_template_path) applyTemplate(m.chat_template_path);
  else applyTemplate('');
  msg.className = 'msg ok';
  msg.textContent = 'Model set to "' + id + '". Fill namespace/token and submit.';
}

modelToggle.addEventListener('click', () => {
  if (modelMenu.hidden) showModelMenu(modelInput.value);
  else modelMenu.hidden = true;
});
modelInput.addEventListener('input', () => showModelMenu(modelInput.value));
modelInput.addEventListener('focus', () => showModelMenu(modelInput.value));
modelInput.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') modelMenu.hidden = true;
  else if (e.key === 'Enter') modelMenu.hidden = true;
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.combo')) modelMenu.hidden = true;
});

function applyUrlPreset() {
  const preset = new URLSearchParams(window.location.search).get('model');
  if (!preset) return;
  const id = decodeURIComponent(preset);
  modelInput.value = id;
  const m = knownModels.find(x => extractModelId(x) === id);
  if (m && m.chat_template_path) applyTemplate(m.chat_template_path);
  msg.className = 'msg ok';
  msg.textContent = 'Model set to "' + id + '". Fill a namespace/token and submit.';
}

// ---- Jobs ----

async function refresh() {
  try {
    const r = await fetch('/api/jobs');
    const jobs = await r.json();
    const tbody = document.querySelector('#jobs tbody');
    tbody.innerHTML = '';
    for (const j of jobs) {
      tbody.appendChild(buildRow(j));
    }
  } catch (e) {
    console.error('refresh failed', e);
  }
}

function buildRow(j) {
  const tr = document.createElement('tr');
  tr.className = 'status-' + j.status;

  // Model
  const tdModel = document.createElement('td');
  tdModel.textContent = j.model_name;
  tr.appendChild(tdModel);

  // Namespace
  const tdNs = document.createElement('td');
  tdNs.textContent = j.namespace;
  tr.appendChild(tdNs);

  // Status
  const tdStatus = document.createElement('td');
  tdStatus.className = 'status-cell';
  tdStatus.textContent = j.status;
  tr.appendChild(tdStatus);

  // Output URL (PVC or S3) shown for succeeded jobs
  const tdOutput = document.createElement('td');
  const outUrl = j.pvc_url;
  if (j.status === 'succeeded' && outUrl) {
    const code = document.createElement('code');
    code.textContent = outUrl;
    code.className = 'pvc-url';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(outUrl).then(() => {
        btn.textContent = 'Copied!';
        setTimeout(() => (btn.textContent = 'Copy'), 1500);
      });
    });
    const wrap = document.createElement('div');
    wrap.className = 'pvc-wrap';
    wrap.appendChild(code);
    wrap.appendChild(btn);
    tdOutput.appendChild(wrap);
  }
  tr.appendChild(tdOutput);

  // Submitted
  const tdSub = document.createElement('td');
  tdSub.textContent = j.created_at ? new Date(j.created_at * 1000).toLocaleString() : '';
  tr.appendChild(tdSub);

  // Finished
  const tdFin = document.createElement('td');
  tdFin.textContent = j.finished_at ? new Date(j.finished_at * 1000).toLocaleString() : '';
  tr.appendChild(tdFin);

  // Logs button
  const tdLogs = document.createElement('td');
  const logBtn = document.createElement('button');
  logBtn.type = 'button';
  logBtn.className = 'log-btn';
  logBtn.textContent = 'View';
  logBtn.dataset.jobId = j.id;
  logBtn.addEventListener('click', () => showLogs(j.id, j.model_name));
  tdLogs.appendChild(logBtn);
  tr.appendChild(tdLogs);

  // Error
  const tdErr = document.createElement('td');
  tdErr.textContent = j.error || '';
  tr.appendChild(tdErr);

  // Actions
  const tdActions = document.createElement('td');
  if (j.status === 'failed' || j.status === 'succeeded') {
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'action-btn delete-btn';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', () => deleteJob(j.id));
    tdActions.appendChild(delBtn);
  }
  if (j.status === 'running') {
    const progBtn = document.createElement('button');
    progBtn.type = 'button';
    progBtn.className = 'action-btn progress-btn';
    progBtn.textContent = 'Progress';
    progBtn.addEventListener('click', () => showProgress(j.id, j.model_name));
    tdActions.appendChild(progBtn);
  }
  tr.appendChild(tdActions);

  return tr;
}

async function deleteJob(jobId) {
  if (!confirm('Delete this job record? (Downloaded model files on the PVC are kept.)')) return;
  try {
    const r = await fetch('/api/jobs/' + jobId, { method: 'DELETE' });
    if (!r.ok) {
      alert('Error: ' + (await apiErrorMessage(r)));
      return;
    }
    refresh();
  } catch (e) {
    alert('Error: ' + e);
  }
}

async function showProgress(jobId, modelName) {
  modalTitle.textContent = 'Progress: ' + modelName;
  modalBody.textContent = 'Fetching progress...';
  modal.hidden = false;
  try {
    const r = await fetch('/api/jobs/' + jobId + '/progress');
    const data = await r.json();
    if (data.error) {
      modalBody.textContent = 'Progress: ' + data.error;
    } else {
      modalBody.textContent = cleanLogs(data.logs);
    }
  } catch (e) {
    modalBody.textContent = 'Error fetching progress: ' + e;
  }
}

// ---- Logs modal ----
const modal = document.getElementById('log-modal');
const modalTitle = document.getElementById('log-title');
const modalBody = document.getElementById('log-body');
const modalClose = document.querySelector('.modal-close');
const modalBackdrop = document.querySelector('.modal-backdrop');

function cleanLogs(text) {
  let s = String(text);
  // Strip ANSI escape sequences (CSI + OSC + simple two-char codes)
  s = s.replace(/\x1b\][^\x07]*(\x07|\x1b\\)/g, '');
  s = s.replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, '');
  s = s.replace(/\x1b[0-9A-Za-z]/g, '');
  // Normalize CRLF / lone CR progress-overwrites to plain line breaks
  s = s.replace(/\r\n?/g, '\n');
  return s;
}

async function showLogs(jobId, modelName) {
  modalTitle.textContent = 'Logs: ' + modelName;
  modalBody.textContent = 'Loading...';
  modal.hidden = false;
  try {
    const r = await fetch('/api/jobs/' + jobId + '/logs');
    const data = await r.json();
    modalBody.textContent = cleanLogs(data.logs);
  } catch (e) {
    modalBody.textContent = 'Error fetching logs: ' + e;
  }
}

function closeModal() {
  modal.hidden = true;
}
modalClose.addEventListener('click', closeModal);
modalBackdrop.addEventListener('click', closeModal);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !modal.hidden) closeModal();
});

function parseJsonSafe(text, fallback) {
  try { return JSON.parse(text); } catch { return fallback; }
}

// Extract a readable message from any API response. Never assume the body is
// JSON: an unhandled server error used to come back as text/plain "Internal
// Server Error", and an expired session as a redirect to the login page —
// both made r.json() throw an opaque SyntaxError and hid the real problem.
// Pass `text` if the response body was already read; a fetch body can only
// be consumed once.
async function apiErrorMessage(r, text) {
  let body = text;
  if (body === undefined) {
    try { body = await r.text(); } catch (e) { body = ''; }
  }
  const contentType = r.headers.get('content-type') || '';
  if (contentType.includes('text/html')) {
    return 'HTTP ' + r.status + ' — got an HTML page instead of JSON. ' +
      'Your session may have expired; reload the page and try again.';
  }
  const data = parseJsonSafe(body, null);
  if (data && data.detail) {
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((x) => (x.msg ? (x.loc ? x.loc.join('.') + ': ' : '') + x.msg : JSON.stringify(x))).join('; ');
    }
    return JSON.stringify(data.detail);
  }
  const snippet = String(body || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 200);
  return 'HTTP ' + r.status + (r.statusText ? ' ' + r.statusText : '') + (snippet ? ' — ' + snippet : '');
}

// ---- Downloaded models (models physically present on PVC / S3) ----

const downloadedTableBody = document.querySelector('#downloaded-models tbody');
const downloadedRefreshBtn = document.getElementById('downloaded-refresh');
const downloadedMsg = document.getElementById('downloaded-msg');

function setDownloadedMsg(text, isError) {
  if (!downloadedMsg) return;
  downloadedMsg.textContent = text;
  downloadedMsg.className = isError ? 'msg error' : 'msg ok';
}

function buildDownloadedRow(m) {
  const tr = document.createElement('tr');
  tr.className = 'status-succeeded';

  const tdModel = document.createElement('td');
  tdModel.className = 'cat-name';
  tdModel.textContent = m.model_name || '';
  tr.appendChild(tdModel);

  const tdBackends = document.createElement('td');
  (m.backends || []).forEach(function (b) {
    const badge = document.createElement('span');
    badge.className = 'backend-badge backend-' + b;
    badge.textContent = b === 's3' ? 'S3' : 'PVC';
    tdBackends.appendChild(badge);
  });
  tr.appendChild(tdBackends);

  const tdLoc = document.createElement('td');
  const locCode = document.createElement('code');
  locCode.className = 'pvc-url';
  locCode.textContent = m.location || '';
  tdLoc.appendChild(locCode);
  tr.appendChild(tdLoc);

  const tdMod = document.createElement('td');
  tdMod.textContent = m.last_modified ? new Date(m.last_modified * 1000).toLocaleString() : '';
  tr.appendChild(tdMod);

  return tr;
}

async function refreshDownloaded(force = false) {
  if (!downloadedTableBody) return;
  if (force) setDownloadedMsg('Rescanning storage (this spawns a short-lived scan Job)...', false);
  try {
    const url = force ? '/api/downloaded?force=1' : '/api/downloaded';
    const r = await fetch(url);
    if (!r.ok) {
      setDownloadedMsg('Scan request failed: HTTP ' + r.status, true);
      return;
    }
    const data = await r.json();
    const models = data.models || [];
    downloadedTableBody.innerHTML = '';
    if (!models.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 4;
      td.className = 'tier-empty';
      td.textContent = 'No downloaded models found on storage yet.';
      tr.appendChild(td);
      downloadedTableBody.appendChild(tr);
    } else {
      for (const m of models) {
        downloadedTableBody.appendChild(buildDownloadedRow(m));
      }
    }
    // The visible line reads the clean scanning-status sentence served by the
    // API ("s3 automatic scanning enabled." / "pvc automatic scanning enabled
    // at <N> second cadence." / "No automatic scanning enabled."). The per-
    // source diagnostics (ok/cached/error per backend, job count) stay in the
    // console — useful for explaining an empty table, but not page copy.
    if (data.scan) console.debug('downloaded models scan detail:', data.scan);
    setDownloadedMsg(data.scan_status || 'No automatic scanning enabled.', false);
  } catch (e) {
    setDownloadedMsg('Scan request error: ' + e, true);
    console.error('downloaded models refresh failed', e);
  }
}

if (downloadedTableBody) {
  refreshDownloaded();
  setInterval(refreshDownloaded, 10000);
  if (downloadedRefreshBtn) {
    downloadedRefreshBtn.addEventListener('click', () => refreshDownloaded(true));
  }
}

// ---- Debug pods ----

const debugForm = document.getElementById('debug-form');
const debugMsg = document.getElementById('debug-msg');
const debugTableBody = document.querySelector('#debug-pods tbody');

function podStatusClass(phase) {
  if (phase === 'Running') return 'status-running';
  if (phase === 'Pending') return 'status-queued';
  if (phase === 'Succeeded') return 'status-succeeded';
  return 'status-failed';
}

function buildDebugRow(p) {
  const tr = document.createElement('tr');
  tr.className = podStatusClass(p.phase || '');

  const tdName = document.createElement('td');
  const code = document.createElement('code');
  code.textContent = p.name;
  tdName.appendChild(code);
  tr.appendChild(tdName);

  const tdNs = document.createElement('td');
  tdNs.textContent = p.namespace;
  tr.appendChild(tdNs);

  const tdStatus = document.createElement('td');
  tdStatus.className = 'status-cell';
  tdStatus.textContent = p.phase || 'unknown';
  tr.appendChild(tdStatus);

  const tdNode = document.createElement('td');
  tdNode.textContent = p.node || '';
  tr.appendChild(tdNode);

  const tdCreated = document.createElement('td');
  tdCreated.textContent = p.created_at ? new Date(p.created_at * 1000).toLocaleString() : '';
  tr.appendChild(tdCreated);

  const tdActions = document.createElement('td');
  const delBtn = document.createElement('button');
  delBtn.type = 'button';
  delBtn.className = 'action-btn delete-btn';
  delBtn.textContent = 'Delete';
  // Deleting goes through the owning Job (the pod itself is created by the
  // job-controller); fall back to the name for anything job-less.
  delBtn.addEventListener('click', () => deleteDebugPod(p.namespace, p.job_name || p.name));
  tdActions.appendChild(delBtn);
  tr.appendChild(tdActions);

  return tr;
}

async function refreshDebugPods() {
  if (!debugTableBody) return;
  try {
    const r = await fetch('/api/debug-pods');
    if (!r.ok) return;
    const pods = await r.json();
    debugTableBody.innerHTML = '';
    for (const p of pods) {
      debugTableBody.appendChild(buildDebugRow(p));
    }
  } catch (e) {
    console.error('debug pod refresh failed', e);
  }
}

async function deleteDebugPod(namespace, jobName) {
  if (!confirm('Delete debug job ' + jobName + ' (and its pod + HF-token secret)?')) return;
  try {
    const r = await fetch('/api/debug-pods/' + encodeURIComponent(namespace) + '/' + encodeURIComponent(jobName), {
      method: 'DELETE',
    });
    if (!r.ok) {
      alert('Error: ' + (await apiErrorMessage(r)));
      return;
    }
    refreshDebugPods();
  } catch (e) {
    alert('Error: ' + e);
  }
}

if (debugForm) {
  debugForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(debugForm));
    if (!body.namespace) body.namespace = debugForm.dataset.defaultNs || '';
    if (!body.namespace) {
      debugMsg.className = 'msg error';
      debugMsg.textContent = 'Namespace is required.';
      return;
    }
    debugMsg.className = 'msg';
    debugMsg.textContent = 'Launching debug pod...';
    try {
      const r = await fetch('/api/debug-pods', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      const text = await r.text();
      const data = parseJsonSafe(text, null);
      if (!r.ok || !data) {
        debugMsg.className = 'msg error';
        debugMsg.textContent = 'Error: ' + (await apiErrorMessage(r, text));
        return;
      }
      debugMsg.className = 'msg ok';
      debugMsg.textContent = 'Launched ' + data.name + ' — ' + (data.kubectl || '');
      debugForm.reset();
      refreshDebugPods();
    } catch (e) {
      debugMsg.className = 'msg error';
      debugMsg.textContent = 'Error: ' + e;
    }
  });
  refreshDebugPods();
  setInterval(refreshDebugPods, 3000);
}

loadKnownData();
refresh();
setInterval(refresh, 3000);