// ---- Manual deletion (admin) ----
//
// Zone D of .scratch/manual-delete/CONTRACT.md. This page is the destructive
// twin of the GC dry-run report: the user picks exact cache directories from
// the live preview and a Job deletes exactly those paths. Three invariants
// shape everything below:
//
//   * The deletion key lives ONLY in the #md-key input's value — an in-memory
//     JS string read fresh per request. It is never written to localStorage,
//     a cookie, or the URL, never logged, and it is sent as the X-Delete-Key
//     request header on the preview and delete calls. No request headers are
//     rendered or logged anywhere in this file: the deployment injects
//     Authorization: Bearer <id_token> and X-Refresh-Token on every request,
//     and a header dump would leak those tokens into browser logs.
//   * The client only ever sends cachepath strings received from the preview.
//     The server intersects them with a fresh scan and rejects anything else
//     (422), so a stale selection can never widen the blast radius — we still
//     prune selections on every re-render so the user always sees exactly
//     what would be sent.
//   * Unknown or missing verdicts are treated as "block" (fail-safe, same
//     posture as the GC report's cross-check handling).
//
// Selection state lives in Sets here, not in the DOM, for the same reason as
// catalog.js's selectedIds: the table re-renders on every preview, and
// DOM-only state would silently drop or keep stale rows across re-renders.

const PREVIEW_URL = '/api/admin/deletions/preview';
const DELETE_URL = '/api/admin/deletions';
const CONFIRM_WORD = 'DELETE';

const mdKeyInput = document.getElementById('md-key');
const mdLoadBtn = document.getElementById('md-load');
const mdMsg = document.getElementById('md-msg');
const mdTable = document.getElementById('md-table');
const mdTableBody = mdTable ? mdTable.querySelector('tbody') : null;
const mdSelectAll = document.getElementById('md-select-all');
const mdDeleteBtn = document.getElementById('md-delete');
const mdModal = document.getElementById('md-modal');
const mdModalBody = document.getElementById('md-modal-body');
const mdModalError = document.getElementById('md-modal-error');
const mdModalOverrides = document.getElementById('md-modal-overrides');
const mdModalConfirm = document.getElementById('md-modal-confirm');
const mdModalGo = document.getElementById('md-modal-go');
const mdModalCancel = document.getElementById('md-modal-cancel');
const mdCount = document.getElementById('md-count');
const mdBytes = document.getElementById('md-bytes');
const mdPathList = document.getElementById('md-path-list');
const mdModalClose = mdModal ? mdModal.querySelector('.modal-close') : null;
const mdModalBackdrop = mdModal ? mdModal.querySelector('.modal-backdrop') : null;

// Last preview response — the single source of truth the table renders from.
let previewRows = [];
// Selected cache paths and soft-block overrides, keyed by the exact cachepath
// string the server sent. These Sets are the source of truth; the checkboxes
// are re-synced from them on every render.
const selectedPaths = new Set();
const overriddenPaths = new Set();
let deleteBusy = false;
let previewBusy = false;
let lastFocus = null;

function setMdMsg(text, isError) {
  if (!mdMsg) return;
  mdMsg.textContent = text;
  mdMsg.className = isError ? 'msg error' : 'msg ok';
}

// Same formatter as app.js's fmtBytes (this page does not load app.js, and
// catalog.js already duplicates esc/parseJsonSafe for the same reason).
function fmtBytes(n) {
  if (n === null || n === undefined || isNaN(Number(n))) return '';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let v = Number(n), u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  return (u === 0 ? String(v) : v.toFixed(1)) + ' ' + units[u];
}

function parseJsonSafe(text, fallback) {
  try { return JSON.parse(text); } catch { return fallback; }
}

// Same extractor as app.js: never assume the body is JSON. An unhandled
// server error arrives as text/plain and an expired SSO session as an HTML
// redirect — r.json() would throw an opaque SyntaxError and hide the cause.
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

// Normalize one row's verdict; anything unrecognized is treated as block.
// (Fail-safe: a schema drift must widen the modal, not the blast radius.)
function verdictOf(row) {
  if (row && row.verdict === 'deletable') return 'deletable';
  if (row && row.verdict === 'soft_block') return 'soft_block';
  return 'block';
}

