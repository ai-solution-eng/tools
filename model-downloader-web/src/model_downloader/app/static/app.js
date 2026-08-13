const form = document.getElementById('submit-form');
const msg = document.getElementById('msg');
const storageSelect = form.querySelector('select[name="storage"]');
const s3PathField = document.getElementById('s3-path-field');
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
    const data = await r.json();
    if (!r.ok) {
      msg.className = 'msg error';
      msg.textContent = 'Error: ' + (data.detail || r.statusText);
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
      const data = parseJsonSafe(await r.text(), {});
      alert('Error: ' + (data.detail || r.statusText));
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

loadKnownData();
refresh();
setInterval(refresh, 3000);