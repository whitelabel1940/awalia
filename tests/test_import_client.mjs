import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<div id="error"></div><main id="root"></main>',{url:'https://unit.invalid/?character=fixture',runScripts:'outside-only'});
for(const k of ['window','document','location','localStorage'])globalThis[k]=dom.window[k];
dom.window.HTMLDialogElement.prototype.showModal=function(){this.querySelectorAll('button')[1].click();};
dom.window.HTMLDialogElement.prototype.close=function(){this.remove();};
const calls=[];let state={id:'batch',status:'cancelled',completed:1,total:2,percent:50,created_at:'2026-10-19',quote:{estimated_seconds:60,estimated_usd:.02}};
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):undefined;calls.push({route,body});
  let data;
  if(route==='/api/import')data=[state];
  else if(route==='/api/import/start'){assert.equal(body.confirmed,true);state={...state,status:'completed',completed:2,percent:100};data=state;}
  else if(route==='/api/import/batch/undo'){state={...state,status:'undone'};data=state;}
  else if(route==='/api/import/preview')data={preview_id:'p',conversations:[
    {key:'a',title:'<script>original title</script>',first_at:'2026-03-01',last_at:'2026-03-02',message_count:2,format:'chatgpt'},
    {key:'b',title:'duplicate',duplicate:true,message_count:2,format:'claude'},
    {key:'c',title:'missing',error:'日付なし',message_count:1,format:'exporter'}]};
  else if(route==='/api/import/plan'){assert.deepEqual(body.keys,['a']);data={token:'q',conversations:1,messages:2,estimated_seconds:20,estimated_usd:.01,estimate_note:'見積もり'};}
  else throw new Error('Unexpected fake endpoint '+route);
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
};
const {openImport}=await import('../cbs/web/static/import.js');
await openImport(document.getElementById('root'));
const button=text=>[...document.querySelectorAll('button')].find(b=>b.textContent===text);
await button('続きから再開').onclick();assert(document.body.textContent.includes('会話を覚えました'));
await button('この取り込みをまとめて取り消す').onclick();assert(document.body.textContent.includes('取り込みを取り消しました'));
const input=document.querySelector('input[type=file]');
Object.defineProperty(input,'files',{value:[{name:'export.json',size:12,arrayBuffer:async()=>new TextEncoder().encode('{}').buffer}]});
await input.onchange();
assert.equal(document.querySelectorAll('script').length,0);
const checks=[...document.querySelectorAll('input[type=checkbox]')];assert.equal(checks.length,4);assert(checks[2].disabled&&checks[3].disabled);
assert.equal(calls.filter(c=>c.route==='/api/import/start').length,1);
await button('時間と費用の目安を見る').onclick();assert(document.body.textContent.includes('$0.01'));
assert(button('この目安で取り込む'));
assert.equal(calls.filter(c=>c.route==='/api/import/start').length,1);
console.log('PASS IM offline DOM: selection, duplicate/date exclusions, escaped titles, estimate before start, resume and batch undo');

// Pasted facts and the new-partner route use the same review and explicit save.
const originalFetch=globalThis.fetch;
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):undefined;
  if(route.endsWith('/about/review')){
    calls.push({route,body});return new Response(JSON.stringify({token:'review',candidates:[
      {id:'0',content:'紅茶が好き',quote:'紅茶が好き',category_label:'ふだんのこと'},
      {id:'1',content:'片頭痛',quote:'片頭痛',category_label:'続いている健康のこと'}]}));
  }
  if(route.endsWith('/about/save')){
    calls.push({route,body});assert.deepEqual(body,{token:'review',keys:['1']});return new Response(JSON.stringify({saved:1}));
  }
  if(route==='/api/bootstrap/import/new')return new Response('[]');
  if(route.startsWith('/api/bootstrap/import/new/'))return originalFetch(path.replace('/api/bootstrap/import/new','/api/import'),options);
  return originalFetch(path,options);
};
document.getElementById('root').replaceChildren();let continued;
await openImport(document.getElementById('root'),{path:'/api/bootstrap/import/new',onContinue:(token,text)=>continued={token,text}});
const about=document.querySelector('textarea');
// The first textarea is the optional setting; the second is about the user.
const pasted=document.querySelectorAll('textarea')[1];pasted.value='紅茶が好き。片頭痛。';pasted.dispatchEvent(new window.Event('input'));
await button('入れる内容を確かめる').onclick();
const factChecks=[...document.querySelectorAll('input[type=checkbox]')];assert.equal(factChecks.length,2);assert(factChecks.every(c=>!c.checked));
assert(!calls.some(c=>c.route.endsWith('/about/save')));
factChecks[1].checked=true;await button('選んだ内容を保存').onclick();assert(document.body.textContent.includes('1件を保存しました'));
about.value='一人称はボク';await button('会話ファイルなしで人物像づくりへ').onclick();assert.deepEqual(continued,{token:null,text:'一人称はボク'});
assert(calls.some(c=>c.route==='/api/bootstrap/import/new/about/save'));
console.log('PASS IM round 2 offline DOM: partner-scoped paste review, unchecked candidates, explicit subset save, optional instructions and continuation');

