import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'https://unit.invalid/#note',runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const activity=date=>({date,title:'title',summary:'summary',impression:'impression',sources:[]});
const scene={id:1,text:'scene',dates:['2026-11-01'],evidence:[]};
const facts=privateFact=>Array.from({length:27},(_,i)=>({id:i+1,content:`fact ${i}`,private:privateFact}));
const publicFacts=facts(false),privateFacts=facts(true),calls=[];
const note={name:'ソラ',today_haru:'today',now_summary:'now',books:[{title:'book',author:'author',read_count:1,chunk_count:2}],
  interests:[{topic:'new interest',new:true},{topic:'old interest',new:false}],activities:[activity('2026-11-01'),activity('2026-10-31'),activity('2026-11-01')],
  activity_weeks:[{week_start:'2026-10-26',count:3,loaded:true},{week_start:'2026-10-19',count:2,loaded:false}],
  self_forming:[{label:'group',summary:'summary',new:true,scenes:[scene,{...scene,id:2}]},{label:'old group',summary:null,new:false,scenes:[{...scene,id:3}]}],
  self_traits:[{text:'trait',new:true,scenes:[]},{text:'old trait',new:false,scenes:[]}],past:['past'],knowing:{understandings:[],facts:publicFacts}};
globalThis.fetch=async(path,options={})=>{
  calls.push(path);path=path.split('?')[0];let data;
  if(path==='/api/partner')data={partner:{id:'fixture',name:'ソラ'}};
  else if(path==='/api/settings')data={ai_ready:true,tts:{enabled:false}};
  else if(path==='/api/look')data={expressions:[],cover:true};
  else if(path==='/api/sessions')data=[];
  else if(path==='/api/note')data=note;
  else if(path==='/api/note/private')data={facts:privateFacts};
  else if(path==='/api/note/private-settings')data={categories:{}};
  else if(path==='/api/note/activities')data={activities:[activity('2026-10-19'),activity('2026-10-20')]};
  else if(path.startsWith('/api/note/facts/')){if(options.method==='DELETE'){const list=document.querySelector('details.card[open]')?privateFacts:publicFacts;list.splice(list.findIndex(f=>path.endsWith('/'+f.id)),1);}data={ok:true};}
  else throw Error(`Unexpected offline request ${path}`);
  return new Response(JSON.stringify(data));
};
const tick=()=>new Promise(r=>setTimeout(r,25));
const click=async(text,scope=document)=>{const button=[...scope.querySelectorAll('button')].find(b=>b.textContent===text);assert(button,text);const result=button.onclick();await tick();return result;};
await import('../cbs/web/static/user.js');await tick();await tick();
assert.deepEqual([...document.querySelectorAll('#notebook-panel h3')].map(h=>h.textContent),['今日のソラ','今の様子','最近のこと','読んでいる本','好きなこと、気になること','あなたが知ったソラのこと','教えてくれた昔の話']);
const header=document.querySelector('.note-header');assert(header.nextElementSibling.textContent.startsWith('会話のなかで'));
const weeks=document.querySelectorAll('.activity-week');assert(weeks[0].open&&!weeks[1].open);
assert.deepEqual([...weeks[0].querySelectorAll('.activity-day')].map(d=>[d.firstChild.textContent,d.open]),[['11月1日（日）2 件',true],['10月31日（土）1 件',false]]);
assert(!calls.some(p=>p.includes('?week=')));weeks[1].open=true;await tick();await tick();
assert.deepEqual([...weeks[1].querySelectorAll('.activity-day')].map(d=>d.open),[true,false]);
weeks[1].open=false;await tick();weeks[1].open=true;await tick();assert.equal(calls.filter(p=>p.includes('?week=')).length,1);
for(const theme of ['light','dark']){
  document.documentElement.dataset.theme=theme;
  assert.equal(document.querySelectorAll('.interest-list .note-new').length,1);
  assert.equal(document.querySelectorAll('.partner-note h4 .note-new').length,1);
  assert.equal(document.querySelectorAll('.note-scene .note-new').length,0);
  assert.equal(document.querySelectorAll('.note-traits .note-new').length,1);
}
await document.querySelectorAll('[role=tab]')[2].onclick();await tick();
const explicit=()=>[...document.querySelectorAll('section')].find(s=>s.querySelector('h3')?.textContent==='あなたが話してくれたこと');
assert.equal(explicit().querySelectorAll('article').length,5);
await click('もっと見る（残り 22 件）',explicit());assert.equal(explicit().querySelectorAll('article').length,15);
await click('直す',explicit());await click('保存する',explicit());assert.equal(explicit().querySelectorAll('article').length,15);
const deletion=click('消す',explicit());await tick();await click('消す',document.querySelector('dialog'));await deletion;await tick();assert.equal(explicit().querySelectorAll('article').length,15);
let privacy=document.querySelector('details.card');privacy.open=true;await tick();await tick();assert.equal(privacy.querySelectorAll('article').length,5);
await click('もっと見る（残り 22 件）',privacy);assert.equal(privacy.querySelectorAll('article').length,15);
await click('直す',privacy);await click('保存する',privacy);await tick();privacy=document.querySelector('details.card');assert(privacy.open);assert.equal(privacy.querySelectorAll('article').length,15);
await click('もっと見る（残り 12 件）',privacy);assert.equal(privacy.querySelectorAll('article').length,25);
await click('もっと見る（残り 2 件）',privacy);assert.equal(privacy.querySelectorAll('article').length,27);assert(!privacy.textContent.includes('もっと見る'));
console.log('PASS NT1 offline DOM: section order, lazy weeks/day folding, NEW light/dark, 5+10 facts and refresh retention');
process.exit(0);
