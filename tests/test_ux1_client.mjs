import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'https://unit.invalid/#talk',runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
// Deterministic clock: no real waits, downloads, audio services or LLM calls.
let now=0,serial=0;const timers=new Map();
globalThis.setTimeout=(fn,ms=0)=>{const id=++serial;timers.set(id,{fn,at:now+ms});return id;};
globalThis.clearTimeout=id=>timers.delete(id);
function advance(ms){const end=now+ms;while(true){const next=[...timers].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;timers.delete(next[0]);now=next[1].at;next[1].fn();}now=end;}
const tick=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
const response=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
const {displayDefaults,replyParts,replyBubbles,waitingPresentation,node}=await import('../cbs/web/static/ui.js');
const long='今日はゆっくり過ごしました。'.repeat(14)+'「また明日も話しましょう。」\n'+'うれしいです！'.repeat(12);
for(const text of ['短いね。','境界なし'.repeat(100),long,'a'.repeat(55)+'\n'+'b'.repeat(55),'One sentence! '.repeat(30),'前半。\n\n後半？'.repeat(30),'Full stop. '.repeat(40),'「引用。」\r\n'.repeat(40),'!?先頭も保存。'.repeat(30)]){
  const parts=replyParts(text);assert.equal(parts.join(''),text);assert(parts.length<=3);
  for(const part of parts.slice(0,-1))assert(/[。！？!?.\n][」』”’）)"']*\s*$/u.test(part));
}
assert.equal(replyParts(long).length,3);assert.deepEqual(replyParts(long,false),[long]);
// Typical short multi-sentence replies split in two; one sentence or very short text stays whole.
const typical='うん、それすごくいいね！ 今日はちょっと肌寒かったから、温かいものが食べたくなる気持ち、わかるよ。私もさっきお茶を淹れてひと息ついてたところ。';
assert.equal(replyParts(typical).length,2);assert.equal(replyParts(typical).join(''),typical);
assert.deepEqual(replyParts('おはよう。今日もよろしくね。'),['おはよう。今日もよろしくね。']);
assert.deepEqual(replyParts(typical,true,{split_min_chars:500,split_three_chars:900}),[typical]);
const detached=node('div');let cancel=replyBubbles(detached,long,{...displayDefaults,bubble_max_seconds:.2},{animate:true});
assert.equal(detached.children.length,1);advance(199);assert.equal(detached.children.length,1);advance(1);assert.equal(detached.children.length,2);cancel();advance(1000);assert.equal(detached.children.length,2);
replyBubbles(detached,long,displayDefaults);assert.equal(detached.textContent,long);assert.equal(detached.children.length,3);
let painted;const wait=waitingPresentation(displayDefaults,{paint:state=>painted={...state}});
advance(1199);assert(!painted.read&&!painted.typing);advance(1);assert(painted.read);advance(1299);assert(!painted.typing);advance(1);assert(painted.typing);wait.finish();advance(9999);assert(!painted.typing);
const failed=waitingPresentation(displayDefaults,{paint:state=>painted={...state}});failed.finish();advance(5000);assert(!painted.read&&!painted.typing,'failed requests cancel all indicators');
const fast=waitingPresentation({...displayDefaults,delay_user:true},{paint:state=>painted={...state}});assert(!painted.user);fast.finish(true);assert(painted.user&&!painted.read);advance(2199);assert(!painted.read);advance(1);assert(painted.read);advance(5000);assert(!painted.typing);

let stream,openingResolve,audioCalls=0;
const cfg={display:{...displayDefaults},ai_ready:true,connection_verified:true,tts:{enabled:true,autoplay:true},early:{notify:true},autonomy:{opening:'normal'}};
globalThis.Audio=class{async play(){} pause(){}};
URL.createObjectURL=()=> 'blob:offline';URL.revokeObjectURL=()=>{};
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0];
  if(route==='/api/partner')return response({partner:{id:'sora',name:'ソラ'}});
  if(route==='/api/settings')return response(cfg);
  if(route==='/api/look')return response({expressions:['neutral','thinking','smile']});
  if(route==='/api/sessions')return response(options.method?{id:'s'}:[]);
  if(route==='/api/proactive/opening')return new Promise(resolve=>openingResolve=resolve);
  if(route.endsWith('/messages/stream'))return new Response(new ReadableStream({start(controller){stream=controller;}}));
  if(route.endsWith('/audio/stream')){audioCalls++;return new Response(new Uint8Array([82,73,70,70]),{headers:{'Content-Type':'audio/wav'}});}
  throw Error('Unexpected offline request: '+route);
};
const $=id=>document.getElementById(id),send=text=>{$('message').value=text;$('composer').dispatchEvent(new window.Event('submit',{cancelable:true}));};
const event=(kind,data)=>stream.enqueue(new TextEncoder().encode(`event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`));
async function view(){window.dispatchEvent(new window.HashChangeEvent('hashchange'));await tick();}
const reply=(id,text=long,pending=false)=>event('reply',{turn_id:id,assistant_text:text,assistant_at:'2026-10-21T12:00:00+09:00',expression:'neutral',expression_pending:pending});
await import('../cbs/web/static/user.js');await tick();
assert.equal(document.querySelectorAll('.thinking').length,0);
openingResolve(response({speak:true,turn_id:1,assistant_text:long,assistant_at:'2026-10-21T11:00:00+09:00',expression:'neutral'}));await tick();
assert.equal(document.querySelectorAll('[data-turn-id="1"] .reply-bubbles>.text').length,1,'greeting starts immediately');advance(3000);await tick();
assert.equal(document.querySelectorAll('[data-turn-id="1"] .reply-bubbles>.text').length,3);
const baseAudio=audioCalls;
send('こんにちは');await tick();assert(!document.querySelector('.mine').hidden);assert(!document.querySelector('.read-receipt'));assert(!document.querySelector('.thinking'));
event('delta',{text:'CHECK前の非公開本文'});event('stage',{stage:'checking'});await tick();assert(!document.body.textContent.includes('CHECK前'));
advance(1200);assert.equal(document.querySelector('.read-receipt').textContent,'既読');assert(!document.querySelector('.thinking'));
advance(1300);assert.equal(document.querySelector('.typing-dots').textContent,'…');assert.equal(document.querySelector('.typing-dots').getAttribute('aria-hidden'),'true');
assert(document.querySelector('.thinking img').src.includes('/thinking'));assert(document.querySelector('.thinking [aria-live=polite]').textContent.includes('ソラ'));
await view();assert.equal(document.querySelectorAll('.thinking').length,1,'restoring view preserves timer state');assert.equal(document.querySelectorAll('.mine').length,1);
reply(2);await tick();assert(!document.querySelector('.thinking'));assert(!$('send').disabled);assert.equal(document.querySelectorAll('[data-turn-id="2"] .reply-bubbles>.text').length,1);
assert.equal(audioCalls-baseAudio,1,'whole turn autoplay only once');
advance(3000);await tick();assert.equal(document.querySelectorAll('[data-turn-id="2"] .reply-bubbles>.text').length,3);assert.equal(audioCalls-baseAudio,1);
event('self_forming',{turn_id:2});event('expression',{turn_id:2,expression:'smile'});await tick();
assert.equal(audioCalls-baseAudio,1,'expression must not replay an already spoken reply');
assert.equal(document.querySelectorAll('[data-turn-id="2"] .forming-notice').length,1);assert(document.querySelector('[data-turn-id="2"] img').src.includes('/smile'));
stream.close();await tick();await view();assert.equal(document.querySelectorAll('[data-turn-id="2"] .reply-bubbles>.text').length,3,'history is already fully displayed');
send('早い返答');await tick();reply(3,'すぐに届いたね。');await tick();stream.close();await tick();assert(![...document.querySelectorAll('.mine')].at(-1).querySelector('.read-receipt'));advance(1199);assert(![...document.querySelectorAll('.mine')].at(-1).querySelector('.read-receipt'));advance(1);assert([...document.querySelectorAll('.mine')].at(-1).querySelector('.read-receipt'));advance(5000);assert(!document.querySelector('.thinking'),'fast response never shows late typing');
cfg.display.delay_user=true;await view();send('遅らせる投稿');await tick();assert([...document.querySelectorAll('.mine')].at(-1).hidden);assert(stream,'request starts before the visual delay');
advance(999);assert([...document.querySelectorAll('.mine')].at(-1).hidden);advance(1);assert(![...document.querySelectorAll('.mine')].at(-1).hidden);
reply(4,long,true);await tick();const previous=audioCalls;event('expression',{turn_id:4,expression:'smile'});await tick();assert.equal(audioCalls,previous+1);advance(5000);await tick();assert.equal(audioCalls,previous+1);
stream.close();await tick();cfg.display.split_replies=false;await view();assert.equal(document.querySelectorAll('[data-turn-id="2"] .reply-bubbles>.text').length,1);
cfg.display.split_replies=true;await view();send('早く届いて投稿の遅延を打ち切る');await tick();assert([...document.querySelectorAll('.mine')].at(-1).hidden);reply(5);await tick();assert(![...document.querySelectorAll('.mine')].at(-1).hidden);assert.equal(document.querySelectorAll('[data-turn-id="5"] .reply-bubbles>.text').length,1);await view();advance(5000);assert.equal(document.querySelectorAll('[data-turn-id="5"] .reply-bubbles>.text').length,3,'history restores all parts and cancels detached presentation timers');stream.close();await tick();
// Theme variables, wrapping and OS motion preference are shared by all characters.
const css=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');
assert(css.includes('@media(prefers-reduced-motion:reduce){.typing-dots{animation:none}}'));
assert(css.includes('overflow-wrap:anywhere'));assert(css.includes('min-width:0'));assert(css.includes('--bubble:'));
console.log('PASS UX1 DOM: exact clocks, read/typing, fast reply, Check boundary, split/history/off, greeting, tab restoration, user delay, original-turn notices/expression, whole-reply autoplay and motion/theme/phone CSS');