// The exact cachepath string, normalized once: Set keys, dataset values and
// modal filters all go through this so state can never split on type.
function pathOf(row) {
  return String((row && row.cachepath) || '');
}

// A row's checkbox may be ticked when it is deletable, or when it is
// soft-blocked AND the user ticked that row's override checkbox.
function isSelectable(row) {
  const v = verdictOf(row);
  return v === 'deletable' || (v === 'soft_block' && overriddenPaths.has(pathOf(row)));
}

function summarizeVerdicts(rows) {
  const counts = { deletable: 0, soft_block: 0, block: 0 };
  let bytes = 0;
  for (const row of rows) {
    counts[verdictOf(row)]++;
    if (row.bytes !== null && row.bytes !== undefined && !isNaN(Number(row.bytes))) {
      bytes += Number(row.bytes);
    }
  }
  return rows.length + ' model dir(s) on the scan root: ' + counts.deletable + ' deletable, ' +
    counts.soft_block + ' soft-blocked (override required), ' + counts.block +
    ' blocked — ' + fmtBytes(bytes) + ' total.';
}

// One <tr> per preview row. Built with createElement + textContent throughout
// so model names / paths / reasons can never inject markup.
function buildRow(row) {
  const v = verdictOf(row);
  const path = pathOf(row);
  const label = String(row.model_name || path || '(unnamed)');

  const tr = document.createElement('tr');
  // Row tint reuses the classes the GC report and push results already use:
  // red = blocked, warm = soft-blocked, green = deletable. Theme-safe because
  // all three are CSS-variable tints.
  tr.className = v === 'block' ? 'status-failed' : v === 'soft_block' ? 'push-skip' : 'status-succeeded';

  // Select checkbox — disabled for block rows, enabled for soft_block only
  // after the override box is ticked, free for deletable rows.
  const tdSelect = document.createElement('td');
  const selWrap = document.createElement('label');
  selWrap.className = 'checkbox-label';
  const selBox = document.createElement('input');
  selBox.type = 'checkbox';
  selBox.dataset.path = path;
  selBox.disabled = !isSelectable(row);
  const selAria = 'Select ' + label +
    (v === 'block' ? ' (blocked)' : v === 'soft_block' ? ' (override required)' : '');
  selBox.setAttribute('aria-label', selAria);
  if (v === 'block') {
    selBox.title = 'Blocked — ' + blockReason(row);
  } else if (v === 'soft_block') {
    selBox.title = 'Tick Override first to select this AIOLI-referenced path';
  }
  selBox.addEventListener('change', () => {
    if (selBox.checked) selectedPaths.add(path);
    else selectedPaths.delete(path);
    updateDeleteUi();
  });
  selWrap.appendChild(selBox);
  tdSelect.appendChild(selWrap);
  tr.appendChild(tdSelect);

  // Override checkbox — rendered only for soft_block rows; the explicit act
  // that says "AIOLI references this path and I want it gone anyway".
  const tdOverride = document.createElement('td');
  if (v === 'soft_block') {
    const ovWrap = document.createElement('label');
    ovWrap.className = 'checkbox-label';
    const ovBox = document.createElement('input');
    ovBox.type = 'checkbox';
    ovBox.dataset.overridePath = path;
    ovBox.setAttribute('aria-label', 'Override AIOLI reference for ' + label);
    ovBox.title = 'This path is referenced by AIOLI (packaged_models). Tick to allow selection and deletion.';
    ovBox.addEventListener('change', () => {
      if (ovBox.checked) {
        overriddenPaths.add(path);
      } else {
        overriddenPaths.delete(path);
        // Un-override must un-select too: a row can never stay selected
        // through a path that is no longer explicitly overridden.
        if (selectedPaths.has(path)) {
          selectedPaths.delete(path);
          selBox.checked = false;
        }
      }
      selBox.disabled = !isSelectable(row);
      updateDeleteUi();
    });
    ovWrap.appendChild(ovBox);
    tdOverride.appendChild(ovWrap);
  } else {
    tdOverride.textContent = '\u2014';
  }
  tr.appendChild(tdOverride);

  // Model + warnings (e.g. "no provenance manifest — cannot confirm this app
  // downloaded it").
  const tdModel = document.createElement('td');
  tdModel.className = 'cat-name';
  tdModel.textContent = label;
  for (const w of row.warnings || []) {
    const note = document.createElement('div');
    note.className = 'md-row-note';
    note.textContent = String(w);
    tdModel.appendChild(note);
  }
  tr.appendChild(tdModel);

  // Size — dash rather than a fabricated number when the scanner has none.
  const tdSize = document.createElement('td');
  tdSize.textContent = (row.bytes === null || row.bytes === undefined) ? '\u2014' : fmtBytes(row.bytes);
  tr.appendChild(tdSize);

  // Age in days, raw like the GC report renders it.
  const tdAge = document.createElement('td');
  tdAge.textContent = (row.age_days === null || row.age_days === undefined) ? '\u2014' : String(row.age_days);
  tr.appendChild(tdAge);

  // Manifest badge — same cells the GC report draws.
  const tdManifest = document.createElement('td');
  tdManifest.className = 'md-col-manifest';
  if (row.manifest === true) {
    const badge = document.createElement('span');
    badge.className = 'backend-badge backend-pvc';
    badge.textContent = 'manifest';
    tdManifest.appendChild(badge);
  } else if (row.manifest === false) {
    tdManifest.textContent = '\u2014';
  } else {
    tdManifest.textContent = '?';
  }
  tr.appendChild(tdManifest);

  // Scan verdict badge (clean vs ModelScan warn/unsafe).
  const tdScan = document.createElement('td');
  tdScan.className = 'md-col-scan';
  if (row.scanned) {
    const badge = document.createElement('span');
    badge.className = row.scanned === 'clean' ? 'backend-badge backend-pvc' : 'backend-badge backend-scan-risk';
    badge.textContent = String(row.scanned);
    tdScan.appendChild(badge);
  } else {
    tdScan.textContent = '\u2014';
  }
  tr.appendChild(tdScan);

  // Verdict badge + the exact block/soft reasons the evaluator produced.
  const tdVerdict = document.createElement('td');
  const vBadge = document.createElement('span');
  vBadge.className = 'backend-badge ' +
    (v === 'deletable' ? 'verdict-badge-deletable' : v === 'soft_block' ? 'verdict-badge-soft' : 'verdict-badge-block');
  vBadge.textContent = v === 'soft_block' ? 'soft block' : v;
  tdVerdict.appendChild(vBadge);
  const reasons = blockReason(row);
  if (reasons) {
    const rNote = document.createElement('div');
    rNote.className = 'md-verdict-reason';
    rNote.textContent = reasons;
    tdVerdict.appendChild(rNote);
  }
  tr.appendChild(tdVerdict);

  // The exact cachepath — the string this page sends back verbatim. It wraps
  // (unlike .pvc-url) because the user must be able to read every character.
  const tdPath = document.createElement('td');
  const pathCode = document.createElement('code');
  pathCode.className = 'md-path';
  pathCode.textContent = path;
  tdPath.appendChild(pathCode);
  tr.appendChild(tdPath);

  return tr;
}

