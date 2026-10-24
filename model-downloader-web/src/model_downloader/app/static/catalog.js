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

// ---- Hierarchical renderer: lab > model > configuration ----
//
// The catalog is stored flat (one entry per configuration) and grouped
// server-side into catalogData.tree.  The tree renders as nested <details>
// elements so expand/collapse needs no JS state and survives a re-render:
// open state is tracked in openKeys and restored afterwards, so refreshing
// the catalog does not collapse everything the user had opened.
const openKeys = new Set();
const searchState = { query: '', tier: '' };

// Configurations ticked for push, keyed by catalog_id.  This is the SOURCE OF
// TRUTH, not the DOM: filtering re-renders the tree and removes rows from the
// document, so reading checked boxes out of the DOM would silently drop any
// selection hidden by the current filter.  Keeping the set here means a
// selection survives filtering, searching, and a catalog refresh.
const selectedIds = new Set();

// Seed the filter from ?q= so a filtered view is a shareable link.  The
// Download page links here from its model picker; bookmarking a filtered
// catalog is a natural thing to want.
(function seedFiltersFromUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    const q = params.get('q');
    if (q) searchState.query = q;
    const gpu = params.get('gpu');
    if (gpu) searchState.tier = gpu;
  } catch (e) { /* no URL support: fall back to an unfiltered view */ }
})();

// Stable identity for a collapsible node, used to restore open state.
function nodeKey(parts) { return parts.join('\u001f'); }

function captureOpenState() {
  catalogTiers.querySelectorAll('details[data-node-key]').forEach(d => {
    if (d.open) openKeys.add(d.dataset.nodeKey);
    else openKeys.delete(d.dataset.nodeKey);
  });
}

// Match one configuration against the search box.  The blob carries every
// distinguishing dimension, so "nvfp4", "hicache", "rtx" or "sglang" filter
// down to the configurations that actually match.  Multiple terms are ANDed.
function configMatches(cfg, q) {
  // GPU-class filter first: it is an exact match on the configuration's tier,
  // not a text search, so "h200" must not also match a description mentioning
  // H200 on an L40S entry.
  if (searchState.tier) {
    const tier = String((cfg.entry || {}).tier || '');
    if (tier !== searchState.tier) return false;
  }
  if (!q) return true;
  const e = cfg.entry || {};
  const blob = [
    cfg.label, cfg.framework, cfg.quant, cfg.publisher, cfg.model_id,
    e.name, e.image, e.description, e.tier, e.model_format,
    (e.metadata && e.metadata.tags) || '',
  ].join(' ').toLowerCase();
  return q.split(/\s+/).filter(Boolean).every(term => blob.includes(term));
}

