const form = document.getElementById('submit-form');
const msg = document.getElementById('msg');
const storageSelect = form.querySelector('select[name="storage"]');
const s3PathField = document.getElementById('s3-path-field');
const chatTemplateField = document.getElementById('chat-template-field');
const chatTemplatePathField = document.getElementById('chat-template-path-field');
const chatTemplateContentsField = document.getElementById('chat-template-contents-field');
const chatTemplateCheckbox = chatTemplateField.querySelector('input[type="checkbox"]');

function syncTemplateFields() {
  const s3Selected = storageSelect ? storageSelect.value === 's3' : false;
  chatTemplateField.hidden = s3Selected;
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
      modalBody.textContent = data.logs || '(no output)';
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

async function showLogs(jobId, modelName) {
  modalTitle.textContent = 'Logs: ' + modelName;
  modalBody.textContent = 'Loading...';
  modal.hidden = false;
  try {
    const r = await fetch('/api/jobs/' + jobId + '/logs');
    const data = await r.json();
    modalBody.textContent = data.logs;
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

// ---- Model catalog ----
const catalogTiers = document.getElementById('catalog-tiers');
const catalogMsg = document.getElementById('catalog-msg');
const pushResults = document.getElementById('push-results');
let catalogData = { tiers: {}, tier_labels: {} };

async function loadCatalog() {
  try {
    const r = await fetch('/api/catalog');
    catalogData = await r.json();
    renderCatalog();
  } catch (e) {
    console.error('catalog load failed', e);
  }
}

function renderCatalog() {
  catalogTiers.innerHTML = '';
  for (const [tier, entries] of Object.entries(catalogData.tiers || {})) {
    const label = (catalogData.tier_labels || {})[tier] || tier;
    const section = document.createElement('div');
    section.className = 'tier-section';

    const header = document.createElement('div');
    header.className = 'tier-header';
    header.innerHTML = '<label class="tier-select-all"><input type="checkbox" data-tier="' + tier + '"><strong>' + label + '</strong> (' + entries.length + ')</label>';
    section.appendChild(header);

    if (entries.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'tier-empty';
      empty.textContent = 'No models. Click "Add Model" to create one.';
      section.appendChild(empty);
    } else {
      const table = document.createElement('table');
      table.className = 'catalog-table';
      table.innerHTML = '<thead><tr><th></th><th>Name</th><th>Ver</th><th>Image</th><th>Format</th><th>GPU</th><th>Cache</th><th></th></tr></thead>';
      const tbody = document.createElement('tbody');
      for (const e of entries) {
        tbody.appendChild(buildCatalogRow(e));
      }
      table.appendChild(tbody);
      section.appendChild(table);
    }
    catalogTiers.appendChild(section);
  }

  // Wire select-all checkboxes
  catalogTiers.querySelectorAll('.tier-select-all input').forEach(cb => {
    cb.addEventListener('change', () => {
      const tier = cb.dataset.tier;
      catalogTiers.querySelectorAll('input[data-catalog-id]').forEach(box => {
        if (box.dataset.tier === tier) box.checked = cb.checked;
      });
    });
  });
}

function extractModelId(e) {
  const uri = e.uri || '';
  const pvc = uri.match(/^pvc:\/\/[^/?]+\/[^/]+\/([^?]+)/);
  if (pvc) return pvc[1].replace(/\/+$/, '');
  const s3 = uri.match(/^s3:\/\/[^/?]+\/(.+)$/);
  if (s3) return s3[1].replace(/\/+$/, '');
  const arg = (e.arguments || []).find(a => /^[A-Za-z0-9_.\-]+\/[A-Za-z0-9_.\-]+$/.test(a));
  return arg || '';
}

function buildCatalogRow(e) {
  const tr = document.createElement('tr');
  const modelId = extractModelId(e);
  const hasTemplate = !!(e.chat_template_path && e.chat_template_contents);
  tr.innerHTML =
    '<td><input type="checkbox" data-catalog-id="' + e.catalog_id + '" data-tier="' + e.tier + '"></td>' +
    '<td class="cat-name">' + esc(e.name) +
      (modelId ? '<div class="cat-model-id">' + esc(modelId) + '</div>' : '') + '</td>' +
    '<td>' + e.version + '</td>' +
    '<td class="cat-image">' + esc(e.image || '') + '</td>' +
    '<td>' + esc(e.model_format || '') + '</td>' +
    '<td>' + esc(e.resource_limit_gpu || '') + '</td>' +
    '<td>' + (e.caching_enabled ? '✓' : '') + '</td>' +
    '<td>' +
      (modelId ? '<button type="button" class="action-btn download-btn" data-dl="' + e.catalog_id + '">Download</button> ' : '') +
      (hasTemplate ? '<button type="button" class="action-btn template-btn" data-tpl="' + e.catalog_id + '">Template</button> ' : '') +
      '<button type="button" class="action-btn delete-btn" data-del="' + e.catalog_id + '">Delete</button>' +
    '</td>';
  const dlBtn = tr.querySelector('[data-dl]');
  if (dlBtn) dlBtn.addEventListener('click', () => useInDownloader(modelId));
  const tplBtn = tr.querySelector('[data-tpl]');
  if (tplBtn) tplBtn.addEventListener('click', () => useInDownloader(modelId, e));
  tr.querySelector('[data-del]').addEventListener('click', () => deleteCatalogEntry(e.catalog_id, e.name));
  return tr;
}

function useInDownloader(modelId, entry) {
  const modelInput = form.querySelector('input[name="model_name"]');
  modelInput.value = modelId;
  if (entry && entry.chat_template_path && entry.chat_template_contents) {
    chatTemplateCheckbox.checked = true;
    form.querySelector('input[name="chat_template_path"]').value = entry.chat_template_path;
    form.querySelector('textarea[name="chat_template_contents"]').value = entry.chat_template_contents;
  }
  syncTemplateFields();
  modelInput.focus();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  msg.className = 'msg ok';
  msg.textContent = 'Model set to "' + modelId + '" in the downloader form — fill namespace/token and submit.';
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

async function deleteCatalogEntry(id, name) {
  if (!confirm('Delete "' + name + '" from the catalog?')) return;
  try {
    const r = await fetch('/api/catalog/' + id, { method: 'DELETE' });
    if (!r.ok) { alert('Error: ' + (await r.json()).detail); return; }
    loadCatalog();
  } catch (e) { alert('Error: ' + e); }
}

// ---- Add model form ----
const addModal = document.getElementById('add-modal');
const addBtn = document.getElementById('add-model-btn');
const addForm = document.getElementById('add-model-form');

addBtn.addEventListener('click', () => { addModal.hidden = false; });
addModal.querySelector('.modal-close').addEventListener('click', () => { addModal.hidden = true; });
addModal.querySelector('.modal-backdrop').addEventListener('click', () => { addModal.hidden = true; });

addForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(addForm);
  const entry = {
    tier: fd.get('tier'),
    name: fd.get('name'),
    version: parseInt(fd.get('version') || '1', 10),
    description: fd.get('description') || '',
    uri: fd.get('uri') || '',
    image: fd.get('image'),
    model_format: fd.get('model_format') || 'custom',
    arguments: (fd.get('arguments') || '').split('\n').map(s => s.trim()).filter(Boolean),
    environment: parseKv(fd.get('environment') || ''),
    resource_request_cpu: fd.get('resource_request_cpu') || '',
    resource_request_memory: fd.get('resource_request_memory') || '',
    resource_request_gpu: fd.get('resource_request_gpu') || '',
    resource_limit_cpu: fd.get('resource_limit_cpu') || '',
    resource_limit_memory: fd.get('resource_limit_memory') || '',
    resource_limit_gpu: fd.get('resource_limit_gpu') || '',
    resource_gpu_type: fd.get('resource_gpu_type') || '',
    caching_enabled: fd.get('caching_enabled') === 'on',
    project: '',
    chat_template_path: fd.get('chat_template_path') || '',
    chat_template_contents: fd.get('chat_template_contents') || '',
    metadata: parseJsonSafe(fd.get('metadata'), { tags: '', modelCategory: 'other' }),
    registry: null,
  };
  try {
    const r = await fetch('/api/catalog', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(entry),
    });
    if (!r.ok) { alert('Error: ' + (await r.json()).detail); return; }
    addModal.hidden = true;
    addForm.reset();
    loadCatalog();
  } catch (e) { alert('Error: ' + e); }
});

function parseKv(text) {
  const obj = {};
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf('=');
    if (idx === -1) continue;
    obj[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
  }
  return obj;
}

function parseJsonSafe(text, fallback) {
  try { return JSON.parse(text); } catch { return fallback; }
}

// ---- Push to MLIS ----
document.getElementById('push-selected-btn').addEventListener('click', async () => {
  const checked = [...catalogTiers.querySelectorAll('input[data-catalog-id]:checked')];
  if (checked.length === 0) { alert('Select at least one model to push.'); return; }
  const ids = new Set(checked.map(c => c.dataset.catalogId));
  const configs = [];
  for (const entries of Object.values(catalogData.tiers || {})) {
    for (const e of entries) {
      if (ids.has(e.catalog_id)) {
        const { catalog_id, tier, ...cfg } = e;
        configs.push(cfg);
      }
    }
  }
  await doPush(configs);
});

document.getElementById('push-direct-btn').addEventListener('click', async () => {
  const text = document.getElementById('direct-json').value.trim();
  if (!text) { alert('Paste a JSON array first.'); return; }
  let configs;
  try {
    configs = JSON.parse(text);
    if (!Array.isArray(configs)) throw new Error('not an array');
  } catch (e) { alert('Invalid JSON: ' + e.message); return; }
  await doPush(configs);
});

async function doPush(configs) {
  pushResults.innerHTML = '<p class="hint">Pushing ' + configs.length + ' model(s) to MLIS...</p>';
  try {
    const r = await fetch('/api/push', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(configs),
    });
    const data = await r.json();
    renderPushResults(data);
  } catch (e) {
    pushResults.innerHTML = '<p class="msg error">Error: ' + e + '</p>';
  }
}

function renderPushResults(data) {
  const { results, pushed, skipped, errors } = data;
  let html = '<div class="push-summary">Pushed: <strong>' + pushed + '</strong>' +
    ' | Skipped: <strong>' + skipped + '</strong>' +
    ' | Errors: <strong>' + errors + '</strong></div>';
  html += '<table class="catalog-table"><thead><tr><th>Name</th><th>Version</th><th>Status</th><th>Detail</th></tr></thead><tbody>';
  for (const r of results) {
    const cls = r.status === 'pushed' ? 'push-ok' : r.status === 'skipped' ? 'push-skip' : 'push-err';
    html += '<tr class="' + cls + '"><td>' + esc(r.name) + '</td><td>' + r.version + '</td><td>' + r.status + '</td><td>' + esc(r.detail) + '</td></tr>';
  }
  html += '</tbody></table>';
  pushResults.innerHTML = html;
}

document.getElementById('refresh-catalog-btn').addEventListener('click', loadCatalog);

loadCatalog();
refresh();
setInterval(refresh, 3000);
