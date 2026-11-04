import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
if(!process.argv[2]){
  for(const mode of ['hidden','silent','show','quiet','quiet-show','race','configured','busy','stop'])
    process.stdout.write(execFileSync(process.execPath,[fileURLToPath(import.meta.url),mode],{encoding:'utf8'}));
  process.exit(0);
}
const mode=process.argv[2],require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'http://127.0.0.1:18765/#talk',runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
const context={token:'test-owner',visible:false,minimized:true,focused:false,foreground:false,everShown:false},events=new Map(),notifications=[];
window.__TAURI__={core:{invoke:async(command,args)=>{
  if(command==='resident_context')return {...context};
  if(command==='resident_take_talk')return null;
  if(command==='resident_opening'){if(args.deferred){context.openingClaimed=false;return false;}const claimed=!context.openingClaimed;context.openingClaimed=true;return claimed;}
  if(command==='resident_preferences')return {preferences:{},shortcutError:null};
  if(command==='resident_notify'){notifications.push(args);return;}
  if(['resident_sync','resident_ui_ready'].includes(command))return;
  if(command==='resident_ptt_state')return false;
  throw Error(command);
}},event:{listen:async(name,fn)=>events.set(name,fn)}};
let serial=0,audioPlays=0,quiet=mode.startsWith('quiet')||mode==='race',release;
globalThis.Audio=class{play(){audioPlays++;return Promise.resolve();}pause(){}};
const timers=new Map(),realTimeout=globalThis.setTimeout,realClear=globalThis.clearTimeout;
globalThis.setTimeout=(fn,ms,...args)=>{
  if(ms>=1000){const id=++serial;timers.set(id,{fn,ms});return id;}
  return realTimeout(fn,ms,...args);
};
globalThis.clearTimeout=id=>{if(!timers.delete(id))realClear(id);};
const cfg=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from pathlib import Path; from cbs.config import load_config; print(json.dumps(load_config(data_dir=Path('.tmp/rs1/node_config')).model_dump()))"],{encoding:'utf8'}));
Object.assign(cfg,{ai_ready:true,connection_verified:true,warnings:[],keys:{},reindex_required:false});
Object.assign(cfg.autonomy,{idle_nudge:true,idle_poll_seconds:180,resident_opening_delay_seconds:mode==='configured'?450:90});
cfg.tts.enabled=true;
const calls=[],response=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,body});
  if(route==='/api/partner')return response({partner:{id:'sora',name:'ソラ'}});
  if(route==='/api/settings')return response(cfg);
  if(route==='/api/look')return response({expressions:[]});
  if(route==='/api/sessions')return response(body?{id:'session'}:[]);
  if(route==='/api/resident/face')return response({quiet,validForMs:15000});
  if(route==='/api/proactive/opening'){
    if(mode==='race'&&body.background)await new Promise(resolve=>release=resolve);
    if(quiet&&body.background)return response({speak:false,deferred:true});
    if(mode==='silent')return response({speak:false});
    return response({speak:true,turn_id:11,assistant_text:'こんにちは。少し話そう。',assistant_at:'2026-10-25T12:00:00+09:00',expression:'neutral'});
  }
  if(route==='/api/proactive/idle')return response({speak:false});
  if(route.endsWith('/audio/stream'))return new Response(new Blob(['wav'],{type:'audio/wav'}));
  throw Error('Unexpected offline endpoint '+route);
};
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
const count=route=>calls.filter(c=>c.route===route).length;
const openings=()=>calls.filter(c=>c.route==='/api/proactive/opening');
const fire=async ms=>{const entry=[...timers].find(([,t])=>t.ms===ms);assert(entry,`timer ${ms}`);timers.delete(entry[0]);await entry[1].fn();await settle();};
const show=async()=>{
  Object.assign(context,{visible:true,minimized:false,everShown:true,foreground:true,focused:true});
  events.get('resident-visibility')({payload:{...context}});await settle();
};
if(['busy','stop'].includes(mode)){
  const {startResident}=await import('../cbs/web/static/resident.js');
  let blocked=true,opened=0;
  const stop=await startResident({opening:async()=>{opened++;return {speak:false};},settings:async()=>cfg,idle:async()=>({speak:false}),busy:()=>blocked,onReply:()=>{},onOpen:()=>{},onError:error=>{throw error;}});
  if(mode==='stop'){stop();assert.equal(timers.size,0);await show();assert.equal(opened,0);}
  else{await fire(90000);assert.equal(opened,0);blocked=false;await fire(180000);assert.equal(opened,1);await show();assert.equal(opened,1);stop();assert.equal(timers.size,0);}
}else{
  await import('../cbs/web/static/user.js');await settle();
  assert.equal(openings().length,0,'hidden startup waits');
  if(mode==='show'){
    await show();assert.equal(openings().length,1);assert(!openings()[0].body.background);
    assert(![...timers.values()].some(t=>t.ms===90000));assert.equal(notifications.length,0);
  }else{
    if(mode==='configured'){await fire(180000);assert.equal(openings().length,0,'idle polling cannot bypass startup delay');}
    const pending=fire(cfg.autonomy.resident_opening_delay_seconds*1000);
    if(mode==='race'){
      await settle();assert.equal(openings().length,1);await show();assert.equal(openings().length,1,'show during pending request never overlaps');release();
    }
    await pending;
    assert(openings()[0].body.background);
    if(mode.startsWith('quiet')){
      assert.equal(notifications.length,0);assert.equal(count('/api/proactive/idle'),0);
      await fire(180000);assert.equal(openings().length,2);assert.equal(count('/api/proactive/idle'),0);
      if(mode==='quiet-show')await show();else{quiet=false;await fire(180000);}
      assert.equal(openings().length,3);
      assert.equal(Boolean(openings().at(-1).body.background),mode==='quiet');
    }
    if(mode==='race')assert.equal(openings().length,2,'deferred response hands off to the show event');
    if(mode==='silent'){
      assert.equal(notifications.length,0);await fire(180000);assert.equal(count('/api/proactive/idle'),1,'silent opening still enables idle polling');
    }else{
      assert.equal(document.querySelectorAll('[data-turn-id="11"]').length,1);
      assert.equal(notifications.length,['quiet-show','race'].includes(mode)?0:1);
      if(notifications.length){assert.equal(notifications[0].body,'こんにちは。');assert.equal(notifications[0].sessionId,'session');assert.equal(audioPlays,0,'hidden opening keeps UI4 audio silence');}
    }
  }
  const before=openings().length;await show();await fire(180000);assert.equal(openings().length,before,'show and later ticks never repeat a completed decision');
  assert.equal(document.getElementById('error').textContent,'');
}
timers.clear();dom.window.close();
console.log(`PASS RS1 offline DOM ${mode}`);
