import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
if(!process.argv[2]){
  for(const mode of ['browser','resident'])process.stdout.write(execFileSync(process.execPath,[new URL(import.meta.url).pathname.replace(/^\/(\w:)/,'$1'),mode],{encoding:'utf8'}));
  process.exit(0);
}
const resident=process.argv[2]==='resident';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'http://127.0.0.1:18765/#talk',runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
let scrolled;
window.HTMLElement.prototype.scrollIntoView=function(){scrolled=this.dataset.turnId;};
const nativeEvents=new Map(),notifications=[],syncs=[],context={token:'owner-token',visible:false,minimized:true,focused:false,foreground:false,everShown:false};
let preferences={alwaysVisible:false,shortcut:'Ctrl+Alt+A'},failShortcut=false;
let talkTarget;
if(resident)window.__TAURI__={core:{invoke:async(command,args)=>{
  if(command==='resident_ptt_state')return false;
  if(command==='resident_ptt_preferences')return {shortcut:args?.shortcut||null,error:null};
  if(command==='resident_context')return {...context};
  if(command==='resident_take_talk'){const target=talkTarget;talkTarget=null;return target;}
  if(command==='resident_opening'){if(args.deferred){context.openingClaimed=false;return false;}const claimed=!context.openingClaimed;context.openingClaimed=true;return claimed;}
  if(command==='resident_ui_ready')return;
  if(command==='resident_notify'){notifications.push(args);return;}
  if(command==='resident_sync'){syncs.push(args.face);return;}
  if(command==='resident_preferences'){
    if(args?.preferences){if(failShortcut)throw Error('このキーは登録できません。他のアプリが使用中の可能性があります。');preferences=structuredClone(args.preferences);}
    return {preferences:structuredClone(preferences),shortcutError:null};
  }
  throw Error(command);
}},event:{listen:async(event,callback)=>{nativeEvents.set(event,callback);return ()=>nativeEvents.delete(event);}}};
let serial=0,stream,audioPlays=0,audioPauses=0,failIdle=false;
globalThis.Audio=class{play(){audioPlays++;return Promise.resolve();}pause(){audioPauses++;}};
const timers=new Map(),faceTimers=new Map(),realTimeout=globalThis.setTimeout,realClear=globalThis.clearTimeout;
globalThis.setTimeout=(fn,ms,...args)=>{if(ms>=10000){const id=++serial;(ms>=60000?timers:faceTimers).set(id,{fn,ms});return id;}return realTimeout(fn,ms,...args);};
globalThis.clearTimeout=id=>{if(timers.has(id))timers.delete(id);else if(faceTimers.has(id))faceTimers.delete(id);else realClear(id);};
const cfg=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from pathlib import Path; from cbs.config import load_config; print(json.dumps(load_config(data_dir=Path('.tmp/m12/node_config')).model_dump()))"],{encoding:'utf8'}));
Object.assign(cfg,{ai_ready:true,connection_verified:true,warnings:[],keys:{},reindex_required:false});
Object.assign(cfg.autonomy,{idle_nudge:true,idle_poll_seconds:120});cfg.tts.enabled=true;cfg.tts.autoplay_proactive=true;
const calls=[],response=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
let nextTurn=10,habitMode=false,silentIdle=false;
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,path,body,options});
  if(route==='/api/partner')return response({partner:{id:'yuki',name:'ユキ'}});
  if(route==='/api/resident/face')return response({characterId:'yuki',name:'ユキ',expression:'smile',revision:11,quiet:false,validForMs:15000,fallbackExpression:'thinking',replyRemainingMs:600000});
  if(route==='/api/settings')return response(cfg);
  if(route==='/api/look')return response({expressions:[]});
  if(route==='/api/note')return response({name:'ユキ',interests:[],activities:[],past:[],self_forming:[]});
  if(route==='/api/sessions')return response(body?{id:'session'}:[]);
  if(route==='/api/proactive/opening')return response({speak:false});
  if(route==='/api/proactive/idle'){
    if(failIdle)return new Response(JSON.stringify({detail:'確認を待っています。'}),{status:503});
    if(silentIdle)return response({speak:false,reason:'今は黙る'});
    return response({speak:true,turn_id:++nextTurn,assistant_text:'今日はどう？ 続きの文。',assistant_at:'2026-10-20T12:00:00+09:00',expression:'neutral',...(habitMode?{habit:{allow_quiet:true}}:{})});
  }
  if(route.endsWith('/audio/stream'))return new Response(new Blob(['wav'],{type:'audio/wav'}));
  if(route.endsWith('/messages/stream'))return new Response(new ReadableStream({start(controller){stream=controller;}}));
  throw Error('Unexpected offline endpoint '+route);
};
const settle=async()=>{for(let i=0;i<18;i++)await new Promise(r=>setImmediate(r));};
const count=route=>calls.filter(c=>c.route===route).length;
const poll=async()=>{assert.equal(timers.size,1);const [id,timer]=timers.entries().next().value;assert.equal(timer.ms,120000);timers.delete(id);await timer.fn();await settle();};
await import('../cbs/web/static/user.js');await settle();
const client=await import('../cbs/web/static/resident.js');
const parent=document.createElement('div'),draft=structuredClone(cfg);
client.idleNudgeControl(parent,cfg,draft);
assert.equal(parent.querySelectorAll('input').length,resident?1:0);
if(!resident){
  assert.equal(await client.residentControls(parent),null);
  assert.equal(count('/api/proactive/opening'),1);assert.equal(timers.size,0);
  assert.deepEqual(client.residentSetting(draft),{});
  await client.notifyResident({name:'ユキ',id:'yuki'},{turn_id:2,assistant_text:'こんにちは。'},'session');
  assert.equal(notifications.length,0);assert.equal(count('/api/proactive/idle'),0);
  assert(calls.every(c=>!c.options.headers?.['X-Awalia-Desktop']));
}else{
  assert.equal(syncs.length,1);assert.equal(syncs[0].expression,'smile');assert.equal(syncs[0].fallbackExpression,'thinking');assert(syncs[0].replyRemainingMs<=600000&&syncs[0].replyRemainingMs>0);assert.equal(faceTimers.size,1);
  window.matchMedia=()=>({matches:true});
  const reducedControls=document.createElement('div');await client.residentControls(reducedControls);
  assert.equal(reducedControls.querySelector('#resident-idle-motion').checked,false);
  window.matchMedia=()=>({matches:false});
  const controls=document.createElement('div'),saveNative=await client.residentControls(controls);
  assert.equal(controls.querySelector('#resident-idle-motion').checked,true);
  assert.equal(controls.querySelector('#resident-face-size').value,'96');
  assert(controls.querySelector('#resident-face-size').parentElement.textContent.includes('顔の大きさ'));
  assert(controls.querySelector('#resident-chime-enabled').parentElement.textContent.includes('話しかけの音を鳴らす'));
  assert.equal(controls.querySelector('#resident-chime-enabled').checked,true);
  assert.equal(controls.querySelector('#resident-chime-volume').value,'40');
  controls.querySelector('#resident-face-size').value='128';
  controls.querySelector('#resident-chime-volume').value='25';
  controls.querySelector('#resident-always-visible').checked=true;
  const keys=controls.querySelector('#resident-shortcut');keys.value='Ctrl+Shift+Y';
  await saveNative();assert.deepEqual(preferences,{alwaysVisible:true,faceSize:128,chimeEnabled:true,chimeVolume:25,idleMotion:true,shortcut:'Ctrl+Shift+Y'});
  controls.querySelector('#resident-idle-motion').checked=false;await saveNative();assert.equal(preferences.idleMotion,false);
  failShortcut=true;keys.value='Ctrl+Alt+B';await assert.rejects(saveNative,/登録できません/);
  assert(controls.querySelector('#resident-shortcut-status').textContent.includes('使用中'));assert.equal(preferences.shortcut,'Ctrl+Shift+Y');
  failShortcut=false;
  const sound=controls.querySelector('#resident-chime-enabled'),volume=controls.querySelector('#resident-chime-volume');
  sound.checked=false;sound.onchange();assert(volume.disabled);await saveNative();assert.equal(preferences.chimeEnabled,false);assert.equal(preferences.chimeVolume,25);
  sound.checked=true;sound.onchange();assert(!volume.disabled);
  const enabled=controls.querySelector('#resident-shortcut-enabled');enabled.checked=false;enabled.onchange();assert(keys.disabled);await saveNative();assert.equal(preferences.shortcut,null);
  assert.equal(count('/api/proactive/opening'),0,'hidden autostart holds opening');
  assert(calls.every(c=>c.options.headers?.['X-Awalia-Desktop']==='owner-token'));
  parent.querySelector('input').checked=false;parent.querySelector('input').onchange();
  assert.deepEqual(client.residentSetting(draft),{idle_nudge:false});
  const [openingId,openingTimer]=[...timers].find(([,t])=>t.ms===90000);
  timers.delete(openingId);await openingTimer.fn();await settle();
  assert.equal(count('/api/proactive/opening'),1,'hidden startup judges after the configured delay');
  await poll();assert.equal(count('/api/proactive/idle'),1);
  assert(document.querySelector('[data-turn-id="11"] .text').textContent.includes('今日はどう'));
  assert.equal(notifications.length,1);assert.equal(notifications[0].body,'今日はどう？');
  assert.equal(notifications[0].title,'ユキ');assert.equal(notifications[0].sessionId,'session');
  assert.equal(audioPlays,0);assert.equal(calls.find(c=>c.route==='/api/proactive/idle').body.after_turn_id,0);
  silentIdle=true;const silentBefore=count('/api/proactive/idle');
  await poll();await poll();assert.equal(count('/api/proactive/idle'),silentBefore+2,'silence keeps polling so server can recheck the same turn');
  assert.equal(notifications.length,1);silentIdle=false;
  nextTurn--;await poll();assert.equal(document.querySelectorAll('[data-turn-id="11"]').length,1);assert.equal(notifications.length,1,'a repeated turn never notifies twice');
  Object.assign(context,{everShown:true,visible:true,minimized:false});
  nativeEvents.get('resident-visibility')({payload:{...context}});await settle();
  assert.equal(count('/api/proactive/opening'),1);
  await poll();assert.equal(notifications.length,2);assert.equal(audioPlays,1,'unfocused visible window autoplays');
  Object.assign(context,{foreground:true,focused:true});nativeEvents.get('resident-visibility')({payload:{...context}});await settle();
  await poll();assert.equal(notifications.length,2);assert.equal(audioPlays,2,'foreground idle can autoplay');
  assert.equal(count('/api/proactive/opening'),1,'reopen never repeats opening');
  const pausesBefore=audioPauses;
  Object.assign(context,{foreground:false,focused:false});nativeEvents.get('resident-visibility')({payload:{...context}});await settle();
  assert.equal(audioPauses,pausesBefore,'focus loss keeps in-flight audio playing');
  context.minimized=true;nativeEvents.get('resident-visibility')({payload:{...context}});await settle();
  assert(audioPauses>pausesBefore,'minimize stops in-flight playback');
  document.getElementById('message').value='入力中の会話';
  document.getElementById('composer').dispatchEvent(new window.Event('submit',{cancelable:true}));await settle();
  const before=count('/api/proactive/idle');await poll();assert.equal(count('/api/proactive/idle'),before,'no overlapping conversation');
  stream.enqueue(new TextEncoder().encode('event: reply\ndata: '+JSON.stringify({turn_id:20,assistant_text:'聞いているよ。',assistant_at:'2026-10-20T12:01:00+09:00',expression:'neutral'})+'\n\n'));stream.close();await settle();
  assert.equal(audioPlays,2,'minimized SSE response never autoplays');
  cfg.autonomy.idle_nudge=false;await poll();assert.equal(count('/api/proactive/idle'),before);
  cfg.autonomy.idle_nudge=true;failIdle=true;await poll();assert(document.getElementById('error').textContent.includes('確認を待っています'));assert.equal(timers.size,1,'errors retain bounded schedule');
  // HB1 reuses this resident timer even with ordinary idle nudges disabled.
  failIdle=false;habitMode=true;cfg.autonomy.idle_nudge=false;cfg.days.has_habits=true;
  const habitBefore=count('/api/proactive/idle');await poll();assert.equal(count('/api/proactive/idle'),habitBefore+1);
  assert.equal(notifications.at(-1).allowQuiet,true,'only the selected habit carries its quiet exception');
  assert.equal(timers.size,1,'habits do not add a second resident timer');
  location.hash='#note';window.dispatchEvent(new window.HashChangeEvent('hashchange'));await settle();assert.equal(document.querySelector('[data-turn-id="11"]'),null);
  talkTarget={characterId:'yuki',sessionId:'session',turnId:11};nativeEvents.get('resident-open-talk')({payload:talkTarget});await new Promise(r=>realTimeout(r,20));await settle();
  assert.equal(location.hash,'#talk');assert.equal(scrolled,'11');
  location.hash='#note';window.dispatchEvent(new window.HashChangeEvent('hashchange'));await settle();
  nativeEvents.get('resident-focus-input')({payload:null});await new Promise(r=>realTimeout(r,20));await settle();
  assert.equal(location.hash,'#talk');assert.equal(document.activeElement.id,'message');
  assert.equal(client.firstSentence('「こんにちは。」\n次の文。'),'「こんにちは。」');
  assert.equal(client.firstSentence('Hello there. Another sentence.'),'Hello there.');
}
timers.clear();faceTimers.clear();dom.window.close();
console.log(`PASS M12 offline DOM ${resident?'resident: deferred opening, configured idle, append/dedupe, hidden/unfocused notification, native target, visible/unfocused audio, minimized silence, busy/off/error paths and setting':'browser: normal opening, no timer, no native settings or notifications'}`);
