import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'https://unit.invalid/#note',runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const calls=[];
const privateFact={id:3,content:'私的な事実',private:true,private_category:'temporary_health',category_label:'一時的な体調の出来事',mention_override:null,allow_mention:true};
const categories={temporary_health:{label:'一時的な体調の出来事',allow_mention:true},ongoing_health:{label:'続いている健康の事情',allow_mention:false},other:{label:'その他',allow_mention:false}};
let data={stopped:false,understandings:[{id:1,kind:'interaction',text:'短い返事が好みらしい',condition:'雑談の時',stage_label:'〜みたい',status:'active',expires_at:null,evidence:[{observed_at:'2026-10-01',summary:'二、三文が好きと言った'}],counterevidence:[]}],facts:[{id:2,content:'明言したこと',private:false}]};
const result=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):undefined;calls.push({path,route,body,method:options.method||'GET'});
  if(route==='/api/partner')return result({partner:{id:'fixture',name:'テスト係'}});
  if(route==='/api/settings')return result({ai_ready:true,tts:{enabled:false}});
  if(route==='/api/look')return result({expressions:[]});
  if(route==='/api/sessions')return result(options.method?{id:'s'}:[]);
  if(route==='/api/proactive/opening')return result({speak:false});
  if(route==='/api/note')return result({name:'テスト係',knowing:data,interests:[],activities:[],past:[],books:[]});
  if(route==='/api/note/private')return result({facts:[privateFact]});
  if(route==='/api/note/private-settings'){if(body)for(const [k,v]of Object.entries(body.categories))categories[k].allow_mention=v;return result({categories});}
  if(route==='/api/note/facts/3/mention'){privateFact.mention_override=body.mention_override;return result({ok:true});}
  if(route==='/api/note/inference'){data.stopped=body.stopped;return result({stopped:body.stopped});}
  if(route.startsWith('/api/note/understandings/')){if(body.action==='agree')data.understandings[0].stage_label='知っている';if(body.action==='forget')data.understandings=[];return result({ok:true});}
  if(route.startsWith('/api/note/facts/')){if(options.method==='DELETE')data.facts=[];else data.facts[0].content=body.content;return result({ok:true});}
  throw new Error('Unexpected offline request: '+route);
};
const tick=()=>new Promise(r=>setTimeout(r,20));
const click=async(text,scope=document)=>{const b=[...scope.querySelectorAll('button')].find(e=>e.textContent===text);assert(b,`Missing ${text}`);const pending=b.onclick();await tick();return pending;};
await import('../cbs/web/static/user.js');await tick();await tick();
const tabs=[...document.querySelectorAll('[role=tab]')];assert.deepEqual(tabs.map(e=>e.textContent),['テスト係のこと','ふたりのこと','あなたのこと']);
await tabs[2].onclick();await tick();assert.equal(tabs[2].getAttribute('aria-selected'),'true');
assert(document.body.textContent.includes('なぜそう思った？'));assert(document.body.textContent.includes('〜みたい'));
const inferenceCard=[...document.querySelectorAll('section')].find(s=>s.querySelector('h3')?.textContent==='あなたについての理解');
assert(inferenceCard?.querySelector('input[type=checkbox]'));
assert.equal(inferenceCard.previousElementSibling.querySelector('h3').textContent,'プライベートなことの気にかけ方');
assert(document.body.textContent.includes('プライベートなことの気にかけ方'));const categoryCard=[...document.querySelectorAll('section')].find(s=>s.querySelector('h3')?.textContent==='プライベートなことの気にかけ方');assert.equal(categoryCard.previousElementSibling.querySelector('summary').textContent,'もっとプライベートなこと');assert.deepEqual([...categoryCard.querySelectorAll('input')].map(i=>i.checked),[true,false,false]);assert(!calls.some(c=>c.route==='/api/note/private'));assert(!document.body.textContent.includes('私的な事実'));
await click('合ってる');assert(document.body.textContent.includes('知っている'));
await click('違う');const correction=[...document.querySelectorAll('input')].find(e=>e.parentElement.textContent.includes('どこが違う'));correction.value='仕事の話は詳しく';correction.dispatchEvent(new window.Event('input'));await click('伝える');assert(calls.some(c=>c.body?.action==='disagree'&&c.body.detail==='仕事の話は詳しく'));
await click('直す');let edit=document.querySelector('textarea');edit.value='直した事実';edit.dispatchEvent(new window.Event('input'));await click('保存する');assert.equal(data.facts[0].content,'直した事実');
const deleted=click('消す');await tick();await click('消す',document.querySelector('dialog'));await deleted;await tick();assert.deepEqual(data.facts,[]);assert(calls.some(c=>c.route==='/api/note/facts/2'&&c.method==='DELETE'));
const privacy=[...document.querySelectorAll('details')].find(e=>e.firstChild.textContent==='もっとプライベートなこと');assert(!privacy.open);privacy.open=true;privacy.dispatchEvent(new window.Event('toggle'));await tick();assert(document.body.textContent.includes('私的な事実'));
const mention=privacy.querySelector('select');assert(mention);assert.equal(mention.value,'default');
for(const [value,expected]of [['false',false],['true',true],['default',null]]){mention.value=value;mention.dispatchEvent(new window.Event('input'));await tick();assert.equal(privateFact.mention_override,expected);}
assert(!calls.some(c=>c.route==='/api/note/facts/3'&&c.method==='PUT'));
const forgotten=click('忘れて');await tick();await click('忘れて',document.querySelector('dialog'));await forgotten;await tick();assert.equal(data.understandings.length,0);
// Loading category controls must not append them to the other tab after a fast switch.
await tabs[0].onclick();let releaseCategories;const categoryGate=new Promise(r=>releaseCategories=r),normalFetch=globalThis.fetch;
globalThis.fetch=async(path,options={})=>{if(path.split('?')[0]==='/api/note/private-settings'&&!options.method)await categoryGate;return normalFetch(path,options);};
const showingYou=tabs[2].onclick();await tick();await tabs[0].onclick();releaseCategories();await showingYou;globalThis.fetch=normalFetch;
assert(!document.getElementById('notebook-panel').textContent.includes('プライベートなことの気にかけ方'));
const {inferenceSetting}=await import('../cbs/web/static/knowing.js');const parent=document.createElement('section');document.body.append(parent);await inferenceSetting(parent,data);
const stopped=parent.querySelector('input');stopped.checked=true;await stopped.onchange();assert(!calls.some(c=>c.route==='/api/note/inference'));
await click('今の理解を残す',parent);assert(data.stopped);assert(calls.at(-1).body.clear===false);
stopped.checked=false;await stopped.onchange();assert(!data.stopped);stopped.checked=true;await stopped.onchange();await click('今の理解を消す',parent);assert(calls.at(-1).body.clear===true);
assert(calls.filter(c=>c.route.startsWith('/api/note')).every(c=>c.path.includes('character_id=fixture')));
const {privateSettings}=await import('../cbs/web/static/knowing.js');const settings=document.createElement('section');document.body.append(settings);await privateSettings(settings);
assert(settings.textContent.includes('プライベートなことの気にかけ方'));const switches=settings.querySelectorAll('input');assert.deepEqual([...switches].map(i=>i.checked),[true,false,false]);
switches[0].checked=false;await switches[0].onchange();assert.equal(categories.temporary_health.allow_mention,false);assert.deepEqual(calls.at(-1).body,{categories:{temporary_health:false}});
console.log('PASS M11/M11c offline DOM: tabs, evidence, controls, lazy private, fact tri-state mention override, inference, category settings');