document.getElementById('root').replaceChildren();let releaseSave;
const savingGate=new Promise(resolve=>releaseSave=resolve);
globalThis.fetch=async(path)=>{
  if(path.split('?')[0].endsWith('/about/review'))return new Response(JSON.stringify({token:'race',candidates:[{id:'0',content:'紅茶',quote:'紅茶',category_label:'ふだんのこと'}]}));
  await savingGate;return new Response(JSON.stringify({saved:1}));
};
const {aboutImport}=await import('../cbs/web/static/import.js');
const control=aboutImport(document.getElementById('root'));
const draft=document.querySelector('textarea');draft.value='紅茶';draft.dispatchEvent(new window.Event('input'));
await button('入れる内容を確かめる').onclick();document.querySelector('input[type=checkbox]').checked=true;
const saveWork=button('選んだ内容を保存').onclick();
draft.value='新しい内容';draft.dispatchEvent(new window.Event('input'));releaseSave();await saveWork;
assert.equal(control.ready(),false,'saving an old selection cannot confirm newly edited text');
console.log('PASS IM round 2 offline DOM: an in-flight save never confirms a newer paste');

document.getElementById('root').replaceChildren();let duplicateSave;
globalThis.fetch=async(path,options={})=>{
  if(path.split('?')[0].endsWith('/about/review'))return new Response(JSON.stringify({token:'duplicates',candidates:[
    {id:'0',content:'紅茶が好き',quote:'紅茶',category_label:'ふだんのこと',existing_facts:[{id:7,content:'紅茶を好む'}]}]}));
  duplicateSave=JSON.parse(options.body);return new Response(JSON.stringify({saved:1}));
};
aboutImport(document.getElementById('root'));
document.querySelector('textarea').value='紅茶';await button('入れる内容を確かめる').onclick();
assert(document.body.textContent.includes('紅茶を好む'));assert.equal(document.querySelector('select').value,'');
assert(!document.querySelector('input[type=checkbox]').checked);document.querySelector('input[type=checkbox]').checked=true;
await button('選んだ内容を保存').onclick();assert(!duplicateSave,'explicit duplicate choice is required');
document.querySelector('select').value='7';await button('選んだ内容を保存').onclick();
assert.deepEqual(duplicateSave,{token:'duplicates',keys:['0'],decisions:{'0':7}});
console.log('PASS FB3 offline DOM: existing duplicate alongside candidate, unchecked default, explicit merge choice');

