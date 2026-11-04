import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'http://127.0.0.1:18765/#talk'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
Object.defineProperty(globalThis,'navigator',{value:window.navigator,configurable:true});
const {microphone,wavPCM}=await import('../cbs/web/static/stt.js');
const tick=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
let now=0;Object.defineProperty(performance,'now',{value:()=>now,configurable:true});
let intervals=new Map(),timer=0;globalThis.setInterval=fn=>{intervals.set(++timer,fn);return timer;};globalThis.clearInterval=id=>intervals.delete(id);
let processor,closed=0,stopped=0,denied=false,deferPermission,stopVoice=0,sends=0,locks=[],errors=[],calls=[],deferTranscription,auto=false;
const connection=()=>({connect(){},disconnect(){}});
class FakeAudioContext {
  sampleRate=48000;destination={};
  async resume(){} async close(){closed++;}
  createMediaStreamSource(){return connection();}
  createScriptProcessor(){processor={...connection(),onaudioprocess:null};return processor;}
  createGain(){return {...connection(),gain:{value:1}};}
}
window.AudioContext=FakeAudioContext;
Object.defineProperty(navigator,'mediaDevices',{value:{async getUserMedia(options){
  assert.equal(options.video,false);assert(options.audio.echoCancellation);
  if(denied){const e=new Error();e.name='NotAllowedError';throw e;}
  if(deferPermission)return await new Promise(resolve=>deferPermission=resolve);
  return {getTracks:()=>[{stop(){stopped++;}}]};
}},configurable:true});
const events=new Map();let opened=0;
window.__TAURI__={core:{invoke:async name=>{assert.equal(name,'resident_ptt_state');return false;}},event:{listen:async(name,fn)=>events.set(name,fn)}};
const button=document.getElementById('mic'),status=document.getElementById('mic-status'),input=document.getElementById('message'),composer=document.getElementById('composer');
composer.requestSubmit=()=>sends++;
const mic=microphone({button,status,input,composer,stopVoice:()=>stopVoice++,onError:e=>errors.push(e),active:()=>true,onLock:v=>locks.push(v),openTalk:async()=>{opened++;return true;},request:async(path,body,method,signal)=>{
  calls.push({path,body,signal});
  if(path.endsWith('/notice'))return {seen:true};
  if(deferTranscription)return await new Promise(resolve=>deferTranscription=resolve);
  return {text:'今日はいい天気ですね。',auto_send:auto};
}});
const configure=extra=>mic.configure({enabled:true,max_seconds:60,silence_seconds:2,silence_rms:.015,...extra});
const frame=(value=.1,count=4800)=>processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(count).fill(value)}});
await tick();configure({enabled:false,reason:'キーを設定してください。'});assert(button.disabled);assert(status.textContent.includes('キー'));
configure();assert(!button.disabled);input.value='前の文。';input.selectionStart=input.selectionEnd=input.value.length;
await button.onclick();assert(button.querySelector('svg'));assert.equal(button.getAttribute('aria-label'),'録音を止める');assert.equal(button.getAttribute('aria-pressed'),'true');assert(status.textContent.includes('Esc'));assert.equal(stopVoice,1);assert.deepEqual(locks,[true]);
frame();await button.onclick();assert.equal(sends,0);assert.equal(input.value,'前の文。今日はいい天気ですね。');assert(!mic.active);assert(!button.disabled);assert.equal(stopped,1);assert.equal(closed,1);
let wav=Buffer.from(calls.find(c=>c.body?.audio).body.audio,'base64');assert.equal(wav.readUInt32LE(24),16000);assert.equal(wav.readUInt16LE(22),1);assert.equal(wav.length,3244);assert(!calls.some(c=>/messages/.test(c.path)));
assert.equal(calls.filter(c=>c.path.endsWith('/notice')).length,1);
const before=calls.length;await button.onclick();frame();window.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',cancelable:true}));assert(!mic.active);assert.equal(calls.length,before);assert(status.textContent.includes('取り消し'));
await button.onclick();frame(0);now=2100;for(const fn of intervals.values())fn();await tick();assert(!mic.active);assert.equal(sends,0);
auto=true;await button.onclick();frame();now+=61000;for(const fn of intervals.values())fn();await tick();assert.equal(sends,1);assert(!mic.active);
deferTranscription=true;await button.onclick();frame();const finishing=button.onclick();await tick();assert(status.textContent.includes('文字起こし'));window.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape'}));const original=input.value;deferTranscription({text:'古い結果',auto_send:true});await finishing;deferTranscription=null;assert.equal(input.value,original);assert.equal(sends,1);assert(calls.at(-1).signal.aborted);
denied=true;await button.onclick();assert(!mic.active);assert(status.textContent.includes('マイク'));assert(errors.at(-1).message.includes('許可'));assert(!button.disabled);denied=false;
auto=false;events.get('resident-ptt')({payload:true});await tick();assert.equal(opened,1);events.get('resident-ptt')({payload:true});await tick();assert.equal(opened,1);frame(0);now+=3000;for(const fn of intervals.values())fn();await tick();assert(mic.active);events.get('resident-ptt')({payload:false});await tick();assert(!mic.active);assert.equal(sends,1);
deferPermission=true;events.get('resident-ptt')({payload:true});await tick();events.get('resident-ptt')({payload:false});await tick();const priorStops=stopped;deferPermission({getTracks:()=>[{stop(){stopped++;}}]});await tick();deferPermission=null;assert.equal(stopped,priorStops+1);assert(!mic.active);
assert.equal(calls.filter(c=>c.path.endsWith('/notice')).length,1);
const limited=Buffer.from(wavPCM([new Float32Array(96000)],48000,1),'base64');assert.equal(limited.length,32044);
assert.equal(intervals.size,0);dom.window.close();
console.log('PASS VO1 offline DOM: capability reason, recording/time/stop, PCM bounds, review/auto-send, insertion, silence/max, Esc/late result, denied permission, TTS stop, one notice, PTT hold/repeat/release/permission race, tracks cleanup');
