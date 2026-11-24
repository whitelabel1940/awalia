import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><main id="screen"></main></body>',{url:'https://unit.invalid/#',runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
globalThis.requestAnimationFrame=f=>f();
let notice=true;const categories={temporary_health:{label:'一時的な体調の出来事',allow_mention:false}};
const result=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(path,options={})=>{
  const body=options.body?JSON.parse(options.body):undefined;
  if(path.startsWith('/api/note/private-settings')){if(body)notice=false;return result({categories,mention_notice:notice});}
  throw new Error('Unexpected offline request: '+path);
};
const tick=()=>new Promise(r=>setTimeout(r,10));
const {checkMentionNotice,privateSettings}=await import('../cbs/web/static/knowing.js');
const button=(text,scope=document)=>[...scope.querySelectorAll('button')].find(b=>b.textContent===text);
// Shown above the composer while the one-time notice is pending, once only.
await checkMentionNotice('テスト係');await checkMentionNotice('テスト係');
const cards=document.querySelectorAll('[data-mention-float]');assert.equal(cards.length,1);
assert(cards[0].classList.contains('backup-notice'));assert(cards[0].textContent.includes('テスト係が日常の話題にしてよい'));
// 「選び直す」 opens the user's page of the note.
await button('選び直す').onclick();assert.equal(location.hash,'#note/user');assert(!document.querySelector('[data-mention-float]'));
// Saving a choice in the note also removes a floating card.
await checkMentionNotice('テスト係');assert(document.querySelector('[data-mention-float]'));
const page=document.getElementById('screen');await privateSettings(page,null,'テスト係');
const input=page.querySelector('input[type=checkbox]');input.checked=true;await input.onchange();await tick();
assert(!document.querySelector('[data-mention-float]'));
// Saved: no card again. 「あとで」 hides it for this page load.
await checkMentionNotice('テスト係');assert(!document.querySelector('[data-mention-float]'));
notice=true;await checkMentionNotice('テスト係');await button('あとで').onclick();
await checkMentionNotice('テスト係');assert(!document.querySelector('[data-mention-float]'));
console.log('PASS: D3 floating notice');
