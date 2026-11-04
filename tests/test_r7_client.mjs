import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<div id="error" hidden></div><main id="screen"></main><form id="composer"><textarea></textarea></form>',{url:'http://127.0.0.1/#talk'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const root=document.getElementById('screen'),calls=[],draws=[],opens=[],downloads=[],copies=[];
window.open=(...args)=>opens.push(args);
window.HTMLAnchorElement.prototype.click=function(){downloads.push({name:this.download,href:this.href});};
URL.createObjectURL=()=> 'blob:fake';URL.revokeObjectURL=()=>{};
let state={available:true,invite:true,name:'ソラ',day:'2026-11-04',hashtag:'Custom',url:'https://example.com/app'},look={cover:true,expressions:['neutral']};
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):undefined;calls.push({route,body});
  let value;
  if(route==='/api/share/state')value=state;
  else if(route==='/api/look')value=look;
  else if(['/api/share/start','/api/telemetry/share'].includes(route))value={ok:true};
  else assert.fail(`Unexpected offline request ${route}`);
  return new Response(JSON.stringify(value));
};
const tick=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
let pending=[];
globalThis.fakeCards={
  async startCard(args){draws.push({type:'start',args});if(pending.length)await pending.shift();return document.createElement('canvas');},
  async diaryCard(args){draws.push({type:'diary',args});if(pending.length)await pending.shift();return document.createElement('canvas');},
  async pngBlob(){return new Blob(['fake-png'],{type:'image/png'});}
};
// Replace only this test's imported drawing exports; the finished design stays untouched.
const fakeModule='data:text/javascript,'+encodeURIComponent('export const {startCard,diaryCard,pngBlob}=globalThis.fakeCards;');
const source=readFileSync(new URL('../cbs/web/static/share.js',import.meta.url),'utf8')
  .replace("'./ui.js'",JSON.stringify(new URL('../cbs/web/static/ui.js',import.meta.url).href))
  .replace("'./share-card.js'",JSON.stringify(fakeModule));
