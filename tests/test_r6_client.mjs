import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<nav id="menu"></nav><main id="screen"></main>',{url:'http://127.0.0.1/wizard'});
for(const key of ['window','document','location','localStorage','sessionStorage','history'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
const root=document.getElementById('screen'),calls=[];
const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from cbs.config import load_config; from pathlib import Path; print(json.dumps(load_config(data_dir=Path('.tmp/r6/node-config')).model_dump()))"],{encoding:'utf8'}));
Object.assign(base,{keys:{},ai_ready:false,connection_verified:false,advanced_presets:base.advanced.presets});
let available=true,configured=true,privacy='',enabled=false,failStep=false;
// Every request is fake, including all usage-event transport and UI calls.
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):undefined;calls.push({route,body});
  let value;
  if(route==='/api/settings')value=base;
  else if(route==='/api/onboarding/state')value={consent:null};
  else if(route==='/api/onboarding/consent'&&body)value={consent:{version:'consent'}};
  else if(route.startsWith('/api/onboarding/'))value={text:'# はじめに\n\n説明',version:'consent'};
  else if(route==='/api/telemetry'){if(body)enabled=body.enabled;value={enabled,available,configured,text:'**注意事項**\n以下を確認してください。\n- 利用状況を送る <img src=x onerror=alert(1)>\n- 会話は送らない',version:'label-version',privacy_url:privacy};}
  else if(route==='/api/telemetry/sent')value=[{event:'error',timestamp:'2026-11-04T10:00:00+09:00',properties:{type:'<img src=x onerror=alert(1)>',version:'0.1.0'}}];
  else if(route==='/api/telemetry/step'){if(failStep)throw new Error('offline');value={ok:true};}
  else assert.fail(`Unexpected fake request ${route}`);
  return new Response(JSON.stringify(value),{status:200});
};
const tick=async()=>{for(let i=0;i<10;i++)await new Promise(r=>setImmediate(r));};
const {onboardingAI}=await import('../cbs/web/static/onboarding-ai.js');
const {telemetrySettings}=await import('../cbs/web/static/telemetry.js');
const {sequence,ONBOARDING_STEPS}=await import('../cbs/web/static/onboarding.js');
async function welcome(){root.replaceChildren();void onboardingAI(root);await tick();return root.querySelector('input[type=checkbox]');}
let input=await welcome();assert(input);assert.equal(input.checked,false);assert(!root.querySelector('a'));
// The notice is the authors' Markdown: bold, kept line breaks and a list, never HTML from the text.
const notice=root.querySelector('.consent-notice');assert.equal(notice.querySelector('strong').textContent,'注意事項');
assert(notice.querySelector('p br'));assert.equal(notice.querySelectorAll('li').length,2);assert(!notice.querySelector('img'));
assert.equal(input.closest('label').textContent,'上記内容に同意する');
assert(notice.compareDocumentPosition(input)&window.Node.DOCUMENT_POSITION_FOLLOWING,'the agreement sits below the notice');
// Starting needs the agreement.
const start=root.querySelector('.primary');assert.equal(start.textContent,'同意してはじめる');assert(start.disabled);
await start.onclick();await tick();
assert(!calls.some(c=>c.route==='/api/telemetry'&&c.body),'unchecked consent never enables collection');
assert(!calls.some(c=>c.route==='/api/onboarding/consent'&&c.body),'unchecked consent never starts');
input.checked=true;input.dispatchEvent(new window.Event('change'));assert(!start.disabled);
input.checked=false;input.dispatchEvent(new window.Event('change'));assert(start.disabled);
privacy='https://example.com/privacy';calls.length=0;input=await welcome();
assert.equal(input.checked,false);input.checked=true;
const policy=root.querySelector('a');assert.equal(policy.href,privacy);assert.equal(policy.target,'_blank');assert.equal(policy.rel,'noopener noreferrer');
await root.querySelector('.primary').onclick();await tick();
assert.deepEqual(calls.filter(c=>c.route==='/api/telemetry'&&c.body).map(c=>c.body),[{enabled:true,version:'label-version'}]);
root.replaceChildren();await telemetrySettings(root);assert(root.textContent.includes('利用状況の送信'));
input=root.querySelector('input');assert.equal(input.checked,true);input.checked=false;input.dispatchEvent(new window.Event('change'));await tick();
assert(calls.some(c=>c.route==='/api/telemetry'&&c.body?.enabled===false));assert(root.textContent.includes('保存しました'));assert(root.textContent.includes('まだ送っていない分'));
await [...root.querySelectorAll('button')].find(b=>b.textContent==='送った内容を見る').onclick();await tick();
assert(root.textContent.includes('<img src=x onerror=alert(1)>'));assert(!root.querySelector('img,script'));assert(root.textContent.includes('error'));assert(root.textContent.includes('2026-11-04'));
available=false;configured=false;privacy='';root.replaceChildren();await telemetrySettings(root);assert(root.querySelector('input').disabled);assert(root.textContent.includes('この版では送信先が設定されていません'));assert(!root.querySelector('a'));
calls.length=0;root.replaceChildren();
const flow=sequence(root,[{id:'role-vision',render:p=>p.append('screen')},{id:'life',render:p=>p.append('next')}],{telemetryFirst:false});
await flow.start();await flow.go('role-vision');await flow.go('life');await flow.go('role-vision');await tick();
assert.deepEqual(calls.filter(c=>c.route==='/api/telemetry/step').map(c=>c.body),[{step:'role-vision',first:false},{step:'life',first:false}]);
failStep=true;await flow.go('life');await tick();assert(root.textContent.includes('next'),'a failed step request never blocks navigation');
assert(ONBOARDING_STEPS.includes('look-cover-import')&&ONBOARDING_STEPS.includes('voice_input'));
// Explanations: ---, ___ and *** are horizontal rules; a list item starting with "- " is not.
const {renderExplanation}=await import('../cbs/web/static/onboarding.js');
const page=document.createElement('div');renderExplanation(page,'上\n\n---\n中\n___\n- 項目\n***\n下');
assert.equal(page.querySelectorAll('hr').length,3);assert.equal(page.querySelectorAll('li').length,1);assert.equal(page.querySelectorAll('p').length,3);
// One blank line separates paragraphs; two or more add a wider gap, never at the start.
const gaps=document.createElement('div');renderExplanation(gaps,'\n\n\n一\n\n二\n\n\n三\n\n\n\n- 四');
assert.deepEqual([...gaps.children].map(e=>e.className||e.tagName),['P','P','explanation-gap','P','explanation-gap','UL']);
const headed=document.createElement('div');renderExplanation(headed,'# 見出し\n\n\n本文');
assert.deepEqual([...headed.children].map(e=>e.tagName),['H2','P'],'no extra gap right under the heading');
console.log('PASS R6 DOM: Markdown notice, required agreement below it, label version, policy links, autosave, disabled switch, safe sent text, once-per-flow steps');