function renderCatalog() {
  captureOpenState();
  catalogTiers.innerHTML = '';
  const tree = catalogData.tree || [];
  const q = searchState.query.trim().toLowerCase();

  if (tree.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'tier-empty';
    empty.textContent = 'No models. Click "Add Model" to create one.';
    catalogTiers.appendChild(empty);
    syncSelectionToDom();
    updateSelectionCount();
    updateSearchSummary(q, 0, 0);
    return;
  }

  let shownConfigs = 0;
  let totalConfigs = 0;

  for (const lab of tree) {
    // Keep a model only when at least one of its configurations matches.
    const models = lab.models.map(m => ({
      ...m,
      configs: m.configs.filter(c => configMatches(c, q)),
    })).filter(m => m.configs.length > 0);
    totalConfigs += lab.config_count;
    if (models.length === 0) continue;

    const shown = models.reduce((n, m) => n + m.configs.length, 0);
    shownConfigs += shown;

    // A search always expands what it matched; otherwise restore prior state.
    const labKey = nodeKey(['lab', lab.name]);
    const labEl = document.createElement('details');
    labEl.className = 'tree-lab';
    labEl.dataset.nodeKey = labKey;
    labEl.open = q ? true : openKeys.has(labKey);

    const labSummary = document.createElement('summary');
    labSummary.className = 'tree-lab-summary';
    labSummary.innerHTML =
      '<span class="tree-lab-name">' + esc(lab.name) + '</span>' +
      '<span class="tree-count">' + models.length + ' model' + (models.length === 1 ? '' : 's') +
      ' \u00b7 ' + shown + ' config' + (shown === 1 ? '' : 's') + '</span>' +
      '<span class="tree-lab-actions">' +
        '<label class="tree-select" title="Select every configuration in this lab">' +
          '<input type="checkbox" data-select-lab="' + esc(lab.name) + '"> all</label>' +
      '</span>';
    labEl.appendChild(labSummary);

    const labBody = document.createElement('div');
    labBody.className = 'tree-lab-body';

    for (const model of models) {
      const mKey = nodeKey(['model', lab.name, model.key]);
      const modelEl = document.createElement('details');
      modelEl.className = 'tree-model';
      modelEl.dataset.nodeKey = mKey;
      modelEl.open = q ? true : openKeys.has(mKey);

      const modelSummary = document.createElement('summary');
      modelSummary.className = 'tree-model-summary';
      modelSummary.innerHTML =
        '<span class="tree-model-name">' + esc(model.name) + '</span>' +
        '<span class="tree-count">' + model.configs.length + ' config' +
          (model.configs.length === 1 ? '' : 's') + '</span>';
      modelEl.appendChild(modelSummary);

      const cfgWrap = document.createElement('div');
      cfgWrap.className = 'tree-configs';
      for (const cfg of model.configs) cfgWrap.appendChild(buildConfigRow(cfg));
      modelEl.appendChild(cfgWrap);
      labBody.appendChild(modelEl);
    }

    labEl.appendChild(labBody);
    catalogTiers.appendChild(labEl);
  }

  if (shownConfigs === 0 && q) {
    const none = document.createElement('p');
    none.className = 'tier-empty';
    none.textContent = 'No configurations match "' + searchState.query.trim() + '".';
    catalogTiers.appendChild(none);
  }

  wireTreeControls();
  syncSelectionToDom();
  updateSelectionCount();
  updateSearchSummary(q, shownConfigs, totalConfigs);
}

// Render a configuration label, emphasising the segments that deserve a second
// look (HiCache, shared GPU) without repeating them as separate chips -- the
// label already carries every dimension, and duplicating it is just noise.
function configLabelHtml(cfg) {
  const parts = String(cfg.label || '').split(' \u00b7 ');
  return parts.map(part => {
    if (part === 'HiCache') {
      return '<span class="cfg-emph cfg-emph-cache" title="Hierarchical cache enabled in the serving args">' + esc(part) + '</span>';
    }
    if (part === 'Shared') {
      return '<span class="cfg-emph cfg-emph-shared" title="Runs on a shared GPU">Shared GPU</span>';
    }
    return esc(part);
  }).join('<span class="cfg-sep"> \u00b7 </span>');
}

// One configuration leaf: select box, label, and the actions that were
// previously table-row buttons (details / download / delete).
function buildConfigRow(cfg) {
  const e = cfg.entry;
  const row = document.createElement('div');
  row.className = 'tree-config';

  row.innerHTML =
    '<label class="tree-config-select"><input type="checkbox" data-catalog-id="' + esc(e.catalog_id) +
      '" data-tier="' + esc(e.tier || '') + '"></label>' +
    '<span class="tree-config-label">' + configLabelHtml(cfg) + '</span>' +
    '<span class="tree-config-id">' + esc(cfg.model_id || '') + '</span>' +
    '<span class="tree-config-actions">' +
      '<button type="button" class="action-btn details-btn" data-details="' + esc(e.catalog_id) + '">Config</button>' +
      (cfg.model_id ? '<button type="button" class="action-btn download-btn" data-dl="' + esc(e.catalog_id) + '">Download</button>' : '') +
      '<button type="button" class="action-btn delete-btn" data-del="' + esc(e.catalog_id) + '">Delete</button>' +
    '</span>';

  row.querySelector('[data-details]').addEventListener('click', () => showDetails(e));
  const dlBtn = row.querySelector('[data-dl]');
  if (dlBtn) dlBtn.addEventListener('click', () => {
    window.location.href = '/?model=' + encodeURIComponent(cfg.model_id);
  });
  row.querySelector('[data-del]').addEventListener('click', () => deleteCatalogEntry(e.catalog_id, e.name));
  const box = row.querySelector('input[data-catalog-id]');
  box.addEventListener('change', updateSelectionCount);
  return row;
}