// Human-readable block/soft reasons for a row, for the verdict cell.
function blockReason(row) {
  return (row.blocks || []).join('; ');
}

// Rebuild the table from previewRows. Prunes selection state to paths still
// present AND still selectable, so what the user sees ticked is exactly what
// the next submit would send.
function renderRows(emptyText) {
  const live = new Set(previewRows.map(pathOf));
  for (const p of [...selectedPaths]) {
    if (!live.has(p)) { selectedPaths.delete(p); continue; }
    const row = previewRows.find((r) => pathOf(r) === p);
    if (row && !isSelectable(row)) selectedPaths.delete(p);
  }
  for (const p of [...overriddenPaths]) {
    if (!live.has(p)) { overriddenPaths.delete(p); continue; }
    const row = previewRows.find((r) => pathOf(r) === p);
    if (row && verdictOf(row) !== 'soft_block') overriddenPaths.delete(p);
  }

  mdTableBody.innerHTML = '';
  if (!previewRows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 9;
    td.className = 'tier-empty';
    td.textContent = emptyText || 'No models found on the PVC scan root.';
    tr.appendChild(td);
    mdTableBody.appendChild(tr);
  } else {
    for (const row of previewRows) mdTableBody.appendChild(buildRow(row));
  }
  syncSelectionToDom();
  updateDeleteUi();
}

