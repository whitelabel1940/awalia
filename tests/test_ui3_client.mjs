import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<div id="screen"></div><p id="error"></p>',{url:'https://unit.invalid/?character=yuki#settings/partner/pace'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
const root=document.getElementById('screen');
const cfg=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from pathlib import Path; from cbs.config import load_config; print(json.dumps(load_config(data_dir=Path('.tmp/ui3/node_config')).model_dump()))"],{encoding:'utf8'}));
Object.assign(cfg,{warnings:[],keys:{},advanced_presets:[],tts:null});
let unanswered=true,failWeek=false,releaseWeek;
const calls=[],result=x=>new Response(JSON.stringify(x));
globalThis.fetch=async(path,options={})=>{
  const url=new URL(path,location.href),route=url.pathname,body=options.body?JSON.parse(options.body):null;calls.push({url,body});
  if(route==='/api/settings'){if(body)for(const [k,v]of Object.entries(body))Object.assign(cfg[k],v);return result(cfg);}
  if(route==='/api/partner/requests')return result({version:0,fields:{},lines:[],limits:{max_lines:40,max_line_chars:300,max_field_chars:1000}});
  if(route==='/api/persona')return result({ai_question:'honest'});
  if(route==='/api/characters/yuki/augment')return result({questions:unanswered?[{}]:[]});
  if(route==='/api/sources')return result([]);
  if(route==='/api/import/jobs')return result({jobs:[]});
  if(route==='/api/import')return result([]);
  if(route==='/api/embedding/download'||route==='/api/stt/download')return result({status:'ready'});
  if(route==='/api/note/private-settings')return result({categories:{}});
  if(route==='/api/resident/face')return result({validForMs:1000});
  if(route==='/api/note/activities'){
    if(failWeek)return new Response(JSON.stringify({detail:'一時的な失敗'}),{status:500});
    await new Promise(resolve=>{releaseWeek=resolve;});return result({activities:[activity('2025-12-29'),activity('2026-01-04')]});
  }
  throw Error('Unexpected offline route '+route);
};
const tick=()=>new Promise(r=>setTimeout(r,15));
const {settingsScreen}=await import('../cbs/web/static/settings.js');
const {clearSettingsNavigation}=await import('../cbs/web/static/settings-navigation.js');
const field=(host,label)=>[...host.querySelectorAll('label')].find(e=>e.textContent.startsWith(label))?.querySelector('input,select');
const click=async(host,text)=>{const button=[...host.querySelectorAll('button')].find(e=>e.textContent===text);assert(button,text);await button.onclick();};
const mount=async()=>{clearSettingsNavigation();root.replaceChildren();await settingsScreen(root,'ユキ');};
await mount();
const change=async(e,value)=>{if(e.type==='checkbox')e.checked=value;else e.value=value;e.dispatchEvent(new window.Event('change',{bubbles:true}));await tick();};
let pace=document.getElementById('settings-content'),greeting=[...pace.querySelectorAll('.card')].find(e=>e.querySelector('h3')?.textContent==='話しかけ');
assert.deepEqual([...greeting.querySelectorAll('label')].map(e=>e.textContent.replace(/多めふつう控えめ|(?:\d\d:\d\d)+/g,'')),['自分から話しかける','話しかける頻度','静かな時間の開始','静かな時間の終了']);
assert(pace.textContent.includes('調べたことや読んだものの話を、会話でしてくることがあります。'));
let talk=field(greeting,'自分から話しかける'),frequency=field(greeting,'話しかける頻度');assert(talk.checked);assert(!frequency.disabled);
await change(frequency,'reserved');await change(talk,false);assert(frequency.disabled);assert.equal(cfg.autonomy.opening,'off');assert.equal(cfg.autonomy.idle_nudge,false);
await change(talk,true);assert(!frequency.disabled);assert.equal(frequency.value,'reserved');assert.equal(cfg.autonomy.opening,'reserved');assert.equal(cfg.autonomy.idle_nudge,true);
assert.deepEqual(calls.filter(c=>c.body&&c.url.pathname==='/api/settings').at(-1).body,{autonomy:{opening:'reserved',idle_nudge:true}});
cfg.autonomy.opening='off';cfg.autonomy.idle_nudge=true;await mount();pace=document.getElementById('settings-content');talk=field(pace,'自分から話しかける');frequency=field(pace,'話しかける頻度');assert(!talk.checked);assert(frequency.disabled);assert.equal(frequency.value,'normal');
location.hash='#settings/relationship';await mount();assert(root.textContent.includes('まだ答えていない質問'));assert(root.textContent.includes('迎えた時に答えなかった、ユキの性格や過去の場面についての質問です。答えるとユキの人となりに加わります。'));assert(root.querySelector('a[href="/wizard?augment=yuki"]'));unanswered=false;await mount();assert(!root.textContent.includes('まだ答えていない質問'));
location.hash='#settings/screen';await mount();assert.deepEqual([...root.querySelectorAll('.card>h3')].map(e=>e.textContent),['画面の見た目','会話とノートの表示','声で入力する']);for(const title of ['外観','返答を分けて出す','ユキのことを新しく知った時に知らせる','ノートに「今日のユキ」を表示する'])assert(field(root,title));
location.hash='#settings/data';await mount();assert(root.textContent.includes('ユキとの以前の会話として、元の日付のまま覚えます。'));await click(root,'ChatGPT や Claude の会話を取り込む');await click(root,'ほかの場所の会話を足す');assert(root.textContent.includes('ChatGPT・Claudeのエクスポートしたファイル'));
const nativeCalls=[];window.__TAURI__={core:{invoke:async(command,args)=>{nativeCalls.push({command,args});if(command==='resident_preferences')return {preferences:{alwaysVisible:true,faceSize:96,chimeEnabled:true,chimeVolume:40,idleMotion:false,shortcut:null}};if(command==='resident_ptt_preferences')return {shortcut:null};if(command==='resident_sync')return;throw Error('Unexpected native command '+command);}}};
location.hash='#settings/screen';await mount();const nativeCard=document.getElementById('resident-always-visible').closest('.card');assert.equal(nativeCard.querySelector('h3').textContent,'常駐アプリ');assert.equal(document.getElementById('resident-ptt-shortcut').closest('.card').querySelector('h3').textContent,'声で入力する');assert(!root.querySelector('#resident-chime-enabled'));await change(root.querySelector('#resident-always-visible'),false);assert(nativeCalls.some(c=>c.command==='resident_preferences'&&c.args?.preferences));
location.hash='#settings/pace';await mount();assert.equal(root.querySelector('#resident-chime-enabled').closest('.card').querySelector('h3').textContent,'話しかけ');assert.equal(root.querySelector('#resident-chime-volume').closest('section').querySelector('h4').textContent,'知らせる音');
delete window.__TAURI__;clearSettingsNavigation();

