// Native window state and all audio/network boundaries are fake.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><article data-turn-id="1"></article></body>',{url:'http://127.0.0.1:18765/'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
Object.defineProperty(document,'visibilityState',{value:'hidden',configurable:true});
const context={token:'fake',visible:false,minimized:true,focused:false,foreground:false,everShown:true},events=new Map();
window.__TAURI__={core:{invoke:async command=>{assert.equal(command,'resident_context');return {...context};}},event:{listen:async(name,fn)=>events.set(name,fn)}};
const audio=[],requests=[];
globalThis.Audio=class{constructor(){audio.push(this);}play(){return Promise.resolve();}pause(){this.paused=true;}};
let response=()=>new Response(new Blob(['wav'],{type:'audio/wav'}));
globalThis.fetch=async(path,options)=>{requests.push({path,options});return response();};
const settle=async()=>{for(let i=0;i<25;i++)await new Promise(r=>setImmediate(r));};
const resident=await import('../cbs/web/static/resident.js');
const voice=await import('../cbs/web/static/voice.js');await settle();
const article=document.querySelector('article');
voice.configureVoice({enabled:true,autoplay:true});
voice.voiceButton(article,1,{autoplay:true});await settle();assert.equal(audio.length,0);assert.equal(requests.length,0);
// No visibility event: an authoritative command must still see the new window state.
Object.assign(context,{visible:true,minimized:false});
voice.autoplayTurn(1);await settle();assert.equal(audio.length,1);assert.equal(resident.residentForeground(),false);
assert.equal(resident.residentVisible(),true,'native visibility wins over document hidden and focus');
events.get('resident-visibility')({payload:{...context}});assert(!audio[0].paused,'focus loss alone keeps playback');
Object.assign(context,{minimized:true,focused:true,foreground:false});
events.get('resident-visibility')({payload:{...context}});assert(audio[0].paused,'even a focused minimized window stops automatic playback');
voice.autoplayTurn(1);await settle();assert.equal(audio.length,1);
Object.assign(context,{visible:false,minimized:false,focused:false});events.get('resident-visibility')({payload:{...context}});
voice.autoplayTurn(1);await settle();assert.equal(audio.length,1,'tray window stays silent');
await article.querySelector('button').onclick();await settle();assert.equal(audio.length,2,'manual playback keeps existing behavior');
events.get('resident-visibility')({payload:{...context}});assert(!audio[1].paused,'native hide does not cancel manual playback');voice.stopVoice();
// Hide while synthesis is pending, without an event; the fresh native query prevents playback.
Object.assign(context,{visible:true,minimized:false});let release;
response=()=>new Promise(resolve=>release=resolve);voice.autoplayTurn(1);await settle();
context.visible=false;release(new Response(new Blob(['wav'])));await settle();assert.equal(audio.length,2);
// Every queued SSE chunk checks native state again, including when no event arrives.
Object.assign(context,{visible:true,minimized:false});let stream;
response=()=>new Response(new ReadableStream({start(controller){stream=controller;}}),{headers:{'Content-Type':'text/event-stream'}});
voice.autoplayTurn(1);await settle();
const chunk='event: audio\ndata: '+JSON.stringify({wav:btoa('wav')})+'\n\n';
stream.enqueue(new TextEncoder().encode(chunk+chunk));await settle();assert.equal(audio.length,3);
context.minimized=true;audio[2].onended();await settle();assert.equal(audio.length,3,'queued chunk is suppressed on minimize');
// A late response from a cancelled generation cannot replace a newer manual playback.
voice.stopVoice();Object.assign(context,{visible:true,minimized:false});
response=()=>new Promise(resolve=>release=resolve);voice.autoplayTurn(1);await settle();
response=()=>new Response(new Blob(['new']));await article.querySelector('button').onclick();await settle();
assert.equal(article.querySelector('button').getAttribute('aria-pressed'),'false');
await article.querySelector('button').onclick();await settle();
const manual=audio.at(-1),before=audio.length;release(new Response(new Blob(['old'])));await settle();
assert.equal(audio.length,before);assert(!manual.paused);
voice.stopVoice();dom.window.close();
console.log('PASS UI4 voice DOM: native visible/unfocused, minimized/tray silence, manual playback, synthesis hide, queued SSE suppression and cancellation race');