// Reflect the persistent Sets onto the freshly rendered checkboxes (the
// catalog.js pattern: DOM is a view of the Sets, never the source of truth).
function syncSelectionToDom() {
  mdTableBody.querySelectorAll('input[data-path]').forEach((box) => {
    const path = box.dataset.path;
    box.checked = selectedPaths.has(path);
    const row = previewRows.find((r) => pathOf(r) === path);
    box.disabled = row ? !isSelectable(row) : true;
  });
  mdTableBody.querySelectorAll('input[data-override-path]').forEach((box) => {
    box.checked = overriddenPaths.has(box.dataset.overridePath);
  });
}

// Enable the red button only when a selection exists; keep the select-all
// box honest (checked / indeterminate / unchecked) against selectable rows.
function updateDeleteUi() {
  const selectable = previewRows.filter((r) => isSelectable(r));
  const n = selectedPaths.size;
  if (mdDeleteBtn) {
    mdDeleteBtn.disabled = deleteBusy || n === 0;
    mdDeleteBtn.textContent = n
      ? 'Delete ' + n + ' selected path' + (n === 1 ? '' : 's') + '\u2026'
      : 'Delete selected\u2026';
  }
  if (mdSelectAll) {
    const checkedCount = selectable.filter((r) => selectedPaths.has(pathOf(r))).length;
    mdSelectAll.checked = selectable.length > 0 && checkedCount === selectable.length;
    mdSelectAll.indeterminate = checkedCount > 0 && checkedCount < selectable.length;
    mdSelectAll.disabled = selectable.length === 0;
  }
}

// ---- Auth mode ----
//
// "secret" needs a key the operator supplies; "sso" takes the identity from the
// PCAI oauth2-proxy gate and needs none. The server decides (manualDelete.auth
// .mode) and the template stamps it on the form. Ignoring it made the page
// demand a key that does not exist in an SSO deployment — the page was unusable
// there, which is exactly the bug this constant fixes.
const AUTH_MODE = (function () {
  const form = document.getElementById('md-key-form');
  return (form && form.dataset && form.dataset.authMode) || 'secret';
})();
const NEEDS_KEY = AUTH_MODE === 'secret';

function authHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (NEEDS_KEY) headers['X-Delete-Key'] = (mdKeyInput.value || '').trim();
  return headers;
}

// ---- Preview ----

async function loadPreview() {
  // Trimmed on read: pasted secrets pick up trailing whitespace/newlines far
  // more often than they legitimately contain leading spaces. The trimmed
  // string is what gets sent; the input itself is never rewritten.
  if (NEEDS_KEY && !(mdKeyInput.value || '').trim()) {
    setMdMsg('Enter the deletion key first.', true);
    mdKeyInput.focus();
    return;
  }
  // The disabled button does not stop Enter-in-input from resubmitting the
  // form, so re-entry is guarded here rather than only in the UI.
  if (previewBusy) return;
  previewBusy = true;
  setMdMsg('Loading preview from the live scan…', false);
  mdLoadBtn.disabled = true;
  try {
    const r = await fetch(PREVIEW_URL, {
      method: 'POST',
      headers: authHeaders(),
      // Empty selection = LIST mode: every candidate on the scan root with its
      // verdict. No verdict_id is issued until a real selection is pinned.
      body: JSON.stringify({ targets: [] }),
    });
    if (!r.ok) {
      selectedPaths.clear();
      overriddenPaths.clear();
      renderRows('No preview loaded (see status above).');
      setMdMsg('Preview failed: ' + (await apiErrorMessage(r)), true);
      return;
    }
    const data = parseJsonSafe(await r.text(), null);
    if (!data || !Array.isArray(data.rows)) {
      selectedPaths.clear();
      overriddenPaths.clear();
      renderRows('No preview loaded (see status above).');
      setMdMsg('Preview response was not the expected shape — ask an operator to check the server logs.', true);
      return;
    }
    previewRows = data.rows;
    renderRows('');
    if (data.scan_error || data.aioli_error) {
      // Same fail-safe surface as the GC report: cross-check trouble is shown,
      // and the verdicts on screen already reflect it (everything blocked).
      setMdMsg('Preview loaded with warnings: ' +
        [data.scan_error, data.aioli_error].filter(Boolean).join(' · '), true);
    } else {
      // The scan is shared with the Downloaded-models page, so say when it was
      // reused rather than letting a fast load look like nothing happened.
      const age = data.scan_age_seconds;
      const when = (typeof age === 'number')
        ? (age < 5 ? 'fresh scan' : 'reused a scan from ' + Math.round(age) + 's ago')
        : '';
      setMdMsg(summarizeVerdicts(previewRows) + (when ? ' \u00b7 ' + when : ''), false);
    }
  } catch (e) {
    selectedPaths.clear();
    overriddenPaths.clear();
    renderRows('No preview loaded (see status above).');
    setMdMsg('Preview request error: ' + e, true);
    console.error('manual delete preview failed', e);
  } finally {
    previewBusy = false;
    mdLoadBtn.disabled = false;
  }
}

