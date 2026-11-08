import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const dom=new JSDOM(read('../cbs/web/static/wizard.html'),{url:'http://unit.invalid/wizard'});
for(const k of ['window','document','location','localStorage','sessionStorage','history'])globalThis[k]=dom.window[k];
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
window.scrollTo=()=>{};
const commands=[],listeners=new Map();let installs=0,restarts=0,consent=false;
let native={currentVersion:'0.1.0',availableVersion:null,phase:'idle'};
window.__TAURI__={core:{invoke:async command=>{
  commands.push(command);
  if(command==='resident_context')return {};
  if(command==='resident_ui_ready')return;
  if(command==='resident_update_status')return native;
  if(command==='resident_update_install'){installs++;throw Error('fake install failure');}
  if(command==='resident_restore_restart'){restarts++;return;}
  assert.fail(command);
}},event:{listen:async(name,callback)=>{listeners.set(name,callback);}}};
window.confirm=()=>consent;
const cfg=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json;from pathlib import Path;from cbs.config import load_config;print(json.dumps(load_config(data_dir=Path('.tmp/r12/node-config')).model_dump()))"],{encoding:'utf8'}));
Object.assign(cfg,{keys:{},ai_ready:false,connection_verified:false});
let feed={unread_count:0,has_update:false,notices:[]},cancelled=0,preview;
const paths=[];
globalThis.fetch=async(path,options={})=>{
  paths.push(path);let data;
  if(path==='/api/partner')data={partner:null};
  else if(path==='/api/settings')data=cfg;
  else if(path==='/api/onboarding/state')data={consent:true};
  else if(path==='/api/news')data=feed;
  else if(path==='/api/restore/preview')data=preview;
  else if(path.endsWith('/stage'))data={status:'staged'};
  else if(path.startsWith('/api/restore/previews/')){cancelled++;data={};}
  else if(path.startsWith('/api/telemetry/'))data={};
  else assert.fail(path);
  return new Response(JSON.stringify(data));
};
const tick=async()=>{for(let i=0;i<30;i++)await new Promise(r=>setImmediate(r));};
await import('../cbs/web/static/wizard.js');await tick();
assert.equal(document.getElementById('error').textContent,'');
assert(commands.includes('resident_ui_ready'),'partnerless wizard must start native updater');
assert(!commands.includes('resident_preferences'),'conversation resident must not start without a partner');
assert(!paths.includes('/api/sessions'));
assert(document.body.classList.contains('onboarding'));
assert(!document.getElementById('onboarding-update'));
const update=payload=>listeners.get('resident-update')({payload});
update({...native,availableVersion:'0.1.1'});
let banner=document.getElementById('onboarding-update');
assert(banner&&banner.parentElement===document.getElementById('screen').parentElement);
assert.equal(banner.querySelector('h3').textContent,'バージョンアップのお知らせ');
assert(banner.textContent.includes('0.1.1 が出ています。'));
assert(banner.textContent.includes('入力中の内容は、更新前に確定してください。'));
await banner.querySelector('button').onclick();assert.equal(installs,0);
consent=true;await banner.querySelector('button').onclick();assert.equal(installs,1);
assert(banner.textContent.includes('fake install failure'));
update({...native,availableVersion:'0.1.1',phase:'downloading',downloaded:25,total:100});
banner=document.getElementById('onboarding-update');assert(banner.textContent.includes('25%'));assert(banner.querySelector('button').disabled);
update(native);assert(!document.getElementById('onboarding-update'));

const {newsScreen,refreshNews,newsEntries,paintNewsBadges}=await import('../cbs/web/static/news.js');
delete window.__TAURI__;
feed={...feed,has_update:true,latest:{version:'0.2.0',notes:'<b>notes</b>',url:'https://example.com/download'}};
await refreshNews();banner=document.getElementById('onboarding-update');assert(banner);
assert.equal(banner.querySelector('a').href,'https://example.com/download');
feed={...feed,has_update:false};await refreshNews();assert(!document.getElementById('onboarding-update'));
document.body.classList.remove('onboarding');
const screen=document.getElementById('screen');screen.replaceChildren();feed={...feed,has_update:true};
await newsScreen(screen);
assert.equal(screen.querySelector('h3').textContent,'バージョンアップのお知らせ');
assert(screen.textContent.includes('0.2.0 が出ています'));assert(!screen.textContent.includes('（今は'));
assert(screen.textContent.includes('<b>notes</b>'));assert(!screen.querySelector('b'));
window.__TAURI__={core:{invoke:async command=>command==='resident_update_status'?{currentVersion:'0.1.0',availableVersion:'0.2.0',phase:'idle'}:(installs++,undefined)}};
screen.replaceChildren();await newsScreen(screen);
assert(screen.textContent.includes('0.2.0 が出ています（今は 0.1.0）'));
await screen.querySelector('button').onclick();assert.equal(installs,2,'same install action on news card');
newsEntries('settings');
for(const unread_count of [0,2]){
  paintNewsBadges({unread_count,has_update:true});
  assert.equal(document.querySelectorAll('[data-news-link] .news-badge').length,2);
  assert([...document.querySelectorAll('.news-badge')].every(b=>b.textContent===(unread_count?'2':'新')));
}