const {recentActivities}=await import('../cbs/web/static/note-activities.js');
function activity(date){return {date,title:'**読んだこと**',summary:'本文',impression:'感想',sources:[{title:'記事'}]};}
root.replaceChildren();
const dates=['2026-10-19','2026-10-12','2026-10-05','2026-09-28'];
const archive=[...dates.map(week_start=>({week_start,count:1,loaded:true})),{week_start:'2025-12-29',count:2,loaded:false}];
const recent=recentActivities(root,dates.map(activity),archive),weeks=[...recent.querySelectorAll('.activity-week')];
assert.deepEqual(weeks.map(e=>e.open),[true,false,false,false,false]);assert.equal(recent.querySelectorAll('article').length,1);
assert.equal(weeks[4].textContent,'12月29日の週（2 件）');assert(!calls.some(c=>c.url.pathname==='/api/note/activities'));
const toggle=async(week,open)=>{week.open=open;week.dispatchEvent(new window.Event('toggle'));await tick();};
await toggle(weeks[1],true);assert.equal(weeks[1].querySelectorAll('article').length,1);assert(!calls.some(c=>c.url.pathname==='/api/note/activities'));
await toggle(weeks[4],true);await toggle(weeks[4],true);assert.equal(calls.filter(c=>c.url.pathname==='/api/note/activities').length,1);
const request=calls.at(-1);assert.equal(request.url.searchParams.get('week'),'2025-12-29');assert.equal(request.url.searchParams.get('character_id'),'yuki');
await toggle(weeks[4],false);releaseWeek();await tick();assert.equal(weeks[4].querySelectorAll('article').length,0);
await toggle(weeks[4],true);assert.equal(weeks[4].querySelectorAll('article').length,2);await toggle(weeks[4],false);await toggle(weeks[4],true);assert.equal(calls.filter(c=>c.url.pathname==='/api/note/activities').length,1);
const errors=recentActivities(root,[],[{week_start:'2025-12-29',count:2,loaded:false}]);failWeek=true;await tick();assert(errors.textContent.includes('読み込めませんでした'));
const retry=errors.querySelector('details');await toggle(retry,false);failWeek=false;await toggle(retry,true);releaseWeek();await tick();assert.equal(errors.querySelectorAll('article').length,2);assert(!errors.querySelector('[role=status]'));

const {knowingScreen}=await import('../cbs/web/static/knowing.js');
const stages=['known','probable','tentative'],understandings=stages.map((stage,id)=>({id,kind:'interaction',text:stage,stage,stage_label:stage,evidence:[],counterevidence:[]}));
root.replaceChildren();await knowingScreen(root,{understandings,facts:[]},'ユキ',async()=>{});
let rows=[...root.querySelectorAll('article')];assert.deepEqual(rows.map(row=>[...row.querySelectorAll('button')].map(b=>b.textContent)),[['違う','忘れて'],['合ってる','違う','忘れて'],['合ってる','違う','忘れて']]);
understandings[0].stage='probable';root.replaceChildren();await knowingScreen(root,{understandings,facts:[]},'ユキ',async()=>{});assert.equal(root.querySelector('article button').textContent,'合ってる');
assert(!root.textContent.includes('テスト係'));
console.log('PASS UI3 offline DOM: shared talk switch/order/legacy/scoped saves, augment placement/empty, import help, screen groups, lazy weeks/cache/race/retry/partner scoping, known and probable controls');
process.exit(0);
