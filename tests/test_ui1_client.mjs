import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'https://unit.invalid/#settings/partner/requests'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
const scrolled=[];window.scrollTo=()=>{};dom.window.HTMLElement.prototype.scrollIntoView=function(){scrolled.push(this.id);};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const cfg=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from pathlib import Path; from cbs.config import load_config; print(json.dumps(load_config(data_dir=Path('.tmp/ui1/node_config')).model_dump()))"],{encoding:'utf8'}));
Object.assign(cfg,{ai_ready:true,connection_verified:true,warnings:[],keys:{},reindex_required:false,advanced_presets:cfg.advanced.presets,days:{has_habits:true},backup_status:{jobs:[],trial:false},backup:{folder:'',frequency:'off'}});
cfg.tts.engine='external';cfg.tts.enabled=true;
let requests={version:0,fields:{first_person:'私',second_person:'キミ',user_name:'',register:'自然に',length:'短く'},lines:[],limits:{max_lines:40,max_line_chars:300,max_field_chars:1000}},previous;
let days={version:1,days:[{id:'d1',kind:'user_birthday',name:'誕生日',month:4,day:2,year:null}],habits:[{id:'h1',name:'挨拶',time:'07:00',weekdays:[0,1],enabled:true,allow_quiet:false}]};
let persona={ai_question:'honest'},sources=[{id:1,title:'読む先',url:'https://example.org/',enabled:true}],voice={mode:'design',description:'落ち着いた声',external_voice:'alloy',reference:''},candidate;
const calls=[],json=x=>new Response(JSON.stringify(x));let failSave=false;
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,body,method:options.method});
  if(route==='/api/partner')return json({partner:{id:'yuki',name:'ユキ'}});
  if(route==='/api/settings'){if(body){if(failSave)return new Response(JSON.stringify({detail:'保存できませんでした'}),{status:422});for(const [k,v]of Object.entries(body))cfg[k]={...cfg[k],...v};}return json(cfg);}
  if(route==='/api/partner/requests'){if(body){previous=structuredClone(requests);requests={...requests,...body,version:requests.version+1,can_undo:true};}return json(requests);}
  if(route==='/api/partner/requests/undo'){requests={...previous,version:requests.version+1,can_undo:true};return json(requests);}
  if(route==='/api/partner/days'){if(body)days={...body,version:days.version+1};return json(days);}
  if(route==='/api/persona'){if(body)persona=body;return json(persona);}
  if(route==='/api/sources'){if(body){const source={id:sources.length+2,title:'追加',url:body.url,enabled:true};sources.push(source);return json(source);}return json(sources);}
  if(route.startsWith('/api/sources/')){if(options.method==='DELETE')sources=sources.filter(s=>s.id!==Number(route.split('/')[3]));return json({ok:true});}
  if(route==='/api/voice'){if(body)voice=candidate.voice;return json({voice,references:[],preview_text:'こんにちは。'});}
  if(route==='/api/voice/candidate'){candidate=body;return json({candidate_id:'candidate',voice:body.voice});}
  if(route==='/api/voice/download')return json({status:'ready'});
  if(route==='/api/backup/settings'){cfg.backup=body;return json({config:body});}
  if(route==='/api/look')return json({expressions:[]});
  if(route==='/api/characters/yuki/augment')return json({questions:[]});
  if(route==='/api/import/jobs')return json({jobs:[]});
  if(route==='/api/import')return json([]);
  if(route==='/api/import/about/review')return json({token:'review',candidates:[{id:'fact1',content:'読書が好き',quote:'読書が好き',destination_label:'あなたのこと'}]});
  if(route==='/api/import/about/save')return json({saved:body.keys.length});
  if(route==='/api/sessions')return json(body?{id:'session'}:[]);
  if(route==='/api/embedding/download'||route==='/api/stt/download')return json({status:'ready'});
  if(route==='/api/llm/failures')return json({notices:[]});
  throw Error('Unexpected offline endpoint: '+route);
};
const tick=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));await new Promise(r=>setTimeout(r,5));};
const button=text=>[...document.querySelectorAll('button')].find(e=>e.textContent===text&&!e.closest('[hidden]'));
const click=async text=>{const b=button(text);assert(b,text);await b.onclick();await tick();};
const input=(label,value)=>{const l=[...document.querySelectorAll('label')].find(e=>e.firstChild?.textContent===label),e=l?.querySelector('input,textarea,select');assert(e,label);e.value=value;e.dispatchEvent(new window.Event('input',{bubbles:true}));return e;};
const checkFlows=()=>{for(const entry of document.querySelectorAll('.settings-flow')){assert(entry.classList.contains('btn'));assert(!entry.classList.contains('quiet'));assert(entry.querySelector('svg path'));assert(!entry.textContent.includes('›'));}};
const settingsWrites=()=>calls.filter(c=>c.route==='/api/settings'&&c.body);
const unload=()=>{const event=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(event);return event.defaultPrevented;};