// ---- Confirm modal ----

function openModal() {
  const chosen = previewRows.filter((r) => selectedPaths.has(pathOf(r)));
  if (!chosen.length) return;
  let total = 0;
  let unknown = 0;
  for (const r of chosen) {
    if (r.bytes === null || r.bytes === undefined || isNaN(Number(r.bytes))) unknown++;
    else total += Number(r.bytes);
  }
  if (mdCount) mdCount.textContent = String(chosen.length);
  if (mdBytes) mdBytes.textContent = fmtBytes(total) + (unknown ? ' + ' + unknown + ' unknown' : '');
  const overrides = chosen.filter((r) => overriddenPaths.has(pathOf(r)));
  if (mdModalOverrides) {
    mdModalOverrides.hidden = overrides.length === 0;
    if (overrides.length) {
      mdModalOverrides.textContent = overrides.length + ' of these ' +
        (overrides.length === 1 ? 'is' : 'are') + ' referenced by AIOLI (soft block) and will be deleted only because you ticked override.';
    }
  }
  mdPathList.innerHTML = '';
  for (const r of chosen) {
    const li = document.createElement('li');
    const code = document.createElement('code');
    code.textContent = pathOf(r);
    li.appendChild(code);
    mdPathList.appendChild(li);
  }
  if (mdModalError) { mdModalError.hidden = true; mdModalError.textContent = ''; }
  mdModalConfirm.value = '';
  mdModalGo.disabled = true;
  mdModalGo.textContent = 'Delete permanently';
  if (mdModalBody) mdModalBody.scrollTop = 0;  // stale scroll from a previous open
  mdModal.hidden = false;
  lastFocus = document.activeElement;
  // Cancel is the default focus: the safest action is one keypress away.
  mdModalCancel.focus();
}

