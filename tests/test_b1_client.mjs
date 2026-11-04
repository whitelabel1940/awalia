import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<div id="root"></div><p id="error" hidden></p>',{url:'https://unit.invalid/'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
const calls=[];let model='local/ruri-v3-130m',state='missing';
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,body});
  const base={model:'ruri-v3-130m',choice:'ruri-v3-130m',completed:70000000,total:140032944,percent:50,eta_seconds:10};
  let data;
  if(route.endsWith('/plan'))data={...base,token:'quote',download_bytes:70032944};
  else if(route.endsWith('/cancel')){state='cancelled';data={cancelled:true};}
  else if(options.method==='POST'){assert.deepEqual(body,{choice:'ruri-v3-130m',token:'quote',confirmed:true});state='downloading';data={...base,status:state};}
  else data={...base,status:state};
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.querySelectorAll('button')[1].onclick();};
dom.window.HTMLDialogElement.prototype.close=function(){this.remove();};
const {embeddingDownload}=await import('../cbs/web/static/embedding-download.js');
const controls=embeddingDownload(document.getElementById('root'),()=>model);
const click=async text=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent===text);assert(b,text);await b.onclick();};
await controls.refresh();assert(document.body.textContent.includes('未取得'));assert(!calls.some(c=>c.body));
await assert.rejects(controls.requireReady(),/取得を完了/);
await click('記憶検索モデルを取得する');assert(document.body.textContent.includes('70.0 MB / 140.0 MB（50%）'));assert(document.body.textContent.includes('残り約1分'));
assert.equal(document.querySelector('progress').value,50);
await click('取得を取り消す');assert(document.body.textContent.includes('途中から再開できます'));
await click('途中から再開する');assert.equal(calls.filter(c=>c.route.endsWith('/plan')).length,2);
state='ready';await controls.refresh();await controls.requireReady();
model='gemini/model';await controls.refresh();assert(document.querySelector('section').hidden);
console.log('PASS B1 offline DOM: capacity confirmation, progress/ETA, cancel/resume, readiness before save');
