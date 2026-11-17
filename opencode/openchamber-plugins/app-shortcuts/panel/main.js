(()=>{var i="openchamber.sdk";var Se=`
:root {
  --oc-scrollbar-thumb: color-mix(in srgb, var(--oc-muted, currentColor) 40%, transparent);
  --oc-scrollbar-thumb-hover: color-mix(in srgb, var(--oc-muted, currentColor) 65%, transparent);
  scrollbar-gutter: stable;
}
* {
  scrollbar-width: thin;
  scrollbar-color: var(--oc-scrollbar-thumb) transparent;
}
/* Chromium's standard scrollbar properties otherwise override its pseudo-elements. */
@supports selector(::-webkit-scrollbar) {
  * { scrollbar-width: auto; scrollbar-color: auto; }
  ::-webkit-scrollbar { width: 6px; height: 6px; background: transparent; }
  :root::-webkit-scrollbar, body::-webkit-scrollbar { background: var(--oc-bg, inherit); }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb {
    background: var(--oc-scrollbar-thumb);
    border-radius: 999px;
    min-width: 24px;
    min-height: 24px;
  }
  ::-webkit-scrollbar-thumb:hover { background: var(--oc-scrollbar-thumb-hover); }
  ::-webkit-scrollbar-corner { background: transparent; }
  ::-webkit-scrollbar-button { display: none; width: 0; height: 0; }
}
@media (forced-colors: active) {
  * { scrollbar-color: auto; }
  ::-webkit-scrollbar-thumb, ::-webkit-scrollbar-thumb:hover { background: CanvasText; }
}
`;var Je=["file","directory","other","missing"],_e=e=>!!(e&&"sessionId"in e),be=e=>!!(e&&"sent"in e&&!("sessionId"in e));var te=500,oe=32e3,Ye=16e3,qe=128,We=200,Qe=2e3,Ze=16e3,et=80,tt=200,ot=16e3;var nt=2e3;var Ae=2e4,ne=1024,re=2e6;var se=64e3,ie=8e3,ye=4e3;var ve=9e4;var rt=999,ae=500,st=["HOST_UNAVAILABLE","HOST_TIMEOUT","HOST_REJECTED","DISCONNECTED","DISABLED","BAD_PATH","NO_INTEGRATION","NO_SERVICE","SERVICE_FAILED","NO_SESSION","SESSION_BUSY","NOT_GRANTED","NO_DIRECTORY","NOT_FOUND","FILE_TOO_LARGE","DENIED","NO_MODEL","MODEL_FAILED"],it=["stopped","starting","ready","failed"],St=new Set(st),at=e=>St.has(e),ct=e=>e&&at(e)?e:"HOST_REJECTED",z=e=>e===void 0?!1:e===null||e===!0||e===!1||String(e)===e?!0:Number(e)===e?Number.isFinite(e):Array.isArray(e)?e.every(z):Object(e)===e?Object.values(e).every(z):!1,dt=e=>z(e)&&JSON.stringify(e).length<=ot,Ve=e=>e?.trim().slice(0,tt)??"",F=e=>{let n=e.id.trim().slice(0,qe),l=e.title.trim().slice(0,We),G=e.url.trim().slice(0,Qe),C=e.text?.trim().slice(0,Ze),u=e.author?.trim().slice(0,et),T=e.kind==="pull"?"pull":"issue",x={providerId:e.providerId.trim(),id:n,title:l||n,url:G,kind:T};if(C&&(x.text=C),u&&(x.author=u),T==="pull"){let R=Ve(e.branches?.head),I=Ve(e.branches?.base);R&&I&&(x.branches={head:R,base:I})}return dt(e.data)&&(x.data=e.data),x},we=e=>{let n=F(e);return e.projectId&&(n.projectId=e.projectId),e.navigation&&(n.navigation=e.navigation),e.worktree&&(n.worktree=e.worktree),n},Oe=e=>{let n={text:e.text.trim().slice(0,Ye)};return e.send&&(n.send=!0),n},Me=e=>e===null||!Number.isFinite(e)?null:Math.min(rt,Math.max(0,Math.round(e)));var K=e=>e.length>0&&e.length<=ne&&!e.includes("\0")&&!e.includes("\\");var ce=e=>!e.startsWith("/")||e.includes("\0")||e.includes("\\")||e.includes("://")||e.length>nt?!1:!e.split("/").some(l=>l==="."||l===".."),gt=new Set(it),Ce=e=>!!(e&&"status"in e&&gt.has(String(e.status))&&!("body"in e)),de=e=>!!(e&&"status"in e&&"body"in e&&Number.isInteger(e.status)),Re=e=>!!(e&&"content"in e&&String(e.content)===e.content),Le=e=>!!(e&&"written"in e&&e.written===!0),Ue=e=>!!(e&&"entries"in e&&Array.isArray(e.entries)),_t=new Set(Je),Ie=e=>!!(e&&"kind"in e&&"size"in e&&_t.has(String(e.kind))&&Number.isFinite(e.size)),Ge=e=>!!(e&&"text"in e&&String(e.text)===e.text&&!("status"in e)),bt=new Set(["workspace","ready","directory","session","connection","settings","session-lifecycle","item","resolve","action"]),At=e=>Object(e)===e?e:null,ge=e=>String(e)===e&&e.length>0,yt=e=>{if(!ge(e.id))return null;if(e.ok===!0){let n={channel:i,v:1,type:"result",id:e.id,ok:!0};return Object(e.payload)===e.payload&&(n.payload=e.payload),n}return e.ok===!1&&ge(e.error)?{channel:i,v:1,type:"result",id:e.id,ok:!1,error:e.error,code:ct(ge(e.code)?e.code:void 0)}:null},Ne=e=>{let n=At(e);return!n||n.channel!==i||n.v!==1?null:n.type==="result"?yt(n):!bt.has(String(n.type))||Object(n.payload)!==n.payload?null:n};var r=class extends Error{code;constructor(n,l){super(l),this.name="HostRequestError",this.code=n}},lt=()=>Promise.reject(new r("BAD_PATH",'Request path must start with "/" and stay on the declared origin.')),le=()=>Promise.reject(new r("BAD_PATH",`File path must be 1 to ${ne} characters without NUL or backslash.`)),d=e=>(e.value+=1,`oc-${e.value}`),$e=(e={})=>{let n=e.target??("window"in globalThis?window:null);if(!n)throw new r("HOST_UNAVAILABLE","No window. connectHost runs in a browser frame.");let l=e.acceptSource??(t=>t===n.parent),G=e.requestTimeoutMs??Ae,C=new Set,u=new Set,T=new Set,x=new Set,R=new Set,I=new Set,J=new Set,Y=null,q=null,X=new Map,B=new Map,W=!1,c={value:0},p=null,L=null,Ke=t=>t?{sessionId:t.id,phase:t.busy?"started":"completed"}:null,N=t=>{n.parent.postMessage(t,"*")},g=(t,o)=>{for(let s of t)try{s(o)}catch(m){console.error(m)}},je=t=>{if(!(t instanceof MessageEvent)||!l(t.source))return;let o=Ne(t.data);if(!o)return;if(o.type==="workspace"){let m=B.get(o.payload.subscriptionId);m&&g([m],o.payload.snapshot);return}if(o.type==="ready"){p=o.payload,L=Ke(o.payload.session),g(C,o.payload),g(u,o.payload.directory),g(T,o.payload.session),L&&g(x,L),g(R,o.payload.connection),g(I,o.payload.settings),g(J,o.payload.item);return}if(o.type==="directory"){p&&(p={...p,directory:o.payload.directory}),g(u,o.payload.directory);return}if(o.type==="session"){p&&(p={...p,session:o.payload.session}),o.payload.session?L?.sessionId!==o.payload.session.id&&(L=Ke(o.payload.session)):L=null,g(T,o.payload.session);return}if(o.type==="session-lifecycle"){L=o.payload,g(x,o.payload);return}if(o.type==="connection"){p&&(p={...p,connection:o.payload.connection}),g(R,o.payload.connection);return}if(o.type==="settings"){p&&(p={...p,settings:o.payload.settings}),g(I,o.payload.settings);return}if(o.type==="item"){p&&(p={...p,item:o.payload.item}),g(J,o.payload.item);return}if(o.type==="action"){let m=k=>{W||N({channel:i,v:1,type:"action-result",id:o.id,payload:k})},O=q;if(!O){m({ok:!1,error:"This extension does not handle background actions."});return}Promise.resolve().then(()=>O(o.payload)).then(()=>m({ok:!0}),k=>{let Te=(k instanceof Error?k.message:String(k)).trim();m({ok:!1,error:(Te||"Action failed.").slice(0,ae)})});return}if(o.type==="resolve"){let m=k=>{N({channel:i,v:1,type:"resolve-result",id:o.id,payload:k})},O=Y;if(!O){m({error:"This extension does not resolve commands."});return}Promise.resolve().then(()=>O(o.payload)).then(k=>m({item:k?F(k):null}),k=>{let Te=(k instanceof Error?k.message:String(k)).trim();m({error:(Te||"Command failed.").slice(0,ae)})});return}let s=X.get(o.id);if(s){if(clearTimeout(s.timer),X.delete(o.id),o.ok){s.resolve(o.payload);return}s.reject(new r(o.code,o.error))}};n.addEventListener("message",je),N({channel:i,v:1,type:"hello"});let A=(t,o=G)=>W||n.parent===n?Promise.reject(new r("HOST_UNAVAILABLE","No host frame. This page is not in an iframe.")):new Promise((s,m)=>{let O=setTimeout(()=>{X.delete(t.id),m(new r("HOST_TIMEOUT","Host did not answer in time."))},o);X.set(t.id,{resolve:s,reject:m,timer:O}),N(t)}),y=t=>A(t).then(()=>{}),$={channel:i,v:1},H=(t,o=1024)=>{if(!t.trim()||t.length>o)throw new r("HOST_REJECTED",`Identity must contain 1 to ${o} characters.`)},xe=async t=>{t.kind!=="projects"&&H(t.projectId);let o=await A({...$,type:"workspace-read",id:d(c),payload:t});if(!o||!("kind"in o)||!("state"in o)||o.kind!==t.kind)throw new r("HOST_REJECTED","Host did not return workspace data.");return o},ke=async(t,o)=>{t.kind!=="projects"&&H(t.projectId);let s=d(c);B.set(s,o);try{await y({...$,type:"workspace-subscribe",id:d(c),payload:{subscriptionId:s,query:t}})}catch(m){throw B.delete(s),W||N({...$,type:"workspace-unsubscribe",id:d(c),payload:{subscriptionId:s}}),m}return()=>{!B.delete(s)||W||N({...$,type:"workspace-unsubscribe",id:d(c),payload:{subscriptionId:s}})}},ee=async t=>{if("key"in t&&(t.key.length===0||t.key.length>128))throw new r("HOST_REJECTED","Storage key must contain 1 to 128 characters.");if(t.op==="set"&&!z(t.value))throw new r("HOST_REJECTED","Storage values must be JSON.");if(t.op==="set"&&new TextEncoder().encode(JSON.stringify(t.value)).length>65536)throw new r("HOST_REJECTED","Storage value exceeds 64 KiB.");let o=await A({...$,type:"storage",id:d(c),payload:t});if(!o||!("storage"in o)||o.op!==t.op)throw new r("HOST_REJECTED","Host did not return storage data.");return o};return{onAction:t=>(q=t,()=>{q===t&&(q=null)}),listProjects:async()=>{let t=await xe({kind:"projects"});if(t.kind!=="projects")throw new r("HOST_REJECTED","Expected projects.");return t},listWorktrees:async t=>{let o=await xe({kind:"worktrees",projectId:t});if(o.kind!=="worktrees")throw new r("HOST_REJECTED","Expected worktrees.");return o},listSessions:async t=>{let o=await xe({kind:"sessions",projectId:t});if(o.kind!=="sessions")throw new r("HOST_REJECTED","Expected sessions.");return o},onProjects:t=>ke({kind:"projects"},o=>{o.kind==="projects"&&t(o)}),onWorktrees:(t,o)=>ke({kind:"worktrees",projectId:t},s=>{s.kind==="worktrees"&&o(s)}),onSessions:(t,o)=>ke({kind:"sessions",projectId:t},s=>{s.kind==="sessions"&&o(s)}),openSession:async t=>{H(t),await y({...$,type:"open-session",id:d(c),payload:{sessionId:t}})},storage:{get:async t=>{let o=await ee({op:"get",key:t});return o.op==="get"&&o.found?o.value:void 0},set:async(t,o)=>{await ee({op:"set",key:t,value:o})},delete:async t=>{await ee({op:"delete",key:t})},keys:async()=>{let t=await ee({op:"keys"});if(t.op!=="keys")throw new r("HOST_REJECTED","Expected storage keys.");return t.keys}},onReady:t=>(C.add(t),p&&t(p),()=>{C.delete(t)}),onDirectory:t=>(u.add(t),p&&t(p.directory),()=>{u.delete(t)}),onSession:t=>(T.add(t),p&&t(p.session),()=>{T.delete(t)}),onSessionLifecycle:t=>(x.add(t),L&&t(L),()=>{x.delete(t)}),onConnection:t=>(R.add(t),p&&t(p.connection),()=>{R.delete(t)}),onSettings:t=>(I.add(t),p&&t(p.settings),()=>{I.delete(t)}),onItem:t=>(J.add(t),p&&t(p.item),()=>{J.delete(t)}),onResolve:t=>(Y=t,()=>{Y===t&&(Y=null)}),toast:t=>{let o=t.message.trim();return!o||o.length>te?Promise.reject(new r("HOST_REJECTED",`Toast message must contain 1 to ${te} characters.`)):t.copy&&t.copy!==!0&&(!t.copy.text.length||t.copy.text.length>oe)?Promise.reject(new r("HOST_REJECTED",`Toast copy text must contain 1 to ${oe} characters.`)):y({channel:i,v:1,type:"toast",id:d(c),payload:{...t,message:o}})},openUrl:t=>y({channel:i,v:1,type:"open-url",id:d(c),payload:{url:t}}),openSurface:t=>y({channel:i,v:1,type:"open-surface",id:d(c),payload:{surfaceId:t}}),writeClipboard:t=>y({channel:i,v:1,type:"clipboard-write",id:d(c),payload:{text:t}}),compose:t=>y({channel:i,v:1,type:"compose",id:d(c),payload:t}),attach:t=>y({channel:i,v:1,type:"attach",id:d(c),payload:F(t)}),startSession:async t=>{t.projectId!==void 0&&H(t.projectId);let o=t.worktree;o&&o!==!0&&(o.kind==="existing"?H(o.directory):(o.name!==void 0&&H(o.name,200),o.baseBranch!==void 0&&H(o.baseBranch,200)));let s=await A({channel:i,v:1,type:"start-session",id:d(c),payload:we(t)},e.requestTimeoutMs??18e4);if(!_e(s))throw new r("HOST_REJECTED","Host did not return a session.");return s},prompt:t=>A({channel:i,v:1,type:"prompt",id:d(c),payload:Oe(t)}).then(o=>{if(!be(o))throw new r("HOST_REJECTED","Host did not return a prompt result.");return o}),sessionLink:t=>y({channel:i,v:1,type:"session-link",id:d(c),payload:F(t)}),close:()=>y({channel:i,v:1,type:"close",id:d(c)}),oauthStart:()=>y({channel:i,v:1,type:"oauth-start",id:d(c)}),oauthDisconnect:()=>y({channel:i,v:1,type:"oauth-disconnect",id:d(c)}),request:t=>(ce(t.path)?A({channel:i,v:1,type:"request",id:d(c),payload:t}):lt()).then(o=>{if(!de(o))throw new r("HOST_REJECTED","Host request result was empty.");return o}),serviceRequest:t=>(ce(t.path)?A({channel:i,v:1,type:"service-request",id:d(c),payload:t}):lt()).then(o=>{if(!de(o))throw new r("HOST_REJECTED","Host service request result was empty.");return o}),serviceStatus:()=>A({channel:i,v:1,type:"service-status",id:d(c)}).then(t=>{if(!Ce(t))throw new r("HOST_REJECTED","Host did not return service status.");return t}),readFile:t=>(K(t)?A({channel:i,v:1,type:"file-read",id:d(c),payload:{path:t}}):le()).then(o=>{if(!Re(o))throw new r("HOST_REJECTED","Host did not return file content.");return o}),writeFile:(t,o)=>K(t)?o.length>re?Promise.reject(new r("FILE_TOO_LARGE",`Content is over ${re} characters.`)):A({channel:i,v:1,type:"file-write",id:d(c),payload:{path:t,content:o}}).then(s=>{if(!Le(s))throw new r("HOST_REJECTED","Host did not confirm the write.");return s}):le(),listDir:t=>(K(t)?A({channel:i,v:1,type:"file-list",id:d(c),payload:{path:t}}):le()).then(o=>{if(!Ue(o))throw new r("HOST_REJECTED","Host did not return directory entries.");return o}),stat:t=>(K(t)?A({channel:i,v:1,type:"file-stat",id:d(c),payload:{path:t}}):le()).then(o=>{if(!Ie(o))throw new r("HOST_REJECTED","Host did not return file status.");return o}),generate:t=>{let o=t.prompt.trim(),s=t.system?.trim();if(o.length===0||o.length>se)return Promise.reject(new r("HOST_REJECTED",`Prompt must be 1 to ${se} characters.`));if(s!==void 0&&(s.length===0||s.length>ie))return Promise.reject(new r("HOST_REJECTED",`System prompt must be 1 to ${ie} characters.`));let m=t.maxOutputTokens===void 0?void 0:Math.min(ye,Math.max(1,Math.floor(t.maxOutputTokens)));if(m!==void 0&&!Number.isFinite(m))return Promise.reject(new r("HOST_REJECTED","maxOutputTokens must be a number."));let O={prompt:o};return s!==void 0&&(O.system=s),m!==void 0&&(O.maxOutputTokens=m),A({channel:i,v:1,type:"generate",id:d(c),payload:O},e.requestTimeoutMs??ve).then(k=>{if(!Ge(k))throw new r("HOST_REJECTED","Host did not return generated text.");return k})},setBadge:t=>y({channel:i,v:1,type:"badge",id:d(c),payload:{count:Me(t)}}),dispose:()=>{for(let t of B.keys())N({...$,type:"workspace-unsubscribe",id:d(c),payload:{subscriptionId:t}});B.clear(),W=!0,Y=null,q=null,n.removeEventListener("message",je);for(let t of X.values())clearTimeout(t.timer),t.reject(new r("HOST_UNAVAILABLE","Host client was disposed."));X.clear(),C.clear(),u.clear(),T.clear(),x.clear(),R.clear(),I.clear(),J.clear()}}};var vt=[["--oc-bg","background"],["--oc-elevated","elevated"],["--oc-fg","foreground"],["--oc-muted","muted"],["--oc-subtle","subtle"],["--oc-border","border"],["--oc-hover","hover"],["--oc-selection","selection"],["--oc-focus","focus"],["--oc-primary","primary"],["--oc-muted-surface","mutedSurface"],["--oc-elevated-fg","elevatedForeground"],["--oc-active","active"],["--oc-selection-fg","selectionForeground"],["--oc-primary-fg","primaryForeground"],["--oc-primary-text","primaryText"],["--oc-success-text","successText"],["--oc-warning-text","warningText"],["--oc-error-text","errorText"],["--oc-info-text","infoText"],["--oc-success","success"],["--oc-warning","warning"],["--oc-error","error"],["--oc-info","info"],["--oc-font","font"],["--oc-mono","mono"],["--oc-radius","radius"],["--surface-background","background"],["--surface-elevated","elevated"],["--surface-foreground","foreground"],["--surface-muted-foreground","muted"],["--surface-subtle","subtle"],["--interactive-border","border"],["--interactive-hover","hover"],["--interactive-selection","selection"],["--interactive-focus-ring","focus"],["--primary","primary"],["--surface-muted","mutedSurface"],["--surface-elevated-foreground","elevatedForeground"],["--interactive-active","active"],["--interactive-selection-foreground","selectionForeground"],["--primary-foreground","primaryForeground"],["--primary-text","primaryText"],["--success-text","successText"],["--warning-text","warningText"],["--error-text","errorText"],["--info-text","infoText"],["--status-success","success"],["--status-warning","warning"],["--status-error","error"],["--status-info","info"],["--font-sans","font"],["--font-mono","mono"],["--radius","radius"]],ft=(e,n)=>{n.style.colorScheme=e.mode;for(let[l,G]of vt)n.style.setProperty(l,e.tokens[G]);n.style.setProperty("font-family",e.tokens.font),n.style.setProperty("font-size","0.875rem"),n.style.setProperty("line-height","1.45"),n.style.setProperty("color",e.tokens.foreground)},He=(e,n)=>{ft(e.theme,n),n.dataset&&(n.dataset.ocSurface=e.surface,n.dataset.ocTheme=e.theme.mode)};var wt={"surface-background":"bg","surface-elevated":"elevated","surface-elevated-foreground":"elevated-fg","surface-foreground":"fg","surface-muted-foreground":"muted","surface-muted":"muted-surface","surface-subtle":"subtle","interactive-border":"border","interactive-hover":"hover","interactive-active":"active","interactive-selection":"selection","interactive-selection-foreground":"selection-fg","interactive-focus-ring":"focus",primary:"primary","primary-foreground":"primary-fg","primary-text":"primary-text","success-text":"success-text","warning-text":"warning-text","error-text":"error-text","info-text":"info-text","status-success":"success","status-warning":"warning","status-error":"error","status-info":"info","font-sans":"font","font-mono":"mono",radius:"radius"},f=(e,n)=>`var(--${e}, var(--oc-${wt[e]}, ${n}))`,P=f("surface-background","transparent"),pe=f("surface-elevated","transparent"),Q=f("surface-elevated-foreground","inherit"),Z=f("surface-foreground","inherit"),E=f("surface-muted-foreground","gray"),Ot=f("surface-muted","transparent"),w=f("interactive-border","currentColor"),v=f("interactive-hover","transparent"),D=f("interactive-active","transparent"),Pe=f("interactive-selection","transparent"),De=f("interactive-selection-foreground","inherit"),ht=f("interactive-focus-ring","currentColor"),M=f("primary","currentColor"),Xe=f("primary-text","inherit"),Be=f("error-text","inherit"),Mt=f("font-sans","inherit"),ze=f("font-mono","monospace"),mt=f("radius","9px"),h=(e,n,l="transparent")=>`color-mix(in srgb, ${e} ${n}%, ${l})`,Et=`box-shadow: 0 0 0 2px ${ht};`,ue=e=>{let n=f(`status-${e}`,"currentColor");return`
.oc-sdk[data-tone="${e}"], .oc-sdk [data-tone="${e}"] { --oc-sdk-tone: ${n}; --oc-sdk-tone-text: ${f(`${e}-text`,"inherit")}; }`},S=`
${Se}
.oc-sdk { box-sizing: border-box; color: ${Z}; font-family: ${Mt}; font-size: 0.875rem; line-height: 1.45; }
.oc-sdk *, .oc-sdk *::before, .oc-sdk *::after { box-sizing: border-box; }
/* :where() keeps the reset at zero specificity so every primitive class below overrides it. */
:where(.oc-sdk) :where(button, input, textarea), :where(button.oc-sdk, input.oc-sdk, textarea.oc-sdk) { font: inherit; color: inherit; margin: 0; }
:where(.oc-sdk) :where(button), :where(button.oc-sdk) { cursor: pointer; background: none; border: 0; padding: 0; }
.oc-sdk button:disabled, button.oc-sdk:disabled, .oc-sdk[aria-disabled="true"], .oc-sdk [aria-disabled="true"] { opacity: .5; pointer-events: none; }
.oc-sdk :focus-visible { outline: none; ${Et} }
.oc-sdk-mono { font-family: ${ze}; }
.oc-sdk-muted { color: ${E}; }
${ue("success")}${ue("warning")}${ue("error")}${ue("info")}
.oc-sdk[data-tone="primary"], .oc-sdk [data-tone="primary"] { --oc-sdk-tone: ${M}; --oc-sdk-tone-text: ${Xe}; }

.oc-sdk-btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 36px; padding: 0 14px; border: 1px solid transparent; border-radius: ${mt}; font-size: 0.875rem; font-weight: 500; line-height: 1; white-space: nowrap; transition: background 150ms ease-out, color 150ms ease-out; }
.oc-sdk-btn[data-size="sm"] { height: 32px; padding: 0 10px; font-size: 0.8125rem; }
.oc-sdk-btn[data-size="xs"] { height: 24px; padding: 0 8px; font-size: 0.75rem; border-radius: 6px; }
.oc-sdk-btn[data-variant="default"] { color: ${Xe}; background: ${h(M,10,P)}; border-color: ${h(M,12)}; }
.oc-sdk-btn[data-variant="default"]:hover { background: ${h(M,16,P)}; }
.oc-sdk-btn[data-variant="default"]:active { background: ${h(M,22,P)}; }
.oc-sdk-btn[data-variant="secondary"] { background: ${Ot}; color: var(--oc-fg); }
.oc-sdk-btn[data-variant="secondary"]:hover { background-image: linear-gradient(${v}, ${v}); }
.oc-sdk-btn[data-variant="secondary"]:active { background-image: linear-gradient(${D}, ${D}); }
.oc-sdk-btn[data-variant="outline"] { background: ${pe}; color: ${Q}; border-color: ${w}; }
.oc-sdk-btn[data-variant="outline"]:hover { background-image: linear-gradient(${v}, ${v}); }
.oc-sdk-btn[data-variant="outline"]:active { background-image: linear-gradient(${D}, ${D}); }
.oc-sdk-btn[data-variant="ghost"] { background: transparent; }
.oc-sdk-btn[data-variant="ghost"]:hover { background: ${v}; }
.oc-sdk-btn[data-variant="ghost"]:active { background: ${D}; }
.oc-sdk-btn[data-variant="destructive"] { --oc-sdk-tone: ${f("status-error","red")}; color: ${Be}; background: ${h("var(--oc-sdk-tone)",7,P)}; border-color: ${h("var(--oc-sdk-tone)",12)}; }
.oc-sdk-btn[data-variant="destructive"]:hover { background: ${h("var(--oc-sdk-tone)",9,P)}; }
.oc-sdk-btn[data-variant="destructive"]:active { background: ${h("var(--oc-sdk-tone)",11,P)}; }
.oc-sdk-btn[data-loading="true"] { opacity: .5; pointer-events: none; }
.oc-sdk-btn > .oc-sdk-spinner-ring { width: 14px; height: 14px; }

.oc-sdk-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.oc-sdk-field-label { font-size: 0.8125rem; font-weight: 500; }
.oc-sdk-field-note { font-size: 0.75rem; color: ${E}; }
.oc-sdk-field[data-invalid="true"] .oc-sdk-field-note { color: ${Be}; }
.oc-sdk-input { display: block; width: 100%; min-width: 0; height: 36px; padding: 0 12px; border: 0; border-radius: ${mt}; background: ${pe}; color: ${Q}; font-size: 0.875rem; line-height: 1.45; appearance: none; box-shadow: inset 0 0 0 1px ${h(w,60)}; transition: background 150ms ease-out, box-shadow 150ms ease-out; }
textarea.oc-sdk-input { height: auto; padding: 8px 12px; resize: vertical; }
.oc-sdk-input::placeholder { color: ${E}; }
.oc-sdk-input:hover:not(:focus) { background-image: linear-gradient(${v}, ${v}); }
.oc-sdk-input:focus, .oc-sdk-input:focus-visible { box-shadow: inset 0 0 0 2px ${ht}; }
.oc-sdk-field[data-invalid="true"] .oc-sdk-input { box-shadow: inset 0 0 0 1px ${f("status-error","red")}; }
.oc-sdk-field[data-invalid="true"] .oc-sdk-input:focus { box-shadow: inset 0 0 0 2px ${f("status-error","red")}; }
.oc-sdk-input[data-mono="true"] { font-family: ${ze}; }

.oc-sdk-search { position: relative; min-width: 0; }
.oc-sdk-search .oc-sdk-input { padding-left: 34px; padding-right: 34px; }
.oc-sdk-search-icon { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); color: ${E}; pointer-events: none; }
.oc-sdk-search[data-active="true"] .oc-sdk-search-icon { color: ${M}; }
.oc-sdk-search-clear { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); display: none; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: 6px; color: ${E}; }
.oc-sdk-search[data-active="true"] .oc-sdk-search-clear { display: inline-flex; }
.oc-sdk-search-clear:hover { background: ${v}; color: ${Z}; }

.oc-sdk-select { position: relative; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.oc-sdk-trigger { display: inline-flex; align-items: center; gap: 6px; width: 100%; min-width: 0; height: 32px; padding: 0 8px 0 10px; border: 1px solid ${w}; border-radius: 6px; background: ${pe}; color: ${Q}; font-size: 0.8125rem; text-align: left; transition: background 150ms ease-out; }
.oc-sdk-trigger:hover { background-image: linear-gradient(${v}, ${v}); }
.oc-sdk-trigger[aria-expanded="true"] { background-image: linear-gradient(${D}, ${D}); }
.oc-sdk-trigger-value { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.oc-sdk-trigger-value[data-empty="true"] { color: ${E}; }
.oc-sdk-trigger-chevron { flex: 0 0 auto; color: ${E}; }
.oc-sdk-popup { --surface-foreground: ${Q}; position: fixed; z-index: 50; display: flex; flex-direction: column; gap: 2px; min-width: 160px; max-width: calc(100vw - 16px); max-height: min(320px, calc(100vh - 16px)); overflow: auto; padding: 4px; border: 1px solid ${h(w,60)}; border-radius: 12px; background: ${pe}; color: ${Q}; box-shadow: 0 8px 24px ${h(Z,12)}; }
.oc-sdk-popup-search { flex: 0 0 auto; padding: 2px 2px 4px; }
.oc-sdk-popup-search .oc-sdk-input { height: 32px; font-size: 0.8125rem; }
.oc-sdk-option { display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 8px; border-radius: 8px; font-size: 0.8125rem; text-align: left; }
.oc-sdk-option[data-active="true"] { background: ${v}; }
.oc-sdk-option[aria-selected="true"] { background: ${Pe}; color: ${De}; }
.oc-sdk-option[data-destructive="true"] { color: ${Be}; }
.oc-sdk-option[data-destructive="true"][data-active="true"] { background: ${h(f("status-error","red"),10)}; }
.oc-sdk-option-label { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.oc-sdk-option-hint { flex: 0 0 auto; font-size: 0.75rem; color: ${E}; }
.oc-sdk-option-check { flex: 0 0 auto; width: 12px; }
.oc-sdk-popup-empty { padding: 8px; font-size: 0.8125rem; color: ${E}; }

.oc-sdk-check { display: inline-flex; align-items: flex-start; gap: 8px; width: 100%; text-align: left; }
.oc-sdk-check-box { flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 14px; height: 14px; margin-top: 3px; border: 1px solid ${w}; border-radius: 4px; color: ${M}; transition: border-color 150ms ease-out; }
.oc-sdk-check[aria-checked="true"] .oc-sdk-check-box { border-color: ${h(M,65,w)}; }
.oc-sdk-check-box > svg { display: none; }
.oc-sdk-check[aria-checked="true"] .oc-sdk-check-box > svg { display: block; }
.oc-sdk-check-thumb { flex: 0 0 auto; position: relative; width: 36px; height: 20px; border-radius: 9999px; background: ${w}; transition: background 150ms ease-out; }
.oc-sdk-check-thumb::after { content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 9999px; background: ${P}; transition: transform 150ms ease-out; }
.oc-sdk-check[aria-checked="true"] .oc-sdk-check-thumb { background: ${M}; }
.oc-sdk-check[aria-checked="true"] .oc-sdk-check-thumb::after { transform: translateX(16px); }
.oc-sdk-check:focus-visible { box-shadow: none; }
.oc-sdk-check:focus-visible .oc-sdk-check-box, .oc-sdk-check:focus-visible .oc-sdk-check-thumb { ${Et} }
.oc-sdk-check-text { display: flex; flex-direction: column; min-width: 0; }
.oc-sdk-check-label { font-size: 0.875rem; }
.oc-sdk-check-desc { font-size: 0.75rem; color: ${E}; }

.oc-sdk-tabs { display: inline-flex; gap: 2px; padding: 2px; border-radius: 10px; max-width: 100%; overflow: auto; }
.oc-sdk-tabs[data-track="true"] { background: ${h(Z,4)}; }
.oc-sdk-tab { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border: 1px solid transparent; border-radius: 8px; font-size: 0.8125rem; font-weight: 500; color: ${E}; white-space: nowrap; transition: color 150ms ease-out, background 150ms ease-out; }
.oc-sdk-tab:hover { color: ${Z}; }
.oc-sdk-tab[aria-selected="true"] { color: ${De}; background: ${Pe}; border-color: ${w}; }
.oc-sdk-tab-count { font-size: 0.75rem; font-variant-numeric: tabular-nums; color: ${E}; }

.oc-sdk-badge { display: inline-flex; align-items: center; padding: 1px 6px; border-radius: 9999px; font-size: 11px; font-weight: 500; line-height: 16px; white-space: nowrap; background: ${v}; color: ${E}; }
.oc-sdk-badge[data-tone] { color: var(--oc-sdk-tone-text, var(--oc-sdk-tone)); background: ${h("var(--oc-sdk-tone)",15)}; }

.oc-sdk-list { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.oc-sdk-row { display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 8px; border-radius: 6px; text-align: left; transition: background 120ms ease-out; }
.oc-sdk-row:hover, .oc-sdk-row[data-active="true"] { background: ${v}; }
.oc-sdk-row[aria-selected="true"] { background: ${Pe}; color: ${De}; }
.oc-sdk-row-lead { flex: 0 0 auto; width: 64px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ${ze}; font-size: 0.75rem; color: ${E}; }
.oc-sdk-row-main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }
.oc-sdk-row-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.oc-sdk-row-sub { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.75rem; color: ${E}; }
.oc-sdk-row-meta { flex: 0 0 auto; font-size: 0.75rem; font-variant-numeric: tabular-nums; color: ${E}; }
.oc-sdk-row[aria-selected="true"] .oc-sdk-row-lead, .oc-sdk-row[aria-selected="true"] .oc-sdk-row-sub, .oc-sdk-row[aria-selected="true"] .oc-sdk-row-meta { color: inherit; opacity: .75; }
.oc-sdk-list-empty { padding: 16px 8px; text-align: center; font-size: 0.8125rem; color: ${E}; }

.oc-sdk-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; padding: 40px 16px; text-align: center; }
.oc-sdk-empty-title { margin: 0; font-size: 0.8125rem; font-weight: 600; }
.oc-sdk-empty-body { margin: 0; max-width: 32rem; font-size: 0.8125rem; color: ${E}; }
.oc-sdk-empty-action { margin-top: 12px; }

@keyframes oc-sdk-spin { to { transform: rotate(360deg); } }
.oc-sdk-spinner { display: inline-flex; align-items: center; gap: 8px; font-size: 0.8125rem; color: ${E}; }
.oc-sdk-spinner-ring { width: 16px; height: 16px; border: 2px solid ${w}; border-top-color: ${M}; border-radius: 9999px; animation: oc-sdk-spin .8s linear infinite; }
.oc-sdk-spinner[data-size="sm"] .oc-sdk-spinner-ring { width: 12px; height: 12px; }

.oc-sdk-banner { display: flex; align-items: flex-start; gap: 12px; padding: 8px 12px; border: 1px solid ${h("var(--oc-sdk-tone)",40)}; border-radius: 8px; background: ${h("var(--oc-sdk-tone)",10)}; }
.oc-sdk-banner-text { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.oc-sdk-banner-title { font-size: 0.8125rem; font-weight: 500; color: var(--oc-sdk-tone-text, var(--oc-sdk-tone)); }
.oc-sdk-banner-body { font-size: 0.8125rem; color: ${E}; }
.oc-sdk-banner-action { flex: 0 0 auto; }

.oc-sdk-separator { display: flex; align-items: center; gap: 8px; width: 100%; margin: 8px 0; font-size: 0.75rem; color: ${E}; }
.oc-sdk-separator::before, .oc-sdk-separator::after { content: ""; flex: 1 1 auto; height: 1px; background: ${h(w,40)}; }
.oc-sdk-separator[data-labeled="false"]::after { display: none; }
.oc-sdk-popup > .oc-sdk-separator { margin: 4px 0; }

.oc-sdk-progress { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.oc-sdk-progress-label { display: flex; justify-content: space-between; font-size: 0.75rem; color: ${E}; font-variant-numeric: tabular-nums; }
.oc-sdk-progress-track { height: 6px; border-radius: 9999px; background: ${w}; overflow: hidden; }
.oc-sdk-progress-fill { height: 100%; border-radius: 9999px; background: var(--oc-sdk-tone, ${M}); transform-origin: left; transition: transform 200ms ease-out; }

.oc-sdk-menu { position: relative; display: inline-flex; }

.oc-sdk-text { white-space: pre-wrap; overflow-wrap: anywhere; }
.oc-sdk-text a { color: ${Xe}; text-decoration: underline; text-underline-offset: 2px; }
.oc-sdk-text img { display: block; max-width: 100%; margin: 8px 0; border-radius: 8px; border: 1px solid ${h(w,60)}; }
`;var It="/api/guests/",Gt=[{id:"terminal",kind:"page",target:"terminal"},{id:"data-manager",kind:"page",target:"data-manager"},{id:"account",kind:"page",target:""},{id:"in-app-terminal",kind:"surface",target:"terminal"},{id:"in-app-files",kind:"surface",target:"file"}],Nt=()=>{let e=window.location.href,n=e.indexOf(It);if(n>0){let l=e.slice(0,n);return l.endsWith("/")?l:`${l}/`}try{let l=new URL(e);if(l.protocol==="http:"||l.protocol==="https:")return`${l.origin}/`}catch{}return null},$t=e=>{let n=Nt();if(!n)return null;try{return e===""?new URL("/",n).href:new URL(e,n).href}catch{return null}},V=e=>{let n=document.querySelector("#diagnostic");n&&(n.hidden=!1,n.textContent=e)},kt=e=>{try{window.open(e,"_blank","noopener,noreferrer")}catch{V(`Open ${e} manually.`)}},Tt=()=>{let e=document.querySelector("#root");if(!e)return;let n=window.parent===window;if(n){let u=document.querySelector("#standalone");u&&(u.hidden=!1)}let l=null;if(!n)try{l=$e()}catch(u){V(u instanceof Error?u.message:"Could not connect to OpenChamber.")}let G=u=>{if(u.kind==="surface"){if(!l){V("In-app panels are only available inside OpenChamber.");return}l.openSurface(u.target).catch(x=>{V(x instanceof Error?x.message:"Could not open that panel.")});return}let T=$t(u.target);if(!T){V("Could not resolve this environment\u2019s address.");return}if(!l){kt(T);return}l.openUrl(T).catch(()=>{kt(T)})};for(let u of e.querySelectorAll(".row")){let T=u.dataset.id??"",x=Gt.find(R=>R.id===T);x&&u.addEventListener("click",()=>G(x))}if(!l)return;let C=!1;l.onReady(u=>{C=!0,u.surface!=="background"&&(He(u,document.documentElement),document.documentElement.dataset.ocConnected="true")}),window.setTimeout(()=>{C||V(`No handshake from OpenChamber after 2.5s (frame: ${window.location.href.slice(0,160)}).`)},2500)};document.readyState==="loading"?document.addEventListener("DOMContentLoaded",Tt,{once:!0}):Tt();})();