function closeModal() {
  if (deleteBusy) return;  // no Escape out of an in-flight submission
  mdModal.hidden = true;
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

async function goDelete() {
  if (mdModalGo.disabled || deleteBusy) return;
  const chosen = previewRows.filter((r) => selectedPaths.has(pathOf(r)));
  if (!chosen.length) { closeModal(); return; }
  if (NEEDS_KEY && !(mdKeyInput.value || '').trim()) {
    closeModal();
    setMdMsg('Enter the deletion key first.', true);
    mdKeyInput.focus();
    return;
  }
  const targets = chosen.map((r) => ({
    cachepath: pathOf(r),
    override: overriddenPaths.has(pathOf(r)),
  }));
  deleteBusy = true;
  mdModalGo.disabled = true;
  mdModalGo.textContent = 'Submitting…';
  mdModalCancel.disabled = true;
  mdModalConfirm.disabled = true;
  if (mdModalError) { mdModalError.hidden = true; mdModalError.textContent = ''; }
  try {
    // Two phases on purpose. The server refuses to act on anything it did not
    // resolve itself, so the selection is pinned first and only the pinned set
    // can be deleted — what the operator reviewed is exactly what dies.
    const pin = await fetch(PREVIEW_URL, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ targets: chosen.map((t) => t.cachepath) }),
    });
    if (!pin.ok) {
      const detail = await apiErrorMessage(pin);
      if (mdModalError) { mdModalError.textContent = 'Deletion refused: ' + detail; mdModalError.hidden = false; }
      setMdMsg('Deletion refused: ' + detail, true);
      return;
    }
    const pinned = parseJsonSafe(await pin.text(), null);
    if (!pinned || !pinned.verdict_id) {
      const detail = 'the server did not pin a target set (nothing in the selection is deletable now)';
      if (mdModalError) { mdModalError.textContent = 'Deletion refused: ' + detail; mdModalError.hidden = false; }
      setMdMsg('Deletion refused: ' + detail, true);
      return;
    }
    const r = await fetch(DELETE_URL, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({
        verdict_id: pinned.verdict_id,
        confirm: CONFIRM_WORD,
        overrides: targets.filter((t) => t.override).map((t) => t.cachepath),
      }),
    });
    if (!r.ok) {
      const detail = await apiErrorMessage(r);
      if (mdModalError) { mdModalError.textContent = 'Deletion refused: ' + detail; mdModalError.hidden = false; }
      setMdMsg('Deletion refused: ' + detail, true);
      return;  // modal stays open so the user can retry or cancel
    }
    const data = parseJsonSafe(await r.text(), {});
    const jobId = data.job_name || '';
    closeModal();
    selectedPaths.clear();
    overriddenPaths.clear();
    // Re-pull the preview: the submitted Job now pins these paths as live, so
    // the fresh table should show them blocked — visible proof of what runs.
    await loadPreview();
    setMdMsg('Deletion job submitted' + (jobId ? ': ' + jobId : '') +
      '. It re-validates every path in-pod and deletes only the paths it was given; the Job\'s logs hold the per-target tombstones.', false);
  } catch (e) {
    if (mdModalError) { mdModalError.textContent = 'Deletion request error: ' + e; mdModalError.hidden = false; }
    setMdMsg('Deletion request error: ' + e, true);
    console.error('manual delete failed', e);
  } finally {
    deleteBusy = false;
    mdModalGo.textContent = 'Delete permanently';
    // Re-arm to exactly the state the confirm word justifies: a cleared or
    // unconfirmed word leaves the button disabled.
    mdModalGo.disabled = mdModalConfirm.value !== CONFIRM_WORD;
    mdModalCancel.disabled = false;
    mdModalConfirm.disabled = false;
  }
}

// ---- Wiring ----

// Enter in the key field submits the key form; md-load is its submit button.
// Guarded on mdLoadBtn only: in SSO mode the key input is not rendered, and
// requiring it here would leave the Load button dead.
if (mdLoadBtn) {
  const keyForm = document.getElementById('md-key-form');
  if (keyForm) {
    keyForm.addEventListener('submit', (e) => {
      e.preventDefault();
      loadPreview();
    });
  }
}

if (mdSelectAll) {
  mdSelectAll.addEventListener('change', () => {
    const on = mdSelectAll.checked;
    for (const row of previewRows) {
      if (!isSelectable(row)) continue;
      if (on) selectedPaths.add(pathOf(row));
      else selectedPaths.delete(pathOf(row));
    }
    syncSelectionToDom();
    updateDeleteUi();
  });
}

if (mdDeleteBtn) mdDeleteBtn.addEventListener('click', openModal);

if (mdModalConfirm) {
  // Exact, case-sensitive match, no trimming: typing "delete " or "DELETE "
  // does not count. This is the only gate on a permanent action, so it gets
  // to be pedantic.
  mdModalConfirm.addEventListener('input', () => {
    mdModalGo.disabled = mdModalConfirm.value !== CONFIRM_WORD;
  });
  mdModalConfirm.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && mdModalConfirm.value === CONFIRM_WORD && !deleteBusy) {
      e.preventDefault();
      goDelete();
    }
  });
}

if (mdModalGo) mdModalGo.addEventListener('click', goDelete);
if (mdModalCancel) mdModalCancel.addEventListener('click', closeModal);
if (mdModalClose) mdModalClose.addEventListener('click', closeModal);
if (mdModalBackdrop) mdModalBackdrop.addEventListener('click', closeModal);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && mdModal && !mdModal.hidden) closeModal();
});

// Keep Tab inside the dialog while it is open (the modal is the only active
// surface; background content is hidden behind the backdrop).
if (mdModal) {
  mdModal.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const focusables = [mdModalClose, mdModalConfirm, mdModalCancel, mdModalGo]
      .filter((el) => el && !el.disabled);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });
}