delete window.__TAURI__;
const {restoreForm}=await import('../cbs/web/static/restore.js');
preview={id:'a'.repeat(32),created_at:'2026-10-20T00:00:00+09:00',turns:120,bytes:2048,partners:[
  {name:'ソラ',turns:120,first_at:'2026-10-20T12:00:00+09:00',selected:true,face:'data:image/png;base64,AAAA'},
  {name:'ソラ',turns:0,first_at:null,selected:false,face:null}
]};
screen.replaceChildren();const section=document.createElement('section');screen.append(section);
await restoreForm(section,{max_bytes:4096});
const file=section.querySelector('input[type=file]');Object.defineProperty(file,'files',{value:[{size:128}]});await file.onchange();
const click=async text=>{const button=[...section.querySelectorAll('button')].find(b=>b.textContent===text);assert(button,text);await button.onclick();};
await click('中身を確かめる');
const rows=[...section.querySelectorAll('.restore-partner')];assert.equal(rows.length,2);
assert.equal(rows[0].querySelector('img').src,preview.partners[0].face);
assert.equal(rows[0].querySelector('.badge').textContent,'いつもの相手');
assert(rows[0].textContent.includes('最初の会話 2026/10/20 · 会話 120 件'));
assert.equal(rows[1].querySelector('.face').textContent,'ソ');assert(!rows[1].querySelector('.badge'));assert(rows[1].textContent.includes('会話なし'));
assert(section.textContent.includes('会話 約120件 · 0.0 MB'));
assert(section.querySelector('.primary').textContent==='中身を確かめる');
assert([...section.querySelectorAll('.primary')].some(b=>b.textContent==='この記録に入れ替える'));
await click('入れ替えずに閉じる');assert.equal(cancelled,1);
assert(section.textContent.includes('入れ替えをやめました。今の記録はそのままです。'));
await click('中身を確かめる');await click('この記録に入れ替える');assert(section.textContent.includes('次の起動で記録を入れ替えます'));

const css=read('../cbs/web/static/screens.css');
assert(css.includes('.news-menu .news-badge,.news-mobile .news-badge{background:var(--accent-fill);color:var(--on-accent);border-radius:999px;'));
const luminance=hex=>hex.match(/[a-f0-9]{2}/gi).map(x=>parseInt(x,16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((sum,x,i)=>sum+x*[.2126,.7152,.0722][i],0);
const ratio=(a,b)=>{const values=[luminance(a),luminance(b)].sort((x,y)=>y-x);return (values[0]+.05)/(values[1]+.05);};
const ratios=[];
for(const accent of ['mauve','madder','evergreen','charcoal'])for(const theme of ['light','dark']){
  const selector=theme==='light'?`:root[data-accent=${accent}]`:`:root[data-theme=dark][data-accent=${accent}]`;
  const block=css.slice(css.indexOf(selector)).split('}')[0],fill=block.match(/--accent-fill:(#[a-f0-9]+)/)[1];
  const foreground=theme==='light'?css.match(/--on-accent:(#[a-f0-9]+)/)[1]:css.match(/:root\[data-theme=dark\][^}]+--on-accent:(#[a-f0-9]+)/)[1];
  const computed=ratio(fill,foreground);assert(computed>=4.5,`${accent}/${theme} ${computed}`);ratios.push(`${accent}/${theme}=${computed.toFixed(2)}:1`);
}
console.log('PASS R12 DOM: partnerless native ready; conditional setup updates; shared install/progress; update copy; restore identities/buttons; news badge classes');
console.log('Contrast: '+ratios.join(', '));
