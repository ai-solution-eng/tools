"""Embedded, self-contained HTML report template.

This module holds the full HTML/CSS/JS for the benchmark report.  It is
embedded as a big string so the whole report is a single shareable file.
"""

import json

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#0f1420; --panel:#171e2e; --panel2:#1d2638; --ink:#e6ecf5; --muted:#93a1b8;
  --accent:#4da3ff; --accent2:#7ce08e; --warn:#ffd166; --bad:#ff6b6b; --border:#2a3550;
  --header1:#141c2e; --header2:#0f1420; --args:#bfd6f5; --shadow:0 6px 18px rgba(0,0,0,.35);
}
html[data-theme="light"]{
  --bg:#f5f7fc; --panel:#ffffff; --panel2:#eef2f8; --ink:#1b2436; --muted:#5b6b82;
  --accent:#1d63d8; --accent2:#12803b; --warn:#9a6400; --bad:#cf2a2a; --border:#d4dbe8;
  --header1:#eef3fb; --header2:#ffffff; --args:#33415c; --shadow:0 6px 18px rgba(30,50,90,.10);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
header{padding:22px 26px;border-bottom:1px solid var(--border);background:linear-gradient(180deg,var(--header1),var(--header2));display:flex;align-items:flex-start;gap:16px;justify-content:space-between}
header .brand{flex:1}
header h1{margin:0 0 4px;font-size:22px}
header .sub{color:var(--muted);font-size:13px}
.wrap{max-width:1500px;margin:0 auto;padding:18px 26px 60px}
.controls{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:flex-end;padding:12px 16px;background:var(--panel);border:1px solid var(--border);border-radius:10px;margin-bottom:18px;position:sticky;top:0;z-index:20;box-shadow:var(--shadow)}
.controls label{color:var(--muted);font-size:12px;display:flex;flex-direction:column;gap:3px}
.controls select,.controls input[type=text]{background:var(--panel2);color:var(--ink);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px}
.controls input[type=text]{min-width:200px}
.controls button{background:var(--accent);border:0;color:#fff;font-weight:600;padding:7px 14px;border-radius:6px;cursor:pointer}
#btn-theme{background:var(--panel2);border:1px solid var(--border);color:var(--ink);font-size:16px;line-height:1;padding:8px 12px;border-radius:8px;cursor:pointer}
.controls label.inline{flex-direction:row;align-items:center;gap:6px}
.hint{color:var(--muted);font-size:12px;margin-left:auto;max-width:340px}
.model-block{margin-bottom:30px}
.model-block>h2{margin:0 0 12px;font-size:18px;display:flex;align-items:center;gap:10px}
.model-block>h2 .count{color:var(--muted);font-weight:400;font-size:13px}
.setup{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.setup.compared-setup{outline:2px solid var(--accent)}
.setup-head{display:flex;align-items:flex-start;gap:12px}
.setup-head .compare{flex:0 0 auto;margin-top:4px;display:flex;gap:6px;align-items:center;color:var(--muted);font-size:12px;cursor:pointer}
.file-header{margin:0;font-size:15px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 6px}
.badge{background:var(--panel2);border:1px solid var(--border);color:var(--ink);font-size:11.5px;border-radius:20px;padding:2px 10px}
.badge-mtp{background:rgba(124,225,177,.12);border-color:rgba(124,225,177,.4);color:var(--accent2)}
.badge-hicache{background:rgba(255,209,102,.1);border-color:rgba(255,209,102,.4);color:var(--warn)}
.badge-old{background:rgba(255,107,107,.12);border-color:rgba(255,107,107,.45);color:var(--bad)}
.meta-line{color:var(--muted);font-size:12px;margin:4px 0 10px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
table.bench{margin-top:8px}
th,td{padding:6px 9px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:600;background:var(--panel2)}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
td.num{font-variant-numeric:tabular-nums}
.fail-any{color:var(--bad);font-weight:700}
.muted{color:var(--muted)}
details.cfg{margin:8px 0;border:1px dashed var(--border);border-radius:8px;padding:8px 12px}
details.cfg summary{cursor:pointer;color:var(--accent);font-weight:600}
table.cfg{font-size:12.5px;margin-top:8px}
table.cfg th{text-align:left;width:180px;background:transparent}
code.args{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;color:var(--args);white-space:pre-wrap;word-break:break-all}
.compare-view{background:var(--panel);border:1px solid var(--accent);border-radius:10px;padding:16px;margin-bottom:18px;display:none}
.compare-view.show{display:block}
.compare-view h3{margin:0 0 10px}
.compare-view .ctrls{margin-bottom:10px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.compare-view .ctrls label{color:var(--muted);font-size:12px;display:flex;flex-direction:column;gap:3px}
.compare-view select{background:var(--panel2);color:var(--ink);border:1px solid var(--border);border-radius:6px;padding:5px 9px}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tabs button{background:var(--panel2);border:1px solid var(--border);color:var(--muted);font-weight:600;padding:8px 18px;border-radius:8px;cursor:pointer}
.tabs button.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.best{color:var(--accent2);font-weight:700}
.cmp-hdr{font-weight:700;color:var(--ink);display:block}
.cmp-file{font-weight:400;color:var(--muted);font-size:11px}
.compare-view table.bench th{white-space:normal;text-align:left;max-width:240px;min-width:100px;vertical-align:bottom}
#cmp-table-wrap{overflow-x:auto}
.worst{color:var(--bad);font-weight:700}
@media (max-width:900px){.wrap{padding:12px}}
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>__TITLE__</h1>
    <div class="sub">Generated __GENERATED__ &middot; results from <b>__RESULTS_DIR__</b> &middot; deployment catalog: <b>__CATALOG__</b> &middot; __MODELS__ model(s) / __SETUPS__ setup(s)<br>__RAG__ RAG scale-benchmark(s) in the RAG tab</div>
  </div>
  <button id="btn-theme" title="Toggle light / dark mode" aria-label="Toggle theme">&#x2600;</button>
</header>
<div class="wrap">
  <div class="controls">
    <label>Model<select id="f-model"></select></label>
    <label>GPU<select id="f-gpu"></select></label>
    <label>Engine<select id="f-engine"></select></label>
    <label>MTP config<select id="f-mtp"></select></label>
    <label>Task<select id="f-task"></select></label>
    <label>Users<select id="f-users"></select></label>
    <label>Context<select id="f-ctx"></select></label>
    <label>Search<input type="text" id="f-search" placeholder="filename, model..."></label>
    <label class="inline"><input type="checkbox" id="f-obsolete"> show obsolete</label>
    <button id="btn-clear">Reset</button>
    <div class="hint">Tick the <b>compare</b> box on 2+ setups for a side-by-side table. TTFT percentiles ascend (P100 = worst); tokens/s are inverted (P100 = slowest) &mdash; higher is better everywhere.</div>
  </div>

  <div class="tabs" id="tabs">
    <button id="tab-models" class="active">Models</button>
    <button id="tab-rag">RAG</button>
  </div>

  <div class="compare-view" id="compare-view">
    <h3>&#x2696; Comparison <span id="cmp-count" class="muted"></span></h3>
    <div class="ctrls" id="cmp-controls"></div>
    <div id="cmp-table-wrap"></div>
  </div>

  <div id="report"></div>
</div>

<script>
const DATA = __DATA__;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PCTS = ['P50','P95','P99','P100'];
const NBSP = '\u00a0\u00a0\u00a0';
const state = { tab:'models', model:'__all__', gpu:'__pcai__', engine:'__all__', mtp:'__all__', task:'__all__', users:'__all__', ctx:'__all__', q:'', showOld:false, compared:new Set() };

function flatSetups(){
  const out=[];
  for (const m of DATA.models) for (const s of m.setups) out.push({...s, model:m.name, slug:m.slug, id:m.slug+'::'+s.file});
  return out;
}
const ALL = flatSetups();
const ALL_RAG = (()=>{ const out=[]; for (const m of (DATA.rag_models||[])) for (const s of m.setups) out.push({...s, model:m.name, slug:m.slug, id:m.slug+'::'+s.file}); return out; })();
const uniq = (a) => [...new Set(a)].filter(Boolean).sort();

function fillSelect(id, vals, placeholder){
  const sel=document.getElementById(id);
  sel.innerHTML='<option value="__all__">'+placeholder+'</option>'+vals.map(v=>'<option value="'+esc(v)+'">'+esc(v)+'</option>').join('');
}
function matches(s){
  const m=s.meta||{};
  if (state.model!=='__all__' && s.model!==state.model) return false;
  if (state.gpu==='__pcai__'){
    if (!GPU_PCAI_MATCH.includes(m.gpu)) return false;
  } else if (state.gpu!=='__all__' && m.gpu!==state.gpu) return false;
  if (state.engine!=='__all__' && m.engine!==state.engine) return false;
  if (state.mtp!=='__all__' && m.mtp!==state.mtp) return false;
  if (!state.showOld && m.obsolete) return false;
  if (state.q){
    const q=state.q.toLowerCase();
    const hay=(s.file+' '+s.slug+' '+s.model+' '+(m.gpu||'')+' '+(m.mtp||'')).toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}
function filtered(){ return ALL.filter(matches); }
function rowMatches(r){
  if (state.task!=='__all__' && r.task!==state.task) return false;
  if (state.users!=='__all__' && String(r.users)!==state.users) return false;
  if (state.ctx!=='__all__' && String(r.ctx)!==state.ctx) return false;
  return true;
}

function badges(m){
  const b=[];
  if (m.gpu) b.push('<span class="badge">'+esc(m.gpu)+(m.gpu_count>1?' ×'+m.gpu_count:'')+'</span>');
  if (m.engine) b.push('<span class="badge">'+esc(m.engine)+'</span>');
  if (m.mtp) b.push('<span class="badge badge-mtp">MTP: '+esc(m.mtp)+'</span>');
  if (m.hicache!==null && m.hicache!==undefined) b.push('<span class="badge badge-hicache">HiCache: '+esc(m.hicache)+'</span>');
  b.push('<span class="badge">replicas: '+esc(m.replicas)+'</span>');
  if (m.obsolete) b.push('<span class="badge badge-old">obsolete</span>');
  return b.join('');
}
function catalogHtml(c){
  if (!c) return '<details class="cfg"><summary>PCAI deployment config (inferred from filename)</summary><p class="muted">No matching Model-Downloader catalog entry found for this setup.</p></details>';
  const fields=[
    ['catalog id',c.catalog_id],['name',c.name],['image',c.image],['tier',c.tier],
    ['model format',c.model_format],['GPU request',c.resource_request_gpu],
    ['memory request',c.resource_request_memory],['CPU request',c.resource_request_cpu],['caching',c.caching_enabled]
  ];
  const rows=fields.filter(f=>f[1]!==undefined&&f[1]!==null&&f[1]!=='').map(f=>'<tr><th>'+esc(f[0])+'</th><td>'+esc(f[1])+'</td></tr>').join('');
  const args=(c.arguments||[]).join(' ');
  return '<details class="cfg"><summary>PCAI deployment config — click to expand</summary><table class="cfg">'+rows+'</table>'+(args?'<code class="args">'+esc(args)+'</code>':'')+'</details>';
}
function tableHtml(s){
  const hdr=['ctx','users','task','failed','TTFT (ms):' + NBSP + 'P50 / P95 / P99 / P100'];
  if (s.multiturn) hdr.push('TTFT-post (ms):' + NBSP + 'P50 / P95 / P99 / P100');
  hdr.push('tokens/s:' + NBSP + 'P50 / P95 / P99 / P100');
  const thead='<thead><tr>'+hdr.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead>';
  const rows = s.rows.filter(rowMatches);
  if (!rows.length) return '<p class="muted">No rows for the selected Task / Users / Context.</p>';
  const body=rows.map(r=>{
    const tds=['<td>'+r.ctx+'</td>','<td>'+r.users+'</td>','<td>'+esc(r.task)+'</td>',
      '<td class="'+(r.failed?'fail-any':'')+'">'+r.failed+'</td>',
      '<td class="num">'+r.ttft.map(v=>v.toFixed(1)).join(' / ')+'</td>'];
    if (s.multiturn) tds.push('<td class="num">'+(r.ttft_post?r.ttft_post.map(v=>v.toFixed(1)).join(' / '):'—')+'</td>');
    tds.push('<td class="num">'+r.tokens.map(v=>v.toFixed(1)).join(' / ')+'</td>');
    return '<tr>'+tds.join('')+'</tr>';
  }).join('');
  return '<table class="bench">'+thead+'<tbody>'+body+'</tbody></table>';
}
function ragTable(rag){
  const rows=[];
  for (const k in (rag.config||{})) rows.push('<tr><th>'+esc(k)+'</th><td>'+esc(rag.config[k])+'</td></tr>');
  for (const k in (rag.results||{})){
    let v=rag.results[k];
    if (k==='Latency (ms)' && v && typeof v==='object') v=Object.entries(v).map(([a,b])=>a+': '+b).join(' · ');
    rows.push('<tr><th>'+esc(k)+'</th><td>'+esc(v)+'</td></tr>');
  }
  return '<table class="cfg">'+rows.join('')+'</table>';
}
function setupTitle(s){
  const m=s.meta||{};
  const parts=[];
  if (m.gpu) parts.push(m.gpu+(m.gpu_count>1?' ×'+m.gpu_count:''));
  if (m.engine) parts.push(m.engine);
  if (m.mtp) parts.push('MTP: '+m.mtp);
  if (m.hicache!==null&&m.hicache!==undefined) parts.push('HiCache: '+m.hicache);
  parts.push('replicas: '+m.replicas);
  return parts.join('  ·  ');
}
function renderSetup(s){
  const m=s.meta||{};
  const compared=state.compared.has(s.id);
  const body = s.rag ? ragTable(s.rag) : tableHtml(s);
  const cmpLabel = s.rag ? '' : '<label class="compare"><input type="checkbox" data-id="'+esc(s.id)+'" '+(compared?'checked':'')+'> compare</label>';
  return '<section class="setup'+(compared?' compared-setup':'')+'" data-id="'+esc(s.id)+'">'+
    '<div class="setup-head">'+
      cmpLabel+
      '<div style="flex:1">'+
        '<h2 class="file-header" title="'+esc(setupTitle(s))+'" >'+esc(setupTitle(s))+'</h2>'+
        '<div class="meta-line">file: <code>'+esc(s.file)+'</code> &middot; model: <b>'+esc(s.model)+'</b> &middot; mode: '+esc(s.mode)+' &middot; mtime: '+esc(s.mtime)+'</div>'+
        catalogHtml(s.catalog)+
        body+
      '</div>'+
    '</div>'+
  '</section>';
}
function matchesRag(s){
  if (!state.showOld && (s.meta&&s.meta.obsolete)) return false;
  if (state.q){
    const q=state.q.toLowerCase();
    const hay=(s.file+' '+s.slug+' '+s.model).toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}
function renderReport(){
  const report=document.getElementById('report');
  const ragTab = state.tab==='rag';
  const vis = ragTab ? (ALL_RAG||[]).filter(matchesRag) : filtered();
  const source = ragTab ? (DATA.rag_models||[]) : DATA.models;
  let html='';
  for (const m of source){
    const list=vis.filter(s=>s.slug===m.slug);
    if (!list.length) continue;
    html+='<div class="model-block"><h2>'+esc(m.name)+' <span class="count">('+list.length+' setup'+(list.length>1?'s':'')+')</span></h2>';
    html+=list.map(renderSetup).join('');
    html+='</div>';
  }
  report.innerHTML = html || '<p class="muted">No setups match the current filters.</p>';
  renderCompare();
}
function metricSel(){
  return '<label>Metric<select id="cmp-metric">'+
    '<option value="tokens_p50">tokens/s P50 (higher=better)</option>'+
    '<option value="tokens_p95">tokens/s P95</option>'+
    '<option value="ttft_p50">TTFT P50 (lower=better)</option>'+
    '<option value="ttft_p95">TTFT P95</option>'+
    '<option value="ttft_post_p50">TTFT-post P50</option>'+
    '<option value="ttft_post_p95">TTFT-post P95</option>'+
    '<option value="failed">failed requests</option>'+
  '</select></label>';
}
function renderCompare(){
  const cv=document.getElementById('compare-view');
  // Preserve the user's metric selection: the select is rebuilt below,
  // which would otherwise reset it to the default option on every render.
  const kept=document.getElementById('cmp-metric');
  const keptMetric=kept ? kept.value : null;
  const selected=ALL.filter(s=>state.compared.has(s.id));
  document.getElementById('cmp-count').textContent = selected.length ? '('+selected.length+' selected)' : '';
  cv.classList.toggle('show', selected.length>=2);
  if (selected.length<2){
    document.getElementById('cmp-controls').innerHTML='';
    document.getElementById('cmp-table-wrap').innerHTML='<p class="muted">Select 2 or more setups with the compare boxes above.</p>';
    return;
  }
  document.getElementById('cmp-controls').innerHTML=metricSel();
  const msel=document.getElementById('cmp-metric');
  if (!msel) return;
  if (keptMetric) msel.value=keptMetric;
  const mval=keptMetric||msel.value;
  const cfgMap={
    tokens_p50:['tokens','50'], tokens_p95:['tokens','95'],
    ttft_p50:['ttft','50'], ttft_p95:['ttft','95'],
    ttft_post_p50:['ttft_post','50'], ttft_post_p95:['ttft_post','95'],
    failed:['failed','0']
  };
  const [colKey, pct] = cfgMap[mval]||['tokens','50'];
  const pidx = pct==='0' ? -1 : PCTS.indexOf('P'+pct);

  const key = r=>r.ctx+'|'+r.users+'|'+r.task;
  let common=null;
  for (const s of selected){
    const k=new Set((s.rows||[]).filter(rowMatches).map(key));
    if (common===null) common=k; else common=new Set([...common].filter(x=>k.has(x)));
  }
  const wrap=document.getElementById('cmp-table-wrap');
  if (!common || !common.size){ wrap.innerHTML='<p class="muted">No shared (ctx, users, task) workload across the selected setups (for the current Task / Users / Context filters).</p>'; return; }
  const sorted=[...common].sort((a,b)=>{
    const pa=a.split('|'),pb=b.split('|');
    return (+pa[0])-(+pb[0]) || (+pa[1])-(+pb[1]) || pa[2].localeCompare(pb[2]);
  });
  const head=['workload'].concat(selected.map(s=>s.model ? (s.model+' — '+s.file) : s.file));
  const headTitle=['workload'].concat(selected.map(s=>s.model ? (setupTitle(s)+'  ·  '+s.model) : setupTitle(s)));
  const thead='<thead><tr>'+head.map((h,i)=>'<th><span class="cmp-hdr">'+esc(headTitle[i])+'</span><br><span class="cmp-file">'+esc(h)+'</span></th>').join('')+'</tr></thead>';
  const body=sorted.map(k=>{
    const [ctx,users,task]=k.split('|');
    const cells=['<td>ctx '+ctx+' &middot; '+users+' users &middot; '+esc(task)+'</td>'];
    const values=[];
    for (const s of selected){
      const r=(s.rows||[]).find(r=>key(r)===k);
      let v=null;
      if (r){
        if (colKey==='failed') v=r.failed;
        else if (colKey==='ttft_post') v=r.ttft_post ? r.ttft_post[pidx] : null;
        else v=r[colKey][pidx];
      }
      values.push(v);
      cells.push('<td class="num">'+(v===null?'—':v.toFixed(1))+'</td>');
    }
    const num=values.filter(v=>v!==null&&v!==undefined);
    if (num.length>=2){
      const better=(a,b)=> colKey==='failed' ? a<b : (colKey==='tokens' ? a>b : a<b);
      let best=num[0],worst=num[0];
      for (const v of num){ if (better(v,best)) best=v; if (better(worst,v)) worst=v; }
      values.forEach((v,i)=>{
        if (v===null) return;
        const idx=i+1;
        if (v===best) cells[idx]=cells[idx].replace('<td','<td class="best"');
        else if (v===worst) cells[idx]=cells[idx].replace('<td','<td class="worst"');
      });
    }
    return '<tr>'+cells.join('')+'</tr>';
  }).join('');
  wrap.innerHTML='<table class="bench">'+thead+'<tbody>'+body+'</tbody></table>';
}

function numSort(a,b){ return Number(a)-Number(b); }
function setTab(which){
  document.getElementById('tab-models').classList.toggle('active', which==='models');
  document.getElementById('tab-rag').classList.toggle('active', which==='rag');
}
// GPU filter presets.  PCAI is the default: it selects the PCIe PCAI
// hardware (H200 PCIe + RTX PRO 6000).  Individual GPUs follow.
const GPU_PCAI_MATCH = ['NVIDIA H200 (PCIe)', 'NVIDIA RTX PRO 6000'];
const GPU_PRESETS = [
  { v:'__pcai__', label:'PCAI: H200 (PCIe) + RTX Pro 6000', match:GPU_PCAI_MATCH },
  { v:'__all__',  label:'All GPUs' },
];
const GPU_LABELS = {
  'NVIDIA H200 (PCIe)':'H200 (PCIe)',
  'NVIDIA H200 (SXM)':'H200 (SXM)',
  'NVIDIA RTX PRO 6000':'RTX Pro 6000',
};
function fillGpuSelect(){
  const avail = new Set(ALL.map(s=>s.meta&&s.meta.gpu));
  const known=['NVIDIA H200 (PCIe)','NVIDIA H200 (SXM)','NVIDIA RTX PRO 6000'];
  const gpus=[...known, ...uniq([...avail]).filter(g=>!known.includes(g))];
  const sel=document.getElementById('f-gpu');
  sel.innerHTML = GPU_PRESETS.map(p=>'<option value="'+esc(p.v)+'"'+(p.v==='__pcai__'?' selected':'')+'>'+esc(p.label)+'</option>').join('')
    + gpus.map(g=>'<option value="'+esc(g)+'">'+esc(GPU_LABELS[g]||g)+'</option>').join('');
}
function initSelects(){
  fillSelect('f-model', DATA.models.map(m=>m.name), 'All models');
  fillGpuSelect();
  fillSelect('f-engine', uniq(ALL.map(s=>s.meta&&s.meta.engine)), 'All engines');
  fillSelect('f-mtp', uniq(ALL.map(s=>s.meta&&s.meta.mtp)), 'All MTP configs');
  const tasks = uniq(ALL.flatMap(s=>s.rows ? s.rows.map(r=>r.task) : []));
  const users = uniq(ALL.flatMap(s=>s.rows ? s.rows.map(r=>String(r.users)) : [])).sort(numSort);
  const ctxs  = uniq(ALL.flatMap(s=>s.rows ? s.rows.map(r=>String(r.ctx))  : [])).sort(numSort);
  fillSelect('f-task', tasks, 'All tasks');
  fillSelect('f-users', users, 'All users');
  fillSelect('f-ctx', ctxs, 'All contexts');
}
function wire(){
  document.getElementById('tab-models').addEventListener('click',()=>{ state.tab='models'; setTab('models'); renderReport(); });
  document.getElementById('tab-rag').addEventListener('click',()=>{ state.tab='rag'; setTab('rag'); renderReport(); });
  document.getElementById('f-model').addEventListener('change',e=>{state.model=e.target.value;renderReport();});
  document.getElementById('f-gpu').addEventListener('change',e=>{state.gpu=e.target.value;renderReport();});
  document.getElementById('f-engine').addEventListener('change',e=>{state.engine=e.target.value;renderReport();});
  document.getElementById('f-mtp').addEventListener('change',e=>{state.mtp=e.target.value;renderReport();});
  document.getElementById('f-task').addEventListener('change',e=>{state.task=e.target.value;renderReport();});
  document.getElementById('f-users').addEventListener('change',e=>{state.users=e.target.value;renderReport();});
  document.getElementById('f-ctx').addEventListener('change',e=>{state.ctx=e.target.value;renderReport();});
  document.getElementById('f-search').addEventListener('input',e=>{state.q=e.target.value;renderReport();});
  document.getElementById('f-obsolete').addEventListener('change',e=>{state.showOld=e.target.checked;renderReport();});
  document.getElementById('btn-clear').addEventListener('click',()=>{
    state.model='__all__';state.gpu='__pcai__';state.engine='__all__';state.mtp='__all__';state.task='__all__';state.users='__all__';state.ctx='__all__';state.q='';state.showOld=false;
    ['f-model','f-engine','f-mtp','f-task','f-users','f-ctx'].forEach(id=>document.getElementById(id).value='__all__');
    document.getElementById('f-gpu').value='__pcai__';
    document.getElementById('f-search').value='';
    document.getElementById('f-obsolete').checked=false;
    renderReport();
  });
  document.addEventListener('change',e=>{
    if (e.target.matches('input[data-id]')){
      const id=e.target.getAttribute('data-id');
      if (e.target.checked) state.compared.add(id); else state.compared.delete(id);
      renderReport();
    }
    if (e.target.id==='cmp-metric') renderCompare();
  });
}
// ---- light / dark mode ----
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('btn-theme').innerHTML = t==='light' ? '&#x1F319;' : '&#x2600;';
}
function initTheme(){
  let t;
  try { t = localStorage.getItem('bench-theme'); } catch(e){}
  if (!t) t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
  applyTheme(t);
  document.getElementById('btn-theme').addEventListener('click', ()=>{
    const next = document.documentElement.getAttribute('data-theme')==='light' ? 'dark' : 'light';
    applyTheme(next);
    try { localStorage.setItem('bench-theme', next); } catch(e){}
  });
}
initTheme();
initSelects();
wire();
renderReport();
</script>
</body>
</html>
"""


def build_html(data: dict, title: str) -> str:
    out = HTML
    out = out.replace("__TITLE__", title)
    out = out.replace("__GENERATED__", data.get("generated", ""))
    out = out.replace("__RESULTS_DIR__", data.get("results_dir", ""))
    out = out.replace("__CATALOG__", data.get("catalog") or "none")
    out = out.replace("__MODELS__", str(data.get("model_count", 0)))
    out = out.replace("__SETUPS__", str(data.get("setup_count", 0)))
    out = out.replace("__RAG__", str(data.get("rag_count", 0)))
    out = out.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    return out
