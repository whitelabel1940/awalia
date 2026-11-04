import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
if(!process.argv[2]){
  for(const hour of ['01','12','23'])process.stdout.write(execFileSync(process.execPath,[fileURLToPath(import.meta.url),hour],{encoding:'utf8',env:{...process.env,TZ:'Asia/Tokyo'}}));
  process.exit(0);
}
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
// Match the recorded conversation; fix both Date() and Date.now(), in both realms.
const RealDate=globalThis.Date;
const fixedNow=new RealDate(`2026-10-21T${process.argv[2]}:00:00+09:00`).getTime();
class FixedDate extends RealDate{constructor(...args){super(...(args.length?args:[fixedNow]));}static now(){return fixedNow;}}
globalThis.Date=FixedDate;
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'http://127.0.0.1:18765/#talk',runScripts:'outside-only',pretendToBeVisual:true});
dom.window.Date=FixedDate;
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};window.HTMLElement.prototype.scrollIntoView=function(){};
const timers=new Map(),realTimeout=globalThis.setTimeout,realClear=globalThis.clearTimeout;let serial=0;
globalThis.setTimeout=(fn,ms,...args)=>{if(ms>=1000){const id=++serial;timers.set(id,{fn,ms});return id;}return realTimeout(fn,ms,...args);};
globalThis.clearTimeout=id=>{if(!timers.delete(id))realClear(id);};
const cfg=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from pathlib import Path; from cbs.config import load_config; print(json.dumps(load_config(data_dir=Path('.tmp/hb1/node_config')).model_dump()))"],{encoding:'utf8'}));
Object.assign(cfg,{ai_ready:true,connection_verified:true,warnings:[],keys:{},reindex_required:false,tts:null});
cfg.advanced_presets=cfg.advanced.presets;cfg.days.has_habits=false;cfg.days.poll_seconds=120;
let state={version:0,days:[],habits:[]},stream,nextTurn=10,failSave=false;
const calls=[],response=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,path,body});
  if(route==='/api/partner')return response({partner:{id:'sora',name:'ソラ'}});
  if(route==='/api/settings')return response(cfg);
  if(route==='/api/look')return response({expressions:[]});
  if(route==='/api/persona')return response({ai_question:'honest'});
  if(route==='/api/characters/sora/augment')return response({questions:[]});
  if(route==='/api/sources')return response([]);
  if(route==='/api/embedding/download')return response({status:'ready'});
  if(route==='/api/import/jobs')return response({jobs:[]});
  if(route==='/api/partner/requests')return response({version:0,fields:{first_person:'私',second_person:'あなた',register:'自然に',length:'短く'},lines:[],limits:{max_lines:40,max_line_chars:300,max_field_chars:1000}});
  if(route==='/api/partner/days'){
    if(body){if(failSave)return new Response(JSON.stringify({detail:'日付を確認してください。'}),{status:422});state={...body,version:state.version+1,days:body.days.map((d,i)=>({...d,id:d.id||`day${i}`})),habits:body.habits.map((h,i)=>({...h,id:h.id||`habit${i}`}))};cfg.days.has_habits=state.habits.some(h=>h.enabled);}
    return response(state);
  }
  if(route==='/api/note')return response({name:'ソラ',days:{first_day:'2026-07-13',elapsed_days:100,talked_days:3},nearby_days:[{name:'<img src=x>記念日',date:'2026-10-21',in_days:0}],interests:[],activities:[],past:[],self_forming:[]});
  if(route==='/api/sessions')return response(body?{id:'session'}:[]);
  if(route==='/api/proactive/opening')return response({speak:false});
  if(route==='/api/proactive/habit')return response({speak:true,turn_id:++nextTurn,assistant_text:'おはよう。',assistant_at:'2026-10-21T07:00:00+09:00',expression:'neutral'});
  if(route.endsWith('/messages/stream'))return new Response(new ReadableStream({start(controller){stream=controller;}}));
  throw Error('Unexpected offline endpoint '+route);
};
const settle=async()=>{for(let i=0;i<18;i++)await new Promise(r=>setImmediate(r));};
const view=async hash=>{location.hash=hash;window.dispatchEvent(new window.HashChangeEvent('hashchange'));await settle();};
const click=async text=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent===text);assert(b,text);await b.onclick();await settle();};
const input=(e,value)=>{assert(e);e.value=value;e.dispatchEvent(new window.Event('input'));};
const poll=async()=>{assert.equal(timers.size,1);const [id,t]=timers.entries().next().value;timers.delete(id);await t.fn();await settle();};
const count=route=>calls.filter(c=>c.route===route).length;
await import('../cbs/web/static/user.js');await settle();assert.equal(timers.size,0);
document.getElementById('message').value='今日が誕生日です';document.getElementById('composer').dispatchEvent(new window.Event('submit',{cancelable:true}));await settle();
const event=(type,data)=>stream.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`));
event('reply',{turn_id:7,assistant_text:'覚えておくね。',assistant_at:'2026-10-21T12:00:00+09:00',expression:'neutral'});await settle();
assert(!document.getElementById('send').disabled);assert.equal(document.querySelectorAll('.day-notice').length,0);
// A successful fast reply deliberately retains the read-receipt timer. Advance
// it explicitly before asserting the number of background polling timers.
assert.equal(timers.size,1);const [readId,readTimer]=timers.entries().next().value;
assert.equal(readTimer.ms,cfg.display?.read_seconds*1000||1200);timers.delete(readId);readTimer.fn();
assert(document.querySelector('.mine .read-receipt'));
assert.equal(document.querySelector('.mine time').dateTime,new FixedDate().toISOString());
assert.equal(document.querySelector('.mine time').textContent,`${process.argv[2]}:00`);
event('important_days',{turn_id:7,notices:['誕生日を大切な日として覚えました','<img src=x onerror=alert(1)>']});
event('important_days',{turn_id:7,notices:['重複']});await settle();
assert.equal(document.querySelectorAll('.day-notice').length,1);assert.equal(document.querySelector('.day-notice img'),null);
assert.equal(document.querySelector('.day-notice a').getAttribute('href'),'#settings/relationship?focus=days');stream.close();await settle();
await view('#settings/relationship?focus=days');assert.equal(document.activeElement.id,'partner-days');assert(!document.getElementById('settings-content').hidden);
assert(document.getElementById('partner-days').textContent.includes('ソラの誕生日'));
await click('あなたの誕生日を追加');let numbers=[...document.querySelectorAll('.day-date-fields input')];
assert.deepEqual(numbers.map(e=>e.type),['number','number','number']);input(numbers[1],'10');input(numbers[2],'21');
await click('記念日を追加');input([...document.querySelectorAll('.day-row')].at(-1).querySelector('input'),'出会った日');
numbers=[...[...document.querySelectorAll('.day-row')].at(-1).querySelectorAll('.day-date-fields input')];input(numbers[1],'10');input(numbers[2],'24');
await click('大切な日を保存');assert.equal(state.days.length,2);assert.equal(state.days[0].year,null);
const version=state.version;failSave=true;await click('大切な日を保存');assert.equal(state.version,version);assert(document.getElementById('error').textContent.includes('日付を確認'));failSave=false;
await view('#settings/pace');await click('習慣を追加');const row=document.querySelector('.habit-row');input(row.querySelector('input'),'おはよう');input(row.querySelector('input[type=time]'),'07:00');
const quiet=[...row.querySelectorAll('label')].find(e=>e.textContent.includes('静かな時間帯')).querySelector('input');quiet.checked=true;quiet.onchange();
await click('時刻の習慣を保存');assert.equal(state.days.length,2);assert.equal(state.habits[0].time,'07:00');assert(state.habits[0].allow_quiet);assert.equal(timers.size,1);
const before=count('/api/proactive/habit');await poll();assert.equal(count('/api/proactive/habit'),before,'settings view never speaks in browser');
await view('#note');await click('ふたりのこと');assert(document.querySelector('.nearby-days').textContent.includes('今日'));assert.equal(document.querySelector('.nearby-days img'),null);assert(document.querySelector('.nearby-days').textContent.includes('<img src=x>'));
await view('#talk');assert.equal(document.querySelectorAll('.day-notice').length,1);await poll();assert.equal(count('/api/proactive/habit'),before+1);assert(document.querySelector('[data-turn-id="11"] .text').textContent.includes('おはよう'));
Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});await poll();assert.equal(count('/api/proactive/habit'),before+1);
Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
document.documentElement.dataset.theme='dark';window.innerWidth=375;
const css=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');assert(css.includes('.day-date-fields'));assert(css.includes('max-width:480px'));
assert(calls.filter(c=>c.route==='/api/partner/days').every(c=>c.path.includes('character_id=sora')));
assert.equal(count('/api/proactive/idle'),0);timers.clear();dom.window.close();globalThis.Date=RealDate;
console.log('PASS HB1 offline DOM: neutral fields, optional year, dates/habits save/error, quiet override, SSE after reply/restore/dedupe/escaping, settings anchor, nearby header, one browser timer, talk/visibility gates and phone/theme CSS');
