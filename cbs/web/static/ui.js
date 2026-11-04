import {preference} from './theme.js';
import {desktopHeaders,isResident} from './resident.js';
export const $ = id => document.getElementById(id);
export function node(tag, text, className) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (className) e.className = className;
  if(className?.split(" ").includes("warn"))e.prepend(icon("warning"));
  return e;
}

export const displayDefaults = {split_replies:true,delay_user:false,read_seconds:1.2,typing_seconds:2.5,user_delay_seconds:1,bubble_max_seconds:1.5,split_min_chars:50,split_three_chars:140};
// Boundaries are purely typographic. Keep every character, including whitespace.
export function replyParts(text, enabled=true, limits=displayDefaults) {
  if(!enabled||[...text].length<(limits.split_min_chars??displayDefaults.split_min_chars))return [text];
  const units=[];let start=0;
  for(const boundary of text.matchAll(/(?:[。！？!?]+[」』”’）)"']*|\.[」』”’）)"']*(?=\s|$)|\n+)\s*/gu)){
    const end=boundary.index+boundary[0].length;units.push(text.slice(start,end));start=end;
  }
  if(start<text.length)units.push(text.slice(start));
  if(units.length<2)return [text];
  const count=Math.min(text.length>=(limits.split_three_chars??displayDefaults.split_three_chars)?3:2,units.length),parts=[];
  let rest=units.slice();
  for(let left=count;left>1;left--){
    const target=rest.join('').length/left;let length=0,best=1,distance=Infinity;
    for(let i=0;i<=rest.length-left;i++){length+=rest[i].length;const d=Math.abs(length-target);if(d<distance){distance=d;best=i+1;}}
    parts.push(rest.splice(0,best).join(''));
  }
  parts.push(rest.join(''));return parts;
}

// First bubble is synchronous; later bubbles belong to this DOM instance only.
export function replyBubbles(content,text,cfg,{animate=false,onAppend=()=>{}}={}) {
  const parts=replyParts(text,cfg.split_replies,cfg);content.classList.add('reply-bubbles');content.classList.remove('text');content.replaceChildren();
  let index=0,timer;
  function append(){const previousHeight=document.documentElement.scrollHeight;content.append(paragraphText(parts[index++]));onAppend(previousHeight);if(index<parts.length)timer=setTimeout(append,Math.min(cfg.bubble_max_seconds*1000,[...parts[index-1]].length*18));}
  if(animate)append();else for(const part of parts)content.append(paragraphText(part));
  return ()=>clearTimeout(timer);
}

export function paragraphText(text){
  const block=node('div',undefined,'text');
  for(const part of text.split(/(\n[ \t]*\n+)/u)){if(/^\n[ \t]*\n/u.test(part)){const gap=node('span',part,'paragraph-break');gap.setAttribute('aria-hidden','true');block.append(gap);}else if(part)block.append(node('p',part));}
  return block;
}
export function chatBand(parent,name,expressions,expression='neutral'){
  const band=node('header',undefined,'chat-band'),info=node('div',undefined,'chat-band-info'),summary=node('p',undefined,'chat-summary'),link=node('a',undefined,'btn quiet icon-button');
  summary.hidden=true;info.append(node('h2',name),summary);link.href='#note';link.setAttribute('aria-label','ノートを開く');link.append(icon('note'));band.append(face(name,expression,expressions),info,link);parent.prepend(band);
  return {expression(value){band.querySelector('.face').replaceWith(face(name,value,expressions));},summary(value){summary.textContent=(value||'').trim().match(/^[^。！？!?\n]+[。！？!?]?/u)?.[0]||'';summary.hidden=!summary.textContent;}};
}

export function typingIndicator(content,name) {
  const dots=node('span','…','typing-dots');dots.setAttribute('aria-hidden','true');
  const label=node('span',`${name}が考えています…`,'sr-only');label.setAttribute('role','status');label.setAttribute('aria-live','polite');
  content.replaceChildren(dots,label);
}

export function waitingPresentation(cfg, callbacks) {
  const state={user:!cfg.delay_user,read:false,typing:false,done:false};const timers=new Map();
  const paint=()=>callbacks.paint(state);
  function later(seconds,key){timers.set(key,setTimeout(()=>{state[key]=true;paint();},seconds*1000));}
  // Read and typing count from when your own bubble appears, so each step gets its own beat while the reply is made.
  const shown=state.user?0:cfg.user_delay_seconds;
  if(!state.user)later(shown,'user');later(shown+cfg.read_seconds,'read');later(shown+cfg.typing_seconds,'typing');paint();
  return {state,paint,finish(success=false){state.done=true;state.user=true;state.typing=false;for(const [key,timer]of timers)if(key!=='read'||!success)clearTimeout(timer);paint();}};
}
// The server's current partner (data/settings.toml) decides; a per-browser choice would
// differ between the resident window and a browser. ?character= stays for the dev screen.
export let partnerId = new URLSearchParams(location.search).get('character') || '';
export function selectPartner(id) { partnerId = id; }
export function scoped(path) {
  return partnerId && path.startsWith('/api/') && !path.startsWith('/api/bootstrap') && !path.startsWith('/api/characters')
    ? `${path}${path.includes('?') ? '&' : '?'}character_id=${encodeURIComponent(partnerId)}` : path;
}
export async function api(path, body, method = 'POST', signal) {
  const headers=await desktopHeaders();
  const r = await fetch(scoped(path), body === undefined ? {...(Object.keys(headers).length?{headers}:{}),...(signal?{signal}:{})} : {method, headers: {'Content-Type':'application/json',...headers}, body:JSON.stringify(body),...(signal?{signal}:{})});
  const data = await r.json();
  if (!r.ok) throw apiError(data);
  return data;
}
export function apiError(data) {
  return Object.assign(new Error(data.message || (typeof data.detail === 'string' ? data.detail : '入力を確認してください。')),
    {kind:data.kind,provider:data.provider,status_code:data.status_code,link:data.link});
}
const failureLinks = new Set(['https://openrouter.ai/settings/credits','https://openrouter.ai/settings/keys',
  'https://platform.openai.com/settings/organization/billing','https://console.anthropic.com/settings/billing','https://aistudio.google.com/']);
export function failureText(parent, error) {
  parent.replaceChildren(node('span',error.message),icon('error'));
  if(error.link && failureLinks.has(error.link.url)) {
    const a=node('a',error.link.label);a.href=error.link.url;a.target='_blank';a.rel='noopener noreferrer';parent.append(' ',a);
  }
}
export function report(error) { failureText($('error'),error); $('error').hidden = false; }
export function action(parent, text, fn, primary = false) {
  const b = node('button', text, `btn${primary ? ' primary' : ''}`); b.type = 'button';
  b.onclick = async () => {
    b.disabled = true; if ($('error')) $('error').hidden = true;
    try { await fn(); } catch(e) { report(e); } finally { b.disabled = false; }
  };
  parent.append(b); return b;
}
export function field(parent, label, value, change, type = 'input') {
  const l = node('label', label, 'f'), e = node(type);
  if (type === 'textarea') e.rows = 3;
  e.value = value ?? ''; e.addEventListener('input', () => change(e.value)); l.append(e); parent.append(l); return e;
}
export function select(parent, label, options, value, change) {
  const e = field(parent, label, '', change, 'select');
  // An array of [value, label] pairs keeps the given order (object keys like '1' sort before '.5').
  for (const [key, text] of Array.isArray(options) ? options : Object.entries(options)) { const o = node('option', text); o.value = key; e.append(o); }
  e.value = value; return e;
}
export function card(parent, title) {
  const c = node('section', undefined, 'card set'); if (title) c.append(node('h3', title)); parent.append(c); return c;
}
export function face(name, expression = 'neutral', expressions = [], cid = partnerId) {
  const e = node('span', [...name][0] || '○', 'face'); e.setAttribute('aria-hidden', 'true');
  const slot = expressions.includes(expression) ? expression : expressions.includes('neutral') ? 'neutral' : null;
  if (slot && cid) {
    const img = node('img'); img.alt = ''; img.src = `/looks/${encodeURIComponent(cid)}/${slot}?v=${Date.now()}`;
    img.onerror = () => { if (slot !== 'neutral' && !img.dataset.fallback) { img.dataset.fallback='1'; img.src=`/looks/${encodeURIComponent(cid)}/neutral`; } else img.remove(); };
    e.append(img);
  }
  return e;
}
export function shell(active = 'talk', name = '') {
  const nav = $('menu'); const brand=node('div',undefined,'brand'),logo=node('img');logo.src='/static/brand/symbol.svg';logo.alt='Awalia';logo.width=44;logo.height=30;brand.append(logo,node('span','Awalia','brand-word'));brand.setAttribute('aria-label','Awalia');nav.replaceChildren(brand);
  for (const [id, label, symbolName] of [['talk','会話','talk'],['note','ノート','note'],['settings','設定','settings']]) {
    const a = node('a'); a.href = `/#${id}`; a.dataset.tab = id;
    const symbol=icon(symbolName);symbol.classList.add('nav-icon'); symbol.setAttribute('aria-hidden','true'); a.append(symbol,node('span',label));
    if(id === active) a.setAttribute('aria-current','page'); nav.append(a);
  }
  if (preference('dev') === 'true') { const a=node('a','開発者画面'); a.href=`/dev${partnerId ? '?character='+encodeURIComponent(partnerId) : ''}`; a.className='dev-link'; nav.append(a); }
  if(name) { const p=node('div',undefined,'partner'); p.append(face(name,'neutral',['neutral']),node('span',name)); nav.append(p); }
}
export function plainMarkdown(text) {
  return text.replace(/^\s{0,3}#{1,6}\s+/gm,'').replace(/\s+#+\s*$/gm,'')
    .replace(/^\s*(?:[-*+・]|\d+[.)])\s+/gm,'')
    .replace(/(\*\*|__)(.+?)\1/g,'$2').replace(/([*_])([^\n]+?)\1/g,'$2');
}
export function confirmDialog(message, label = '変更して作り直す') {
  return new Promise(resolve => {
    const d=node('dialog'), p=node('p',message), buttons=node('div',undefined,'row');
    function close(value) { d.close(); d.remove(); resolve(value); }
    action(buttons,'戻る',()=>close(false)); action(buttons,label,()=>close(true),true);
    d.append(p,buttons); d.oncancel=e=>{e.preventDefault();close(false);}; document.body.append(d);d.showModal();
  });
}

// OAuth starts in the destination browser so its own PKCE cookie returns there.
export async function openExternal(url){
  const target=new URL(url,location.href);
  if(!['http:','https:'].includes(target.protocol))throw new Error('http(s) の URL を指定してください。');
  if(isResident())await api('/api/external/open',{url:target.href});
  else window.open(target.href,'_blank','noopener,noreferrer');
}
// Delegation also covers links added after startup, in settings and the wizard.
document.addEventListener('click',event=>{
  const link=event.target.closest?.('a[target="_blank"]');
  if(!link||!isResident())return;
  const url=new URL(link.href,location.href);if(!['http:','https:'].includes(url.protocol))return;
  event.preventDefault();void openExternal(url.href).catch(report);
});

// UV2: shared drawn icons; decorative, with the control's text supplying its name.
export function icon(name){
  const paths={mail:'M3 5h18v14H3Z M3 5l9 7 9-7',warning:'M12 3 2 21h20Z M12 9v5 M12 18h.01',check:'M5 12l4 4L19 6',photo:'M3 4h18v16H3z M3 16l6-6 5 5 3-3 4 4 M16 8h.01',mic:'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0Z M6 10v2a6 6 0 0 0 12 0v-2 M12 18v4 M8 22h8',send:'M12 20V4 M5 11l7-7 7 7',speaker:'M4 9h4l5-4v14l-5-4H4Z M17 8q5 4 0 8',stop:'M6 6h12v12H6Z',talk:'M4 4h16v12H9l-5 4Z',note:'M6 3h13v18H6z M3 7h3 M3 12h3 M3 17h3 M10 7h5 M10 11h5',settings:'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1Z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',down:'m6 9 6 6 6-6',up:'m6 15 6-6 6 6',right:'m9 6 6 6-6 6',success:'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0 M7 12l3 3 7-7',error:'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0 M12 7v6 M12 17h.01',share:'M12 15V4 M8 8l4-4 4 4 M6 12v7h12v-7'};
  const e=document.createElementNS('http://www.w3.org/2000/svg','svg'),p=document.createElementNS(e.namespaceURI,'path');
  for(const [key,value]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.6','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false',class:'ui-icon'}))e.setAttribute(key,value);
  p.setAttribute('d',paths[name]||paths.right);e.append(p);return e;
}
// Compare labels only, never field values. Ignore question punctuation and allow
// a shorter trailing label (the question may name the subject explicitly).
// Context stays in its container: a sibling section cannot hide an unrelated label.
export function tidyComponents(root){
  const title=e=>[...e.childNodes].filter(n=>n.nodeType===3||!n.matches?.('input,select,textarea,.ui-icon,.accordion-summary')).map(n=>n.textContent).join('').replace(/[\uFF1F?]$/u,'').trim();
  function walk(parent,previous=''){
    for(const e of parent.children){
      if(e.hidden)continue;
      if(e.matches('summary')&&!e.querySelector('.ui-icon'))e.prepend(icon('right'));
      if(e.matches('h2,h3,summary,.accordion-row,legend,label.f')){
        let label=e;
        if(e.matches('label.f')){label=e.querySelector(':scope > .field-label');if(!label){label=node('span',undefined,'field-label');const texts=[...e.childNodes].filter(n=>n.nodeType===3);for(const n of texts)label.append(n);e.prepend(label);}}
        const text=title(label),duplicate=!!text&&(text===previous||previous.endsWith(text))&&e.matches('h3,legend,label.f');
        label.classList.toggle('sr-only',duplicate);label.classList.toggle('duplicate-heading',duplicate);
        previous=e.matches('h2,h3,summary,.accordion-row')?text:'';
        continue;
      }
      if(e.matches('input,select,textarea,button')){previous='';continue;}
      const next=walk(e,previous);previous=e.matches('section,[role=tabpanel]')?'':next;
    }
    return previous;
  }
  walk(root);
}
if(typeof MutationObserver!=='undefined'){
  // One pass per frame, and none for the chat log: streamed replies change it constantly and it holds no form headings.
  let queued=false;
  const refresh=()=>{queued=false;tidyComponents(document.getElementById('screen')||document.body);};
  const schedule=records=>{
    if(queued||records.every(r=>(r.target.nodeType===1?r.target:r.target.parentElement)?.closest?.('#messages')))return;
    queued=true;(window.requestAnimationFrame||setTimeout)(refresh);
  };
  new MutationObserver(schedule).observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['hidden']});
  refresh();
}
