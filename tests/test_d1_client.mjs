import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<main id="screen"></main><nav id="menu"></nav>',{url:'http://unit.invalid/wizard'});
for(const k of ['window','document','location','localStorage','sessionStorage','history'])globalThis[k]=dom.window[k];
window.scrollTo=()=>{};
const root=document.getElementById('screen');
const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from pathlib import Path; from cbs.config import load_config; print(json.dumps(load_config(data_dir=Path('.tmp/d1_node_config')).model_dump()))"],{encoding:'utf8'}));
let cfg,calls;
globalThis.fetch=async(url,options={})=>{
  const path=url.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({path,body});let data;
  if(path==='/api/settings'){if(body){if(body.models)cfg.models=body.models;if(body.llm)Object.assign(cfg.llm,body.llm);}data=cfg;}
  else if(path==='/api/onboarding/state')data={consent:true};
  else if(path.startsWith('/api/onboarding/'))data={text:'# Example',version:'test'};
  else if(path.endsWith('/download'))data={status:'ready'};
  else data={status:'connected'};
  return new Response(JSON.stringify(data));
};
const {onboardingAI,decisionChoices,reconcileDecision,requiredProviders,modelLabel}=await import('../cbs/web/static/onboarding-ai.js');
const tick=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
const next=async()=>{await root.querySelector('.wizard-actions .primary').onclick();await tick();};
const back=async()=>{await [...root.querySelectorAll('button')].find(b=>b.textContent==='戻る').onclick();await tick();};
function choose(title,value){const group=[...root.querySelectorAll('fieldset')].find(f=>f.querySelector('legend')?.textContent===title);assert(group, title);const e=[...group.querySelectorAll('input[type=radio]')].find(i=>i.value===value);assert(e,`${title}: ${value}`);e.checked=true;e.dispatchEvent(new window.Event('change'));}
function model(role,provider){choose(role+'のサービス',provider);const e=root.querySelector('.model-role-stack input:not([type=radio])');e.value='test-model';e.dispatchEvent(new window.Event('input'));}
async function start(actor,judge){
  root.replaceChildren();sessionStorage.clear();calls=[];cfg=structuredClone(base);
  cfg.advanced_presets=cfg.advanced.presets;cfg.advanced_models=structuredClone(cfg.models);cfg.keys={};cfg.ai_ready=false;cfg.connection_verified=false;
  void onboardingAI(root);await tick();await next();await next();choose('設定のしかた','key');await next();choose('AI の組み合わせ','custom');await next();
  model('会話',actor);await next();model('返事の確認',judge);await next();
}
const g='gemini/gemini-decision-1',o='openrouter/google/gemini-decision-1';
for(const [actor,judge,values,selected]of [
  ['gemini','openai',[g,''],g],['openai','openrouter',[o,''],o],
  ['gemini','openrouter',[g,o,''],o],['openrouter','gemini',[g,o,''],g],
  ['openai','anthropic',[],null]
]){
  await start(actor,judge);
  if(values.length){
    const group=[...root.querySelectorAll('fieldset')].find(f=>f.querySelector('legend')?.textContent==='場面の読み取り');assert(group);
    assert.deepEqual([...group.querySelectorAll('input')].map(e=>e.value),values);
    assert.equal(group.querySelector('input:checked').value,selected);
    assert(root.textContent.includes('選ぶことに特化したモデルです。'));
    await next();
  }else assert(!root.textContent.includes('場面の読み取り'));
  assert(root.textContent.includes('記憶の検索'));
}
// Removing the chosen service resets the selection; a surviving choice (including off) is retained.
await start('gemini','openrouter');choose('場面の読み取り',o);await back();model('返事の確認','gemini');await next();assert.equal(root.querySelector('input:checked').value,g);
await back();model('返事の確認','openrouter');await next();assert.equal(root.querySelector('input:checked').value,g);
choose('場面の読み取り','');await back();model('返事の確認','gemini');await next();assert.equal(root.querySelector('input:checked').value,'');
await back();model('返事の確認','anthropic');await back();model('会話','openai');await next();model('返事の確認','anthropic');await next();assert(root.textContent.includes('記憶の検索'));assert(!root.textContent.includes('場面の読み取り'));
const skippedCount=Number(root.querySelector('.chapter-caption').textContent.match(/AI\d+ \/ (\d+)/)[1]);
await start('gemini','gemini');await next();const includedCount=Number(root.querySelector('.chapter-caption').textContent.match(/AI\d+ \/ (\d+)/)[1]);assert.equal(includedCount,skippedCount+1);
assert.deepEqual(decisionChoices({actor:'openai/a',judge:'anthropic/b'}),[]);
assert.equal(reconcileDecision({actor:'openai/a',judge:'anthropic/b',decision:o}),'');
assert.deepEqual(requiredProviders({actor:'openai/a',judge:'gemini/b',decision:o}),['openai','gemini','openrouter']);
assert.equal(modelLabel('',base,'decision'),'返事の確認と同じ AI');
// The settings entry lists the current Decision choice even when disabled.
root.replaceChildren();cfg.ai_ready=cfg.connection_verified=true;cfg.models.decision='';void onboardingAI(root,{settingsEntry:true});await tick();
const row=[...root.querySelectorAll('.summary-row')].find(r=>r.firstChild.textContent==='場面の読み取り');assert(row?.textContent.includes('返事の確認と同じ AI'));
console.log('PASS D1 DOM: five provider combinations, order, defaults, skip/count, back/reset/retain/off, required keys and settings summary');
