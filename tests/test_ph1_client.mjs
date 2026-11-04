import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'https://unit.invalid/#talk'});
for(const key of ['window','document','location','localStorage','history','FileReader'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
let blobs=0,revoked=0;URL.createObjectURL=()=>`blob:photo-${++blobs}`;URL.revokeObjectURL=()=>revoked++;
const tick=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
const response=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
const calls=[];let stream;
const cfg={photos:{enabled:true,reason:'',max_count:4,max_bytes:1024,notice_seen:false},tts:null,early:{notify:true},ai_ready:true,connection_verified:true,autonomy:{opening:'off'}};
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,body,method:options.method});
  if(route==='/api/partner')return response({partner:{id:'yuki',name:'ユキ'}});
  if(route==='/api/settings')return response(cfg);
  if(route==='/api/look')return response({expressions:[]});
  if(route==='/api/sessions')return response(body?{id:'s'}:[]);
  if(route==='/api/proactive/opening')return response({speak:false});
  if(route==='/api/sessions/s/messages/stream')return new Response(new ReadableStream({start(controller){stream=controller;}}));
  if(route.startsWith('/api/photos/'))return response({deleted:true});
  throw Error('Unexpected endpoint: '+route);
};
const file=()=>new window.File(['photo'], 'photo.png', {type:'image/png'});
function transfer(kind,files){const event=new window.Event(kind,{bubbles:true,cancelable:true});Object.defineProperty(event,kind==='paste'?'clipboardData':'dataTransfer',{value:{files,types:['Files']}});return event;}
function stage(type,data){stream.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`));}
await import('../cbs/web/static/user.js');await tick();
const $=id=>document.getElementById(id);
assert(!$('photo-add').disabled);
let pickerClicks=0;$('photo-files').click=()=>pickerClicks++;$('photo-add').click();assert.equal(pickerClicks,1);
Object.defineProperty($('photo-files'),'files',{configurable:true,value:[file()]});$('photo-files').onchange();await tick();
assert.equal(document.querySelectorAll('.photo-pending').length,1);
assert($('photo-status').textContent.includes('写真は画像を読むモデルの提供元に送られます'));
$('message').dispatchEvent(transfer('paste',[file()]));await tick();
window.dispatchEvent(transfer('drop',[file()]));await tick();
assert.equal(document.querySelectorAll('.photo-pending').length,3);
window.dispatchEvent(transfer('drop',[file(),file()]));await tick();
assert.equal(document.querySelectorAll('.photo-pending').length,3);assert($('error').textContent.includes('4枚'));
document.querySelector('.photo-pending button').click();assert.equal(document.querySelectorAll('.photo-pending').length,2);assert(revoked);
assert(!$('message').required,'photo-only send permitted');
$('composer').dispatchEvent(new window.Event('submit',{cancelable:true}));
for(let i=0;i<30&&!stream;i++)await new Promise(r=>setTimeout(r,5));
assert(stream);assert($('photo-add').disabled);assert($('message').disabled);
assert.equal(document.querySelectorAll('.photo-pending').length,0,'photos leave the input together with the text');assert.equal(document.querySelectorAll('.photo-sending img').length,2,'and show in your bubble right away');
const payload=calls.find(c=>c.route.endsWith('/messages/stream')).body;
assert.equal(payload.content,'');assert.deepEqual(payload.photos,['cGhvdG8=','cGhvdG8=']);
assert(!calls.some(c=>c.route==='/api/photos/notice'),'notice adds no foreground request');
stage('stage',{stage:'vision'});await tick();assert.equal(document.querySelectorAll('.thinking').length,0,'UX1 delays the indicator, including vision');
const photo={id:'a'.repeat(32),description:'見えたこと: <img src=x onerror=alert(1)>',width:1600,height:800};
stage('reply',{turn_id:7,assistant_text:'写真を見せてくれてありがとう。',assistant_at:'2026-10-21T12:00:00+09:00',expression:'neutral',photos:[photo]});await tick();
assert(!$('send').disabled);assert(!$('photo-add').disabled);assert.equal(document.querySelectorAll('.photo-pending').length,0);
assert.equal(document.querySelectorAll('.photo-sending').length,0,'saved photos replace the local copies');
const thumbnail=document.querySelector('.photo-gallery img');assert(thumbnail);assert(thumbnail.src.includes('character_id=yuki'));assert.equal(thumbnail.loading,'lazy');
assert.equal(document.querySelectorAll('.photo-gallery img').length,1,'description HTML escaped');
assert(document.querySelector('.photo-gallery details').textContent.includes('<img'));
await document.querySelector('.photo-gallery button').onclick();await tick();assert.equal(document.querySelectorAll('.photo-gallery img').length,0);
assert.equal(calls.find(c=>c.route==='/api/photos/'+photo.id).method,'DELETE');
stream.close();await tick();
// Notice stays dismissed on a subsequent attachment in this browser session.
$('message').dispatchEvent(transfer('paste',[file()]));await tick();assert(!$('photo-status').textContent.includes('提供元'));
// Reenter the talk view with no vision role; all input routes fail closed.
cfg.photos={enabled:false,reason:'写真を読むモデルが設定されていません。',notice_seen:true};
window.dispatchEvent(new window.HashChangeEvent('hashchange'));await tick();
assert($('photo-add').disabled);assert($('photo-status').textContent.includes('設定されていません'));
const before=document.querySelectorAll('.photo-pending').length;window.dispatchEvent(transfer('drop',[file()]));await tick();assert.equal(document.querySelectorAll('.photo-pending').length,before);
// Theme and phone layout use existing shared variables, with bounded thumbnails.
const css=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');
assert(css.includes('.photo-gallery'));assert(css.includes('@media(max-width:480px)'));assert(css.includes('var(--line)'));
console.log('PASS PH1 offline DOM: picker, paste/drop, count, remove, photo-only payload, one notice, no extra wait, vision stage, early input restore, scoped thumbnails/deletion, escaped description, disabled reason, theme/phone rules');
