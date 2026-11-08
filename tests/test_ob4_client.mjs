import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<nav id="menu"></nav><main id="screen"></main><p id="error" hidden></p>',{url:'http://127.0.0.1:9987/wizard'});
for(const k of ['window','document','location','history','localStorage','sessionStorage'])globalThis[k]=dom.window[k];
let scrolls=0;window.scrollTo=(x,y)=>{assert.equal(x,0);assert.equal(y,0);scrolls++;};
window.__TAURI__={core:{invoke:async command=>command==='resident_context'?{token:'fake-desktop'}:{}},event:{listen:async()=>{}}};
const root=document.getElementById('screen'),calls=[],opened=[];
const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json;from pathlib import Path;from cbs.config import load_config;print(json.dumps(load_config(data_dir=Path('.tmp/ob4/config')).model_dump()))"],{encoding:'utf8'}));
let cfg={...base,keys:{},advanced_presets:base.advanced.presets,onboarding:base.onboarding,ai_ready:false,connection_verified:false},completed=0;
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):undefined;calls.push({route,body,headers:options.headers});let data;
  if(route==='/api/settings'){if(body?.llm)Object.assign(cfg.llm,body.llm);data=cfg;}
  else if(route==='/api/onboarding/state')data={consent:{at:'test'}};
  else if(route.startsWith('/api/onboarding/'))data={text:'# '+route.split('/').at(-1)};
  else if(route==='/api/openrouter/status')data={completed};
  else if(route==='/api/external/open'){opened.push(body.url);data={opened:true};}
  else if(route==='/api/import')data=[{id:'batch',created_at:'2026-11-01',status:'completed',total:1,completed:1,percent:100}];
  else throw new Error('Unexpected fake request: '+route);
  return new Response(JSON.stringify(data));
};
const tick=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
const {sequence}=await import('../cbs/web/static/onboarding.js');
let ready;
const order=sequence(root,[{id:'look',explanation:true,pending:true,render:p=>p.append('first'),enter:({setReady})=>ready=setReady},{id:'next',render:p=>p.append('second')}]);
await order.start();
assert.deepEqual([...root.querySelectorAll('.wizard-actions>.btn')].map(b=>b.textContent),['飛ばす','次へ']);
assert(root.querySelector('.primary').disabled);ready(true);await root.querySelector('.primary').onclick();
assert.deepEqual([...root.querySelectorAll('.wizard-actions>.btn')].map(b=>b.textContent),['戻る','次へ']);
await root.querySelector('.wizard-actions>.btn').onclick();assert(root.textContent.includes('first'));assert(scrolls>=3);
// The same equal flex shares apply at 375px in light and dark. No browser launch.
const css=document.createElement('style');css.textContent=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');document.head.append(css);
document.body.classList.add('onboarding');Object.defineProperty(window,'innerWidth',{value:375,configurable:true});
for(const theme of ['light','dark']){
  document.documentElement.dataset.theme=theme;
  for(const count of [1,2,3]){
    const row=document.createElement('div');row.className='wizard-actions';
    for(let i=0;i<count;i++){const b=document.createElement('button');b.className='btn'+(i===count-1?' primary':'');b.textContent='長いボタンの名前';row.append(b);}root.append(row);
    const shares=[...row.children].map(b=>window.getComputedStyle(b).flex);assert(shares.every(s=>s==='1 1 0px'),shares.join(','));row.remove();
  }
}
const {onboardingAI}=await import('../cbs/web/static/onboarding-ai.js');
void onboardingAI(root);await tick();
async function next(){const b=root.querySelector('.wizard-actions>.primary');assert(!b.disabled);await b.onclick();await tick();}
await next();await next();await next();assert(root.textContent.includes('OpenRouter と連携する'));assert(root.querySelector('.primary').disabled);
let login=[...root.querySelectorAll('button')].find(b=>b.textContent==='OpenRouter と連携する');assert(!login.closest('.wizard-actions'));
await login.onclick();await tick();assert.equal(opened.at(-1),'http://127.0.0.1:9987/oauth/openrouter/start');assert.equal(calls.find(c=>c.route==='/api/external/open').headers['X-Awalia-Desktop'],'fake-desktop');
assert(root.querySelector('.primary').disabled);assert(root.textContent.includes('連携を待っています'));
assert(root.textContent.includes('購入済みのクレジットが必要です'));assert(root.textContent.includes('もう一度押せます'));
assert.equal(root.querySelector('a[href="https://openrouter.ai/settings/credits"]').target,'_blank');assert(!login.disabled);
completed++;cfg.keys.openrouter='set';window.dispatchEvent(new window.Event('focus'));await tick();
assert(root.textContent.includes('連携できました'));assert(login.disabled);assert.equal(login.nextElementSibling.getAttribute('role'),'status');assert(login.nextElementSibling.classList.contains('success'));assert(!root.querySelector('.primary').disabled);assert(!root.textContent.includes('会話を保存しない提供元'));
await next();assert(root.textContent.includes('会話を保存しない提供元'));
// Dynamic target=_blank links use the same resident endpoint.
const link=document.createElement('a');link.href='https://example.org/key';link.target='_blank';link.textContent='キー';root.append(link);
link.dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true}));await tick();assert.equal(opened.at(-1),'https://example.org/key');
const {openExternal}=await import('../cbs/web/static/ui.js');
delete window.__TAURI__;let tab;
window.open=(...args)=>tab=args;await openExternal('/oauth/openrouter/start');assert.deepEqual(tab,['http://127.0.0.1:9987/oauth/openrouter/start','_blank','noopener,noreferrer']);
// Settings login also stays in place, including an already-set key reconnect.
const {connectionPanel}=await import('../cbs/web/static/settings.js');root.replaceChildren();let refreshed=0;connectionPanel(root,cfg,undefined,{onConnected:()=>refreshed++});
login=[...root.querySelectorAll('button')].find(b=>b.textContent==='OpenRouter でつなぎ直す');assert(login);
await login.onclick({preventDefault(){}});await tick();assert(root.textContent.includes('連携を待っています'));
completed++;document.dispatchEvent(new window.Event('visibilitychange'));await tick();assert(root.textContent.includes('連携できました'));
// After connecting, the button offers a reconnect and the screen refreshes warnings computed before it.
assert.equal(login.textContent,'OpenRouter でつなぎ直す');assert.equal(refreshed,1);
assert(!document.getElementById('error').textContent);
const {openImport}=await import('../cbs/web/static/import.js');root.replaceChildren();let importReady=false;
await openImport(root,{onboarding:true,onContinue:()=>{importReady=true;},onPending:()=>{importReady=false;}});
assert(importReady);assert(root.textContent.includes('会話を覚えました'));assert(root.querySelector('input[type=file]'),'completed import remains on the same screen');
assert(![...root.querySelectorAll('button')].some(b=>b.textContent==='人物像づくりへ'));
await root.querySelector('input[type=file]').onchange();assert(!importReady,'new file selection resets readiness');
console.log('PASS OB4 DOM: equal rows in light/dark at 375px, order, scroll, pending next, resident links, browser tab, OAuth wait/focus/reconnect');
