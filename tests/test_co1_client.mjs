// Offline only: numeric fixtures, no provider/network access or downloads.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<form><div id="ai" role="tabpanel"></div></form>',{url:'https://unit.invalid/'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
const purposes=[['chat','会話'],['memory','記憶の整理'],['autonomy','自主活動'],['media','写真と声'],['import','取り込み'],['welcome','迎える手順'],['other','その他']];
const period=(usd,unknown)=>({usd,unknown,calls:3,purposes:purposes.map(([id,label],i)=>({id,label,usd:i===0?usd:0,unknown:i===1?unknown:0,calls:1}))});
const cfg={costs:{monthly_guide_usd:null,pause_autonomy:false},cost_summary:{today:period(.01,1),month:{...period(.3,2),turns:3,per_turn_usd:.1},previous:period(.2,0),paused:false}};
let seen=false,requests=0;
globalThis.fetch=async(path,options)=>{
  assert.equal(path,'/api/costs/notice');assert.equal(options.method,'POST');requests++;
  const notice=seen?null:{month:'2026-10',paused:true};seen=true;
  return new Response(JSON.stringify({notice}));
};
const {costSettings,showCostNotice}=await import('../cbs/web/static/costs.js');
const parent=document.getElementById('ai'),form=document.querySelector('form'),draft={};
parent.hidden=true;
await costSettings(parent,cfg,draft,form);
assert(parent.textContent.includes('使った費用の目安'));
assert(parent.textContent.includes('正確な請求は提供元の画面'));
assert(parent.textContent.includes('$0.3000（不明 2 件）'));
assert(parent.textContent.includes('1ターンあたり $0.1000'));
assert.equal(parent.querySelectorAll('tbody tr').length,7);
assert.equal(parent.querySelectorAll('thead th').length,4);
assert.equal(form.querySelector('.cost-notice').parentNode,form,'The notice stays visible when the AI tab is hidden');
assert(form.querySelector('.cost-notice').textContent.includes('会話と記憶の整理は続けられます'));
assert.equal(form.querySelector('.cost-notice').getAttribute('role'),'status');
const input=parent.querySelector('input[type=number]'),pause=parent.querySelector('input[type=checkbox]');
assert.equal(input.value,'');assert.equal(pause.checked,false);
input.value='2.5';input.dispatchEvent(new window.Event('input'));assert.equal(draft.costs.monthly_guide_usd,2.5);
input.value='';input.dispatchEvent(new window.Event('input'));assert.equal(draft.costs.monthly_guide_usd,null);
pause.checked=true;pause.dispatchEvent(new window.Event('change'));assert.equal(draft.costs.pause_autonomy,true);
await costSettings(parent,cfg,{},form);assert.equal(form.querySelectorAll('.cost-notice').length,1);
showCostNotice(null,{paused:false});
for(const theme of ['light','dark']){
  document.documentElement.dataset.theme=theme;
  window.innerWidth=320;
  assert.equal(parent.querySelector('table').style.width,''); // Theme/responsive rules stay in CSS.
  assert(!parent.querySelector('[style*="color"]'));
}
const css=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');
assert(css.includes('.cost-settings table{width:100%;table-layout:fixed;'));
assert(css.includes('@media(max-width:400px){.cost-totals{grid-template-columns:1fr}'));
assert.equal(requests,2);
console.log('PASS CO1 offline DOM: periods, seven purposes, unknown, per turn, optional guide/clear, autonomy switch, one visible notice, theme tokens and phone CSS');