await import('../cbs/web/static/user.js');await tick();
const {settingsRoute,shelves}=await import('../cbs/web/static/settings-shelves.js');
assert.equal(document.getElementById('heading').textContent,'設定');assert.equal(document.getElementById('subtitle').textContent,'');
assert.deepEqual([...document.querySelectorAll('.settings-shelves button')].map(b=>b.textContent),shelves.map(([,v])=>v));assert.equal(document.querySelectorAll('.shelf-divider').length,2);
for(const [old,target]of Object.entries({partner:'relationship','partner/requests':'relationship','partner-requests':'relationship','partner/days':'relationship','partner-days':'relationship','partner/pace':'pace','partner/look':'look','partner/history':'data',other:'data',screen:'screen',ai:'ai'}))assert.equal(settingsRoute('#settings/'+old),target);
input('あなたの名前（任意）','葵');assert(unload());
let move=button('画面と入力').onclick();await tick();await click('とどまる');await move;assert.equal(document.getElementById('request-user_name').value,'葵');
move=button('画面と入力').onclick();await tick();await click('保存して移る');await move;await tick();assert.equal(requests.fields.user_name,'葵');assert.equal(settingsWrites().length,0);assert(!unload());
const change=async(e,value)=>{if(e.type==='checkbox'||e.type==='radio')e.checked=value;else e.value=value;e.dispatchEvent(new window.Event('change',{bubbles:true}));await tick();};
const blur=async(title,value)=>{const e=input(title,value);e.dispatchEvent(new window.Event('blur'));await tick();return e;};
await change(input('外観','dark'),'dark');assert.equal(localStorage.getItem('cbs.theme'),'dark');assert(!unload());
const accentRadios=[...document.querySelectorAll('.accent-choices input[type=radio]')];assert.equal(accentRadios.length,4);assert.equal(document.querySelectorAll('.accent-choices .swatch').length,4);await change(accentRadios[1],true);assert.equal(localStorage.getItem('cbs.accent'),accentRadios[1].value);await change(accentRadios[0],true);assert.equal(localStorage.getItem('cbs.accent'),accentRadios[0].value);
let split=[...document.querySelectorAll('label.switch')].find(e=>e.textContent.startsWith('返答を分けて出す')).querySelector('input');
failSave=true;await change(split,false);assert.deepEqual(settingsWrites().at(-1).body,{display:{split_replies:false}});assert(split.closest('label').textContent.includes('保存できませんでした · もう一度'));assert(!unload());failSave=false;await click('もう一度');assert(split.closest('label').textContent.includes('保存しました'));assert.equal(split.checked,false);
await click('AI と費用');assert(document.querySelector('a[href="/wizard?ai=1"]'));checkFlows();assert.equal(document.querySelector('.settings-value-row').firstElementChild.textContent,'つなぎ方');assert(!document.body.textContent.includes('API キー'));assert(document.body.textContent.includes('作り話・食い違い・踏み込みすぎ'));assert(!button('AI の設定を保存'));
await click('話し方と関係');await click('一つ前に戻す');assert.equal(document.getElementById('request-user_name').value,'');assert(!document.body.textContent.includes('時刻の習慣'));assert(document.querySelector('a[href^="#note/"]'));
input('あなたの名前（任意）','未保存');await document.querySelector('.day-row button').onclick();input('月','5');await click('大切な日を保存');assert.equal(days.days[0].month,5);assert(unload(),'saving dates must not clean requests');move=button('話しかけと過ごし方').onclick();await tick();await click('捨てて移る');await move;await tick();
assert.equal(location.hash,'#settings/pace');assert(document.body.textContent.includes('時刻の習慣'));assert(!document.body.textContent.includes('あなたの誕生日'));assert.equal(document.querySelectorAll('.activity-row').length,7);assert(!document.querySelector('details').open);
let freq=input('自主活動の頻度','off');await change(freq,'off');assert.deepEqual(settingsWrites().at(-1).body,{autonomy:{inner_life:false}});assert(document.querySelector('.activity-list').disabled);assert(document.querySelector('details').inert);await change(freq,'1');assert(!document.querySelector('.activity-list').disabled);
let before=settingsWrites().length;const length=input('動画1本の長さ（分）','12');assert.equal(settingsWrites().length,before);length.dispatchEvent(new window.Event('blur'));await tick();assert.deepEqual(settingsWrites().at(-1).body,{autonomy:{tools:{watch_video:{max_minutes:12}}}});length.dispatchEvent(new window.Event('blur'));await tick();assert.equal(settingsWrites().length,before+1);
length.value='999';length.dispatchEvent(new window.Event('blur'));await tick();assert.equal(settingsWrites().length,before+1);length.value='13';length.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));await tick();assert.equal(settingsWrites().at(-1).body.autonomy.tools.watch_video.max_minutes,13);
input('サイトの URL','https://example.net/');await click('サイトを追加する');assert.equal(sources.length,2);assert(!unload());assert(!button('もう一度確かめる'));await click('外す');assert.equal(sources.length,1);
const habitSave=()=>[...document.querySelectorAll('button')].find(b=>b.textContent==='時刻の習慣を保存');assert(habitSave().hidden);const habit=document.querySelector('.habit-row');await habit.querySelector(':scope>button').onclick();assert(habitSave().hidden);input('名前（例：おはよう、おやすみ）','夕方').dispatchEvent(new window.Event('input',{bubbles:true}));assert(!habitSave().hidden);assert(unload());await click('時刻の習慣を保存');assert.equal(days.habits[0].name,'夕方');assert(habitSave().hidden);assert.equal(days.days[0].month,5);assert(!unload());
await click('習慣を追加');assert(!habitSave().hidden);move=button('見た目と声').onclick();await tick();await click('捨てて移る');await move;await tick();assert(document.querySelector('a[href="/wizard?voice=1"]'));assert([...document.querySelectorAll("a")].some(a=>a.getAttribute("href").includes("use=cover")),[...document.querySelectorAll("a")].map(a=>a.getAttribute("href")).join(" | "));assert(document.body.textContent.includes('まだありません'));assert(!document.body.textContent.includes('試しに読む文'));assert(![...document.querySelectorAll('label')].some(l=>l.textContent.startsWith('音声のエンジン')));
checkFlows();const enabled=[...document.querySelectorAll('.switch')].find(l=>l.textContent==='音声を使う').querySelector('input');await change(enabled,false);assert.deepEqual(settingsWrites().at(-1).body,{tts:{enabled:false}});assert([...document.querySelectorAll('.switch input')].filter(e=>e!==enabled).every(e=>e.disabled));assert(document.querySelector('input[type=range]').disabled);assert(!unload());
await click('記録とデータ');assert(scrolled.includes('settings-tab-data'));assert(document.body.textContent.includes('PC の故障や買い替え'));assert(document.body.textContent.includes('自動のバックアップには付けられません'));assert(button('バックアップから元に戻す'));assert(button('今すぐバックアップする'));assert(!document.body.textContent.includes('書き出し'));assert(document.getElementById('backup-folder').parentElement.hidden);
// 毎日 without a folder is not saved (the server needs one): it asks for the folder, then saves both together.
const daily=document.querySelector('input[type=radio][value=daily]'),off=document.querySelector('input[type=radio][value=off]'),backupSaves=()=>calls.filter(c=>c.route==='/api/backup/settings');
const savesBefore=backupSaves().length;daily.checked=true;daily.dispatchEvent(new window.Event('change'));await tick();
assert.equal(backupSaves().length,savesBefore);assert(!document.body.textContent.includes('保存できませんでした'));
const ask=[...document.querySelectorAll('p')].find(p=>p.textContent==='保存先のフォルダを入れると、この設定を保存します。');assert(ask&&!ask.hidden);
await blur('保存先のフォルダ','C:\\exports');assert.deepEqual(backupSaves().at(-1).body,{frequency:'daily',folder:'C:\\exports'});assert(ask.hidden);
off.checked=true;off.dispatchEvent(new window.Event('change'));await tick();assert.deepEqual(backupSaves().at(-1).body,{frequency:'off',folder:'C:\\exports'});assert(!unload());
for(const entry of document.querySelectorAll('.settings-flow')){assert(entry.classList.contains('btn'));assert(!entry.classList.contains('quiet'));assert(entry.querySelector('svg path'));assert(!entry.textContent.includes('›'));}await click('いまの設定');assert.equal(location.hash,'#settings');assert.equal(document.querySelectorAll('.settings-overview-row:not(.warn)').length,10);assert(!document.querySelector('.settings-content input'));for(const row of document.querySelectorAll('.settings-overview-row:not(.warn)')){assert(row.querySelector(':scope>.settings-label'));assert(row.querySelector(':scope>.settings-value'));assert(row.querySelector(':scope>svg path'));assert.equal(row.childNodes.length,3);assert(!row.textContent.includes('›'));}const overviewValue=label=>[...document.querySelectorAll('.settings-overview-row:not(.warn)')].find(r=>r.querySelector('.settings-label').textContent===label).querySelector('.settings-value');assert(overviewValue('声で入力').classList.contains('settings-problem'));assert(overviewValue('バックアップ').textContent.includes('この PC: まだありません · 別の場所: 未設定'));assert(overviewValue('今月の費用').textContent.includes('目安額なし'));assert(!overviewValue('AI').textContent.includes('openrouter/'));
cfg.llm.mode='simple';cfg.simple={actor:'openrouter/test/model'};cfg.model_candidates={actor:[{model:'openrouter/test/model',label:'標準'}]};await click('画面と入力');checkFlows();await click('いまの設定');assert.equal(overviewValue('AI').textContent,'かんたん（OpenRouter） · 標準');cfg.model_candidates.actor=[];await click('画面と入力');await click('いまの設定');assert.equal(overviewValue('AI').textContent,'かんたん（OpenRouter） · test/model');
location.hash='#settings/partner-days';await tick();assert.equal(document.activeElement.id,'partner-days');
const {clearSettingsNavigation}=await import('../cbs/web/static/settings-navigation.js');clearSettingsNavigation();
console.log('PASS ST2 settings DOM: shelves/aliases/overview, immediate and blur/Enter scoped saves, validation/retry/badge, guarded cards, habits/date version, disabled controls, wizard links, backup copy');process.exit(0);