// Lab-level "all" checkbox + per-node open/close bookkeeping.
function wireTreeControls() {
  catalogTiers.querySelectorAll('[data-select-lab]').forEach(cb => {
    cb.addEventListener('change', () => {
      const labEl = cb.closest('details.tree-lab');
      if (!labEl) return;
      // Only the configs currently rendered: with a GPU-class or text filter
      // active, "all" means "all of what I can see", never an invisible
      // selection the user cannot review before pushing.
      labEl.querySelectorAll('input[data-catalog-id]').forEach(box => {
        box.checked = cb.checked;
        if (cb.checked) selectedIds.add(box.dataset.catalogId);
        else selectedIds.delete(box.dataset.catalogId);
      });
      cb.indeterminate = false;
      updateSelectionCount();
      syncSelectionToDom();
    });
  });
  catalogTiers.querySelectorAll('details[data-node-key]').forEach(d => {
    d.addEventListener('toggle', () => {
      if (d.open) openKeys.add(d.dataset.nodeKey);
      else openKeys.delete(d.dataset.nodeKey);
    });
  });
}

function updateSelectionCount() {
  const n = catalogTiers.querySelectorAll('input[data-catalog-id]:checked').length;
  const btn = document.getElementById('push-selected-btn');
  if (btn) btn.textContent = n ? ('Push ' + n + ' Selected to MLIS') : 'Push Selected to MLIS';
}

// Reflect the persistent selection onto the freshly rendered checkboxes, and
// flag a lab's "all" box when every one of its visible configs is ticked.
function syncSelectionToDom() {
  catalogTiers.querySelectorAll('input[data-catalog-id]').forEach(box => {
    box.checked = selectedIds.has(box.dataset.catalogId);
  });
  catalogTiers.querySelectorAll('[data-select-lab]').forEach(cb => {
    const labEl = cb.closest('details.tree-lab');
    if (!labEl) return;
    const boxes = [...labEl.querySelectorAll('input[data-catalog-id]')];
    cb.checked = boxes.length > 0 && boxes.every(b => b.checked);
    cb.indeterminate = !cb.checked && boxes.some(b => b.checked);
  });
}

function updateSearchSummary(q, shown, total) {
  const el = document.getElementById('catalog-search-summary');
  if (!el) return;
  const filtered = q || searchState.tier;
  if (!filtered) {
    el.textContent = total + ' configurations in ' + (catalogData.tree || []).length + ' labs';
    return;
  }
  const scope = searchState.tier
    ? ((catalogData.tier_labels || {})[searchState.tier] || searchState.tier)
    : 'all GPUs';
  el.textContent = shown + ' of ' + total + ' configurations (' + scope + ')';
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
  const ids = new Set([...catalogTiers.querySelectorAll('input[data-catalog-id]:checked')].map(c => c.dataset.catalogId));
  if (ids.size === 0) { alert('Select at least one model to push.'); return; }
  // Read from the tree (the single grouping source) so a push sends exactly
  // the configurations the user sees, in tree order.
  const configs = [];
  for (const lab of catalogData.tree || []) {
    for (const model of lab.models) {
      for (const cfg of model.configs) {
        const e = cfg.entry;
        if (!ids.has(e.catalog_id)) continue;
        const { catalog_id, tier, ...rest } = e;
        configs.push(rest);
      }
    }
  }
  await doPush(configs);
});

