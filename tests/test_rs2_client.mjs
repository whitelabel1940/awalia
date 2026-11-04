import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
if(!process.argv[2]){
  for(const mode of ['look','augment','reload','target','link'])process.stdout.write(execFileSync(process.execPath,[fileURLToPath(import.meta.url),mode],{encoding:'utf8'}));
  process.exit(0);
}
const mode=process.argv[2],require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const wizard=['look','augment'].includes(mode);
const dom=new JSDOM(readFileSync(new URL(`../cbs/web/static/${wizard?'wizard':'index'}.html`,import.meta.url),'utf8'),{url:`http://127.0.0.1:18765/${wizard?'wizard?'+(mode==='look'?'look=1':'augment=sora'):mode==='target'?'#talk':'?resident_turn=11#talk'}`,runScripts:'outside-only'});
for(const key of ['window','document','localStorage'])globalThis[key]=dom.window[key];
// Observe real navigation requests without asking JSDOM to implement a page load.
let navigated,reloaded=0;
globalThis.location={get search(){return window.location.search;},get hash(){return window.location.hash;},get href(){return window.location.href;},get origin(){return window.location.origin;},get pathname(){return window.location.pathname;},assign:url=>{navigated=url;},reload:()=>{reloaded++;}};
window.scrollTo=()=>{};let scrolled;
window.HTMLElement.prototype.scrollIntoView=function(){scrolled=this.dataset.turnId;};
window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};window.HTMLDialogElement.prototype.close=function(){this.open=false;};
window.HTMLCanvasElement.prototype.getContext=()=>({clearRect(){},drawImage(){}});
window.HTMLCanvasElement.prototype.toDataURL=()=> 'data:image/png;base64,AA==';
window.HTMLCanvasElement.prototype.toBlob=function(callback){callback(new Blob(['png'],{type:'image/png'}));};
let plays=0;globalThis.Audio=class{play(){plays++;return Promise.resolve();}pause(){}};
globalThis.createImageBitmap=async()=>({width:1200,height:600});
const events=new Map(),notifications=[],context={token:'fake-owner',visible:true,minimized:false,focused:true,foreground:true,everShown:true,elapsedMs:0,openingClaimed:!['reload','look'].includes(mode)};
let talkTarget=mode==='target'?{characterId:'sora',sessionId:'session',turnId:11}:null;
window.__TAURI__={core:{invoke:async(command,args)=>{
  if(command==='resident_context')return {...context};
  if(command==='resident_preferences')return {preferences:{},shortcutError:null};
  if(command==='resident_take_talk'){const target=talkTarget;talkTarget=null;return target;}
  if(command==='resident_opening'){if(args.deferred){context.openingClaimed=false;return false;}const claim=!context.openingClaimed;context.openingClaimed=true;return claim;}
  if(command==='resident_notify'){notifications.push(args);return;}
  if(['resident_sync','resident_ui_ready'].includes(command))return;
  if(command==='resident_ptt_state')return false;
  throw Error(command);
}},event:{listen:async(name,fn)=>events.set(name,fn)}};
let serial=0;const timers=new Map(),realTimeout=globalThis.setTimeout,realClear=globalThis.clearTimeout;
globalThis.setTimeout=(fn,ms,...args)=>{if(ms>=1000||ms===0){const id=++serial;timers.set(id,{fn,ms});return id;}return realTimeout(fn,ms,...args);};
globalThis.clearTimeout=id=>{if(!timers.delete(id))realClear(id);};
const cfg={ai_ready:true,connection_verified:true,autonomy:{idle_nudge:true,idle_poll_seconds:180,resident_opening_delay_seconds:90},days:{has_habits:true,poll_seconds:180},tts:{enabled:true,autoplay:true,autoplay_proactive:true},display:{},stt:{},photos:{}};
const lookCatalog=wizard&&mode==='look'?JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c','import json; from cbs.look import field_catalog; print(json.dumps(field_catalog()))'],{encoding:'utf8'})):null;
const calls=[],turns=[{id:11,assistant_text:'少し話せる？',created_at:'2026-10-25T12:00:00+09:00',assistant_at:'2026-10-25T12:00:00+09:00',kind:'proactive',status:'completed',user_text:''}];
let nextTurn=11,holdDraft=false,releaseDraft,holdJob=false,releaseJob,failSave=false;
const json=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=typeof options.body==='string'?JSON.parse(options.body):null;calls.push({route,body,method:options.method});
  if(route==='/api/partner')return json({partner:{id:'sora',name:'ソラ'}});
  if(route==='/api/settings')return json(cfg);
  if(route==='/api/look')return json({expressions:[]});
  if(route==='/api/sessions')return json([{id:'session'}]);
  if(route==='/api/sessions/session/turns')return json(turns);
  if(route==='/api/proactive/idle'){const result={speak:true,turn_id:++nextTurn,assistant_text:'少し話せる？ 続き。',assistant_at:'2026-10-25T13:00:00+09:00',expression:'neutral',habit:{allow_quiet:true}};turns.push({...result,id:result.turn_id,status:'completed',kind:'proactive',created_at:result.assistant_at});return json(result);}
  if(route==='/api/proactive/opening')return json({speak:false});
  if(route==='/api/resident/face')return json({validForMs:15000,quiet:false});
  if(route.endsWith('/audio/stream'))return new Response(new Blob(['wav']));
  if(route==='/api/look/fields')return json(lookCatalog);
  if(route==='/api/look/prompt')return json({prompt:'画像の指示文'});
  if(route==='/api/look/draft'){if(holdDraft)await new Promise(resolve=>releaseDraft=resolve);return json({assignments:[],suggestions:[]});}
  if(route==='/api/look/meta'){if(failSave)return new Response(JSON.stringify({detail:'保存に失敗しました'}),{status:422});return json({});}
  if(route==='/api/look/cover')return json({});
  if(route==='/api/bootstrap/questionnaire')return json({questionnaire:{sections:[],common_choices:{leave_open:'任せる'}},domains:[]});
  if(route==='/api/characters/sora/augment'){
    if(body){if(holdJob)await new Promise(resolve=>releaseJob=resolve);return json({status:'completed',result:{}});}
    return json({questions:[{id:'R1',section:'relationship',scene:'場面の質問',choices:{leave_open:'任せる',A:'答える'},multi:false}]});
  }
  throw Error('Unexpected offline endpoint '+route);
};
const settle=async()=>{for(let i=0;i<30;i++)await new Promise(r=>setImmediate(r));};
const count=route=>calls.filter(c=>c.route===route).length;
const fire=async ms=>{const entry=[...timers].find(([,t])=>t.ms===ms);assert(entry,`timer ${ms}`);timers.delete(entry[0]);await entry[1].fn();await settle();};
const click=async text=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent===text);assert(b,text);await b.onclick();await settle();};
const nativeClick=async()=>{talkTarget={characterId:'sora',sessionId:'session',turnId:nextTurn};events.get('resident-open-talk')({payload:talkTarget});await settle();};
if(wizard){
  await import('../cbs/web/static/wizard.js');await settle();
  assert.equal(document.getElementById('error').textContent,'');
  assert.equal(count('/api/proactive/opening'),mode==='look'?1:0,'wizard greets only on a new native launch');
  await fire(180000);
  assert.equal(count('/api/proactive/idle'),1);assert.equal(notifications.length,1);
  assert.equal(notifications[0].outsideTalk,true,'foreground wizard still gets a corner bubble');assert.equal(notifications[0].allowQuiet,true);
  assert.equal(notifications[0].body,'少し話せる？');assert.equal(plays,1,'visible wizard follows automatic voice rule');
  Object.assign(context,{visible:false,minimized:true,foreground:false});events.get('resident-visibility')({payload:{...context}});
  cfg.autonomy.idle_nudge=false;await fire(180000);assert.equal(count('/api/proactive/idle'),2,'habits reuse the same loop when ordinary nudges are off');assert.equal(plays,1,'hidden wizard stays silent');
  Object.assign(context,{visible:true,minimized:false,foreground:true});events.get('resident-visibility')({payload:{...context}});
  cfg.days.has_habits=false;await fire(180000);assert.equal(count('/api/proactive/idle'),2);
  cfg.autonomy.idle_nudge=true;cfg.days.has_habits=true;
  assert.equal(count('/api/proactive/opening'),mode==='look'?1:0,'later visibility and ticks never repeat the launch greeting');
  const before=count('/api/proactive/idle');
  if(mode==='look'){
    holdDraft=true;const working=click('設定文と性格から下書きする');await settle();await fire(180000);assert.equal(count('/api/proactive/idle'),before,'look generation is busy');releaseDraft();await working;
    // A dropped image lives in editor state even though the file input stays empty.
    const drop=document.querySelector('.look-cover .drop');await drop.ondrop({preventDefault(){},dataTransfer:{files:[{type:'image/png',size:100}]}});await settle();
  }else{
    const answer=document.querySelector('input[value=A]');answer.checked=true;answer.dispatchEvent(new window.Event('change',{bubbles:true}));
  }
  await nativeClick();assert(document.querySelector('dialog[open]'));assert.equal(navigated,undefined);
  await click('とどまる');assert.equal(navigated,undefined);assert(turns.some(t=>t.id===nextTurn),'staying keeps the saved proactive turn');
  await nativeClick();
  if(mode==='look'){
    failSave=true;await click('保存して移る');assert(document.querySelector('dialog[open]'));assert(document.querySelector('dialog').textContent.includes('保存に失敗'));assert.equal(navigated,undefined);
    failSave=false;await click('保存して移る');assert(count('/api/look/meta')>=2);assert.equal(count('/api/look/cover'),2);
  }else{
    holdJob=true;const saving=click('保存して移る');await settle();await fire(180000);assert.equal(count('/api/proactive/idle'),before,'augment saving is busy');releaseJob();await saving;
    assert(calls.some(c=>c.route==='/api/characters/sora/augment'&&c.body?.answers.R1.answer.includes('A')));
  }
  assert.equal(navigated,`/?character=sora&resident_turn=${nextTurn}#talk`);
  // Discard has no save side effect and the clean state also navigates directly.
  if(mode==='look'){
    document.getElementById('screen').replaceChildren();const {editLook}=await import('../cbs/web/static/settings.js');await editLook(document.getElementById('screen'));await settle();
    const e=document.querySelector('[data-look-path="person.hair.color"]');e.value='青';e.dispatchEvent(new window.Event('input',{bubbles:true}));await settle();
  }
  if(mode==='look'){navigated=undefined;await nativeClick();const saved=count('/api/look/meta');await click('捨てて移る');assert.equal(count('/api/look/meta'),saved);assert(navigated);}
  window.dispatchEvent(new window.Event('pagehide'));assert(![...timers.values()].some(t=>[10000,180000,90000].includes(t.ms)),'unloaded wizard leaves no resident loops');
}else if(mode==='reload'){
  const {startResident}=await import('../cbs/web/static/resident.js');let greeted=0,blocked=false;
  const args={settings:async()=>cfg,busy:()=>blocked,opening:async()=>{greeted++;return {speak:false};},idle:async()=>({speak:false}),onReply:()=>{},onOpen:()=>{},onError:e=>{throw e;}};
  let stop=await startResident(args);await settle();assert.equal(greeted,1);stop();localStorage.clear();
  stop=await startResident(args);await settle();await fire(180000);assert.equal(greeted,1,'reload uses native state, independent of browser storage');stop();
  context.openingClaimed=false;stop=await startResident(args);await settle();assert.equal(greeted,2,'new native process gets one greeting');stop();
  Object.assign(context,{openingClaimed:false,everShown:false,visible:false,elapsedMs:60000});
  stop=await startResident(args);await settle();assert([...timers.values()].some(t=>t.ms===30000),'reload preserves remaining startup delay');await fire(30000);assert.equal(greeted,3);stop();
  context.openingClaimed=true;stop=await startResident(args);await settle();
  context.openingClaimed=false;context.elapsedMs=120000;await fire(180000);assert.equal(greeted,4,'quiet deferral from the previous page is picked up from native state');stop();
  assert.equal(timers.size,0);
  window.dispatchEvent(new window.PageTransitionEvent('pageshow',{persisted:true}));assert.equal(reloaded,1,'cached navigation return rebuilds history and the stopped loop');
}else{
  await import('../cbs/web/static/user.js');await settle();
  assert.equal(scrolled,'11','cross-page target scrolls after history loads');assert(document.querySelector('[data-turn-id="11"]'));
  assert.equal(talkTarget,null,'native click buffered before handler setup is consumed');assert.equal(count('/api/proactive/opening'),0);
}
timers.clear();dom.window.close();console.log(`PASS RS2 offline DOM ${mode}`);