// FB4: settings keeps one panel during loading, use, close, and completion.
document.getElementById('root').replaceChildren();
let releaseList,listCount=0;
const listGate=new Promise(resolve=>releaseList=resolve);
globalThis.fetch=async(path,options)=>{
  if(path.split('?')[0]!=='/api/import')return originalFetch(path,options);
  listCount++;
  if(listCount===1)await listGate;
  return new Response(JSON.stringify(listCount===3?[{...state,status:'completed'}]:[]));
};
const {importSettings}=await import('../cbs/web/static/import.js');
importSettings(document.getElementById('root'));
const launch=button('ほかの場所の会話を足す'),opening=launch.onclick();
assert(launch.disabled);assert.equal(document.querySelectorAll('input[type=file]').length,1);
await launch.onclick();assert.equal(listCount,1);assert.equal(document.querySelectorAll('input[type=file]').length,1);
releaseList();await opening;
await launch.onclick();assert.equal(listCount,1);assert.equal(document.activeElement,document.querySelector('section'));
await button('閉じる').onclick();assert.equal(document.querySelectorAll('input[type=file]').length,0);assert.equal(document.activeElement,launch);
await launch.onclick();assert.equal(listCount,2);assert.equal(document.querySelectorAll('input[type=file]').length,1);
await button('閉じる').onclick();await launch.onclick();assert(document.body.textContent.includes('会話を覚えました'));
await launch.onclick();assert.equal(listCount,3,'past completion keeps the current draft panel open');
const newFile=document.querySelector('input[type=file]');
Object.defineProperty(newFile,'files',{value:[{name:'export.json',size:12,arrayBuffer:async()=>new TextEncoder().encode('{}').buffer}]});
await newFile.onchange();await button('時間と費用の目安を見る').onclick();await button('この目安で取り込む').onclick();
await launch.onclick();assert.equal(listCount,4);assert.equal(document.querySelectorAll('input[type=file]').length,1);
await button('閉じる').onclick();
globalThis.fetch=async()=>{throw new Error('offline list failure');};await launch.onclick();
assert.equal(document.querySelectorAll('input[type=file]').length,0);assert(!launch.disabled);
globalThis.fetch=async()=>new Response('[]');await launch.onclick();assert.equal(document.querySelectorAll('input[type=file]').length,1);
console.log('PASS FB4 offline DOM: one settings import panel, loading/repeated focus, close/reopen, completion/reopen, failed load/retry');

// FB4-4: one paste, explicit destinations and unchecked subset selection in both routes.
for(const path of ['/api/import','/api/bootstrap/import/new']){
  document.getElementById('root').replaceChildren();let saved;
  globalThis.fetch=async(url,options)=>{
    const body=JSON.parse(options.body);
    if(url.split('?')[0]===path+'/about/review')return new Response(JSON.stringify({token:'mixed',candidates:[
      {id:'0',content:'紅茶が好き',quote:'紅茶が好き',destination:'fact',destination_label:'あなたのこと',category_label:'ふだんのこと'},
      {id:'1',content:'アキ',quote:'アキと呼んで',destination:'request',destination_label:'呼び方'},
      {id:'2',content:'絵文字を使わない',quote:'絵文字を使わない',destination:'request',destination_label:'お願い'},
      {id:'3',content:'<b>ぼく</b>',quote:'<b>ぼく</b>',destination:'request',destination_label:'一人称'}]}));
    assert.equal(url.split('?')[0],path+'/about/save');saved=body;return new Response(JSON.stringify({saved:2}));
  };
  const pasteControl=aboutImport(document.getElementById('root'),path);
  assert(document.body.textContent.includes('あなたについてのことや、お願い（任意）'));
  document.querySelector('textarea').value='紅茶が好き。アキと呼んで。絵文字を使わない。<b>ぼく</b>';
  await button('入れる内容を確かめる').onclick();
  assert(document.body.textContent.includes('あなたのこと ／ ふだんのこと'));
  assert(document.body.textContent.includes('アキ（呼び方）'));assert(document.body.textContent.includes('絵文字を使わない（お願い）'));
  assert(document.body.textContent.includes('<b>ぼく</b>（一人称）'));assert(!document.querySelector('b'));
  const choices=[...document.querySelectorAll('input[type=checkbox]')];assert(choices.every(c=>!c.checked));assert(!saved);
  choices[0].checked=choices[1].checked=true;await button('選んだ内容を保存').onclick();
  assert.deepEqual(saved,{token:'mixed',keys:['0','1']});assert(pasteControl.ready());
}
console.log('PASS FB4-4 offline DOM: mixed destinations, unchecked review, explicit subset save, escaping, settings and welcome routes');