// Parse the direct-JSON textarea: accepts a single {MODEL_CONFIGURATION}
// object or a [{MC1}, {MC2}, ...] array.  Returns the normalized array or
// null (after alerting) on invalid input.
function parseDirectJson() {
  const text = document.getElementById('direct-json').value.trim();
  if (!text) { alert('Paste JSON first — a single object or an array.'); return null; }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) { alert('Invalid JSON: ' + e.message); return null; }
  if (typeof parsed !== 'object' || parsed === null) {
    alert('Invalid JSON: expected an object or an array.'); return null;
  }
  return Array.isArray(parsed) ? parsed : [parsed];
}

document.getElementById('push-direct-btn').addEventListener('click', async () => {
  const configs = parseDirectJson();
  if (!configs) return;
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

// "Add to Catalog": add the direct-JSON entries (single object or array) to
// the catalog so they appear in the tiers table above.  Duplicates (same
// catalog_id or name+version) are skipped server-side.
document.getElementById('add-direct-btn').addEventListener('click', async () => {
  const configs = parseDirectJson();
  if (!configs) return;
  pushResults.innerHTML = '<p class="hint">Adding ' + configs.length + ' entr' +
    (configs.length === 1 ? 'y' : 'ies') + ' to the catalog...</p>';
  try {
    const r = await fetch('/api/catalog/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(configs),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    let html = '<div class="push-summary">Added: <strong>' + data.added + '</strong>' +
      ' | Skipped: <strong>' + data.skipped + '</strong></div>';
    html += '<table class="catalog-table"><thead><tr><th>Name</th><th>Status</th><th>Detail</th></tr></thead><tbody>';
    for (const res of data.results) {
      const cls = res.status === 'added' ? 'push-ok' : 'push-skip';
      html += '<tr class="' + cls + '"><td>' + esc(res.name) + '</td><td>' + res.status + '</td><td>' + esc(res.detail) + '</td></tr>';
    }
    html += '</tbody></table>';
    pushResults.innerHTML = html;
    await loadCatalog();
    setStatus('Added ' + data.added + ' entr' + (data.added === 1 ? 'y' : 'ies') +
      ' to the catalog' + (data.skipped ? ', skipped ' + data.skipped : '') + '.', 'ok');
  } catch (e) {
    pushResults.innerHTML = '<p class="msg error">Error: ' + e.message + '</p>';
  }
});

// Inline status next to the buttons (top of the page) — the #catalog-msg
// div sits below the whole table, which made refresh feedback invisible.
const catalogStatus = document.getElementById('catalog-status');
function setStatus(text, kind) {
  if (!catalogStatus) return;
  catalogStatus.textContent = text;
  catalogStatus.className = 'ctl-status' + (kind ? ' ' + kind : '');
  catalogMsg.className = 'msg' + (kind ? ' ' + kind : '');
  catalogMsg.textContent = text;  // keep the bottom-of-page copy in sync
}

const refreshCatalogBtn = document.getElementById('refresh-catalog-btn');
refreshCatalogBtn.addEventListener('click', async () => {
  refreshCatalogBtn.disabled = true;
  setStatus('Reloading catalog...', '');
  try {
    await loadCatalog();
    const n = Object.values(catalogData.tiers || {}).reduce((a, l) => a + l.length, 0);
    setStatus('Catalog reloaded — ' + n + ' entr' + (n === 1 ? 'y' : 'ies') +
      ' · ' + new Date().toLocaleTimeString(), 'ok');
  } catch (e) {
    setStatus('Reload failed: ' + e.message, 'error');
  } finally {
    refreshCatalogBtn.disabled = false;
  }
});

// "Refresh from GitHub": fetch the latest catalog JSON from GitHub via the
// API and merge it in (new entries added, user edits kept, removed entries
// stay removed), then re-render the table.
const refreshGithubBtn = document.getElementById('refresh-github-btn');
if (refreshGithubBtn) {
  refreshGithubBtn.addEventListener('click', async () => {
    refreshGithubBtn.disabled = true;
    setStatus('Refreshing from GitHub...', '');
    try {
      const r = await fetch('/api/catalog/refresh', { method: 'POST' });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
      if (data.added) {
        setStatus('Refreshed from GitHub: ' + data.added + ' new entr' +
          (data.added === 1 ? 'y' : 'ies') + ' added' +
          (data.skipped ? ', ' + data.skipped + ' already in catalog' : '') +
          (data.removed ? ', ' + data.removed + ' removed entr' + (data.removed === 1 ? 'y' : 'ies') + ' skipped' : '') + '.', 'ok');
      } else {
        setStatus('Up to date with GitHub' +
          (data.skipped ? ' (' + data.skipped + ' entr' + (data.skipped === 1 ? 'y' : 'ies') + ' already present)' : '') +
          (data.removed ? ', ' + data.removed + ' removed entr' + (data.removed === 1 ? 'y' : 'ies') + ' stayed removed' : '') + '.', 'ok');
      }
      await loadCatalog();
    } catch (e) {
      setStatus('Refresh from GitHub failed: ' + e.message, 'error');
    } finally {
      refreshGithubBtn.disabled = false;
    }
  });
}

// ---- Catalog search ----
// Filters the rendered tree live.  Re-rendering (rather than hiding rows) keeps
// the lab/model counts honest: they report what matched, not what was hidden.
const searchInput = document.getElementById('catalog-search-input');
const searchClear = document.getElementById('catalog-search-clear');

// Both filters share one URL writer, so a filtered view (text AND/OR GPU class)
// is always a shareable link.
function syncUrl() {
  try {
    const url = new URL(window.location.href);
    if (searchState.query) url.searchParams.set('q', searchState.query);
    else url.searchParams.delete('q');
    if (searchState.tier) url.searchParams.set('gpu', searchState.tier);
    else url.searchParams.delete('gpu');
    window.history.replaceState(null, '', url);
  } catch (e) { /* non-fatal: the filter still applies */ }
}

function applySearch(value) {
  searchState.query = value;
  if (searchClear) searchClear.hidden = !value;
  syncUrl();
  renderCatalog();
}

function applyTier(value) {
  searchState.tier = value;
  syncUrl();
  renderCatalog();
}

if (searchInput) {
  let debounce = null;
  searchInput.addEventListener('input', () => {
    clearTimeout(debounce);
    // Short debounce keeps typing smooth while the user is still mid-word.
    debounce = setTimeout(() => applySearch(searchInput.value), 120);
  });
  searchInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      searchInput.value = '';
      applySearch('');
    }
  });
}
if (searchClear) {
  searchClear.addEventListener('click', () => {
    if (searchInput) { searchInput.value = ''; searchInput.focus(); }
    applySearch('');
  });
}

// ---- GPU class filter ----
const gpuRadios = [...document.querySelectorAll('input[name="gpu-class"]')];
if (gpuRadios.length) {
  gpuRadios.forEach(radio => {
    radio.addEventListener('change', () => {
      if (radio.checked) applyTier(radio.value);
    });
  });
  // Restore a ?gpu= deep link onto the matching radio (the template renders
  // "All" checked, so without this the URL and the control would disagree).
  if (searchState.tier) {
    const match = gpuRadios.find(r => r.value === searchState.tier);
    if (match) match.checked = true;
    else searchState.tier = '';
  }
}

if (searchInput && searchState.query) {
  searchInput.value = searchState.query;
  if (searchClear) searchClear.hidden = false;
}

loadCatalog();
