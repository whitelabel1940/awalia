import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><main id="note"></main></body>',{url:'https://unit.invalid/#note'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
globalThis.fetch=()=>{throw new Error('EX2 DOM must not use the network');};
const {partnerNote}=await import('../cbs/web/static/partner-note.js');
const parent=document.getElementById('note');
const scene={id:7,text:'好意を告げられると、理由を聞くと言っていた。',dates:['2026-10-19','2026-10-20'],
  evidence:[{date:'2026-10-19',summary:'もしもの質問に答えた。<img src=x onerror=alert(1)>',mode:'hypothetical'},
            {date:'2026-10-20',summary:'もう一度、同じことを尋ねた。',mode:'hypothetical'}]};
for(const theme of ['light','dark']){
  document.documentElement.dataset.theme=theme;
  parent.replaceChildren();
  partnerNote(parent,{name:'ソラ',self_forming:[{label:'好意を伝えられた時',summary:'好意には照れながら向き合うと言っていた。',scenes:[scene]}],
    self_traits:[{text:'相手の気持ちを丁寧に聞こうとする人みたい。',scenes:[{id:7,label:'好意を伝えられた時',text:scene.text}]}]});
  assert(parent.textContent.includes('あなたが知ったソラのこと'));
  assert(parent.textContent.includes('見えてきたソラらしさ'));
  assert(!parent.textContent.includes('テスト係'));
  assert.equal(parent.querySelectorAll('.scene-summary').length,1);
  assert.equal(parent.querySelector('.note-scene p').textContent,scene.text);
  assert(parent.textContent.includes('2026-10-19、2026-10-20 の会話で'));
  const scenes=parent.querySelector('.scene-details');assert(!scenes.open);assert.equal(scenes.querySelector('summary').textContent,'場面（1）');
  const detail=parent.querySelector('.note-scene details');
  assert.equal(detail.querySelector('summary').textContent,'なぜそう思った？');
  detail.open=true;assert(detail.open&&detail.textContent.includes('（もしもの話）'));
  assert.equal(parent.querySelectorAll('img,script').length,0);
  const link=parent.querySelector('.trait-sources a');
  assert.equal(link.getAttribute('href'),'#note/scene-7');
  let scrolled=false;parent.querySelector('#note-scene-7').scrollIntoView=()=>{scrolled=true;};
  link.click();assert(scenes.open);assert(scrolled);assert.equal(document.activeElement.id,'note-scene-7');
}
parent.replaceChildren();
partnerNote(parent,{name:'ソラ',self_forming:[],self_traits:[]});assert.equal(parent.childElementCount,0);
partnerNote(parent,{name:'ソラ',self_forming:[{label:'好意を伝えられた時',summary:null,scenes:[scene]}],self_traits:[]});
assert.equal(parent.querySelectorAll('.scene-summary,.note-traits').length,0);
assert.equal(parent.querySelectorAll('details').length,1);
const css=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');
assert(css.includes('.partner-note,.scene-list,.note-scene,.note-traits{min-width:0;overflow-wrap:anywhere}'));
assert(css.includes('@media(max-width:480px){.note-scene'));
console.log('PASS EX2 offline DOM: three levels, absent summary/traits, empty, evidence, dates, escaped text, character-neutral light/dark and phone rules, focusable scene links');
