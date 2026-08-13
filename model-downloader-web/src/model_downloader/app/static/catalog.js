// ---- Model catalog ----
const catalogTiers = document.getElementById('catalog-tiers');
const catalogMsg = document.getElementById('catalog-msg');
const pushResults = document.getElementById('push-results');
let catalogData = { tiers: {}, tier_labels: {} };

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
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

function buildCatalogRow(e) {
  const tr = document.createElement('tr');
  const modelId = extractModelId(e);
  tr.innerHTML =
    '<td><input type="checkbox" data-catalog-id="' + e.catalog_id + '" data-tier="' + e.tier + '"></td>' +
    '<td class="cat-name"><button type="button" class="cat-details-btn" data-details="' + e.catalog_id + '">' + esc(e.name) + '</button>' +
      (modelId ? '<div class="cat-model-id">' + esc(modelId) + '</div>' : '') + '</td>' +
    '<td>' + e.version + '</td>' +
    '<td class="cat-image">' + esc(e.image || '') + '</td>' +
    '<td>' + esc(e.model_format || '') + '</td>' +
    '<td>' + esc(e.resource_limit_gpu || '') + '</td>' +
    '<td>' + (e.caching_enabled ? '✓' : '') + '</td>' +
    '<td>' +
      (modelId ? '<button type="button" class="action-btn download-btn" data-dl="' + e.catalog_id + '">Download</button> ' : '') +
      '<button type="button" class="action-btn delete-btn" data-del="' + e.catalog_id + '">Delete</button>' +
    '</td>';
  tr.querySelector('[data-details]').addEventListener('click', () => showDetails(e));
  const dlBtn = tr.querySelector('[data-dl]');
  if (dlBtn) dlBtn.addEventListener('click', () => {
    window.location.href = '/?model=' + encodeURIComponent(modelId);
  });
  tr.querySelector('[data-del]').addEventListener('click', () => deleteCatalogEntry(e.catalog_id, e.name));
  return tr;
}

// ---- Model details modal (full config that will be included) ----

const detailsModal = document.getElementById('details-modal');
const detailsTitle = document.getElementById('details-title');
const detailsBody = document.getElementById('details-body');

function showDetails(e) {
  detailsTitle.textContent = e.name + ' (v' + e.version + ')';
  const copy = {};
  for (const [k, v] of Object.entries(e)) {
    if (k === 'catalog_id' || k === 'tier') continue;
    if (k === 'chat_template_contents') {
      copy.chat_template_contents = '(template body — use the "Chat template preset" dropdown on the Download page)';
      continue;
    }
    copy[k] = v;
  }
  detailsBody.textContent = JSON.stringify(copy, null, 2);
  detailsModal.hidden = false;
}

function closeDetails() {
  detailsModal.hidden = true;
}
detailsModal.querySelector('.modal-close').addEventListener('click', closeDetails);
detailsModal.querySelector('.modal-backdrop').addEventListener('click', closeDetails);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !detailsModal.hidden) closeDetails();
});

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