const {shareDialog,startShareNotice,composeUrl}=await import('data:text/javascript,'+encodeURIComponent(source));
const {selectPartner}=await import('../cbs/web/static/ui.js');selectPartner('fixture');
const text='日本語 & ? #\nhttps://example.com/?a=1&b=2';
for(const [service,url]of Object.entries({x:'https://x.com/intent/post?text=',bluesky:'https://bsky.app/intent/compose?text=',misskey:'https://misskey-hub.net/share/?text='}))assert.equal(composeUrl(service,text),url+encodeURIComponent(text));
const buttons=dialog=>[...dialog.querySelectorAll('button')];
const button=(dialog,text)=>buttons(dialog).find(b=>b.textContent===text);
const entry={day:'2026-10-09',summary:'<script>日記</script>',partner_comment:'また話そう',user_comment:'楽しかった'};
let release;pending=[new Promise(r=>release=r)];
const opening=shareDialog({where:'diary',entry});await tick();
let dialog=document.querySelector('dialog');
assert(dialog);assert(!dialog.querySelector('canvas'));assert(buttons(dialog).filter(b=>b.textContent!=='閉じる').every(b=>b.disabled));
await button(dialog,'X').onclick();assert.equal(opens.length,0,'cannot compose before the preview');
release();await opening;
assert(dialog.querySelector('canvas'));assert.equal(dialog.querySelector('canvas').getAttribute('role'),'img');
assert(dialog.querySelector('canvas').getAttribute('aria-label').includes(entry.summary));assert(!dialog.querySelector('script'));
assert.deepEqual([...dialog.firstChild.children].map(e=>e.className||e.tagName),['share-preview','warn share-caution','switch','switch','f','row','row','small muted','status','row']);
assert(dialog.querySelector('.share-caution').textContent.includes('公開したくない情報'));
assert.equal(button(dialog,'画像をコピー'),undefined);
let checks=dialog.querySelectorAll('input');assert.deepEqual([...checks].map(c=>c.checked),[true,false]);
assert.equal(draws.at(-1).args.partnerComment,entry.partner_comment);assert.equal(draws.at(-1).args.userComment,'');
assert.match(draws.at(-1).args.faceUrl,/^\/looks\/fixture\/neutral\?v=\d+$/);
checks[0].checked=false;checks[0].onchange();await tick();assert.equal(draws.at(-1).args.partnerComment,'');
checks[1].checked=true;checks[1].onchange();await tick();assert.equal(draws.at(-1).args.userComment,entry.user_comment);
assert.equal(dialog.querySelector('textarea').value,'ソラとのふたりの日記（10月9日）\n#Custom\nhttps://example.com/app');
dialog.querySelector('textarea').value=text;
for(const label of ['X','Bluesky','Misskey'])await button(dialog,label).onclick();
assert.deepEqual(opens.map(o=>o[0]),['x','bluesky','misskey'].map(s=>composeUrl(s,text)));
assert(opens.every(o=>o[1]==='_blank'&&o[2]==='noopener,noreferrer'));
await button(dialog,'画像を保存').onclick();assert.equal(downloads.at(-1).name,'awalia-diary-2026-10-09.png');
assert(calls.some(c=>c.route==='/api/telemetry/share'&&c.body.where==='diary'&&c.body.service==='save'));
assert(calls.filter(c=>c.route==='/api/telemetry/share').every(c=>Object.keys(c.body).length===2));
await button(dialog,'閉じる').onclick();assert(!document.querySelector('dialog'));
dialog=await shareDialog({where:'diary',entry:{...entry,user_comment:''}});assert.equal(dialog.querySelectorAll('input').length,1);
await button(dialog,'閉じる').onclick();
Object.defineProperty(navigator,'clipboard',{value:{write:async items=>copies.push(items)},configurable:true});
globalThis.ClipboardItem=class{constructor(value){this.value=value;}};
dialog=await shareDialog({where:'start'});assert.equal(dialog.querySelectorAll('input').length,0);
assert.equal(draws.at(-1).type,'start');assert.match(draws.at(-1).args.coverUrl,/\/looks\/fixture\/cover\?v=/);
assert.equal(dialog.querySelector('textarea').value,'AI のパートナー、ソラと話しはじめました。\n#Custom\nhttps://example.com/app');
await button(dialog,'画像をコピー').onclick();assert.equal(copies.length,1);assert.equal((await copies[0][0].value['image/png']).type,'image/png');
await button(dialog,'画像を保存').onclick();assert.equal(downloads.at(-1).name,'awalia-start-20261104.png');
await button(dialog,'閉じる').onclick();
look={cover:false,expressions:[]};dialog=await shareDialog({where:'start'});assert.equal(draws.at(-1).args.coverUrl,null);assert.equal(draws.at(-1).args.faceUrl,null);await button(dialog,'閉じる').onclick();
// Rapid redraws keep only the latest selection and cannot unlock a stale preview.
dialog=await shareDialog({where:'diary',entry});
checks=dialog.querySelectorAll('input');let first,second;pending=[new Promise(r=>first=r),new Promise(r=>second=r)];
checks[0].checked=false;checks[0].onchange();checks[1].checked=true;checks[1].onchange();
first();await tick();assert(!dialog.querySelector('canvas'));assert(button(dialog,'画像を保存').disabled);
second();await tick();assert(dialog.querySelector('canvas'));assert(dialog.querySelector('canvas').getAttribute('aria-label').includes(entry.user_comment));await button(dialog,'閉じる').onclick();
const composer=document.getElementById('composer');
await startShareNotice(composer);assert(composer.firstChild.classList.contains('share-invite'));
await button(composer,'今はしない').onclick();assert.deepEqual(calls.findLast(c=>c.route==='/api/share/start').body,{character_id:'fixture',action:'dismissed'});assert(!composer.querySelector('.share-invite'));
await startShareNotice(composer);await button(composer,'シェアする').onclick();assert.deepEqual(calls.findLast(c=>c.route==='/api/share/start').body,{character_id:'fixture',action:'shared'});await button(document.querySelector('dialog'),'閉じる').onclick();
const originalFetch=globalThis.fetch;let releaseInvitation,holdInvitation=true;
globalThis.fetch=async(path,options)=>{
  if(path.startsWith('/api/share/state')&&holdInvitation){holdInvitation=false;await new Promise(r=>releaseInvitation=r);return new Response(JSON.stringify(state));}
  return originalFetch(path,options);
};
const oldInvitation=startShareNotice(composer);await tick();await startShareNotice(composer);
await button(composer,'今はしない').onclick();releaseInvitation();await oldInvitation;
assert(!composer.querySelector('.share-invite'),'an older response cannot restore a consumed invitation');globalThis.fetch=originalFetch;
state={...state,available:false};await startShareNotice(composer);assert(!composer.querySelector('.share-invite'));assert.equal(await shareDialog({where:'start'}),undefined);
const {diaryScreen}=await import('../cbs/web/static/diary.js');
await diaryScreen(root,{name:'ソラ',diaries:[entry],share_enabled:false},()=>{});assert(!button(root,'シェア'));
root.replaceChildren();await diaryScreen(root,{name:'ソラ',diaries:[entry],share_enabled:true},()=>{});assert(root.querySelector('.diary-date button'));
console.log('PASS R7 offline DOM: compose URLs, dialog order, preview gating, redraws, image actions, invitation and trial controls');
