import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<nav id="menu"></nav><main id="screen"></main><p id="error" hidden></p>',{url:'http://unit.invalid/wizard'});
for(const key of ['window','document','location','localStorage','sessionStorage','history','navigator'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
window.scrollTo=()=>{};
const root=document.getElementById('screen');
const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from cbs.config import load_config; from pathlib import Path; print(json.dumps(load_config(data_dir=Path('.tmp/r10/node-config')).model_dump()))"],{encoding:'utf8'}));
base.advanced_presets=base.advanced.presets;base.advanced_models=base.advanced.presets[0].models;base.keys={};base.ai_ready=false;base.connection_verified=false;
const guide='<!-- awalia-setup-helper 1 -->\n<script>alert(1)</script> & <img src=x>\n手引き';
const description='ふだん使っている AI（ChatGPT や Claude など）に貼ると、画面に合わせて設定を案内してもらえます。API キーは AI に教えないでください。';
let cfg,calls,fail=true,helperFailure=false;
function reset(){cfg=structuredClone(base);calls=[];helperFailure=false;sessionStorage.clear();root.replaceChildren();}
globalThis.fetch=async(url,options={})=>{
  const path=new URL(url,location.href).pathname,body=options.body?JSON.parse(options.body):null;calls.push(path);let data;
  if(path==='/api/settings'){if(body){if(body.models)cfg.models=body.models;if(body.llm)Object.assign(cfg.llm,body.llm);}data=cfg;}
  else if(path==='/api/onboarding/state')data={consent:{at:'test'}};
  else if(path==='/api/onboarding/setup-helper'){if(helperFailure)return new Response('{}',{status:500});data={text:guide,source:'bundled'};}
  else if(path.startsWith('/api/onboarding/'))data={text:'# AI について\n\n説明です。'};
  else if(path==='/api/settings/connection'){if(fail)return new Response(JSON.stringify({detail:'接続できません'}),{status:422});data={status:'connected'};}
  else if(path==='/api/telemetry/step')data={};
  else assert.fail(`Unexpected request: ${path}`);
  return new Response(JSON.stringify(data));
};
const {onboardingAI}=await import('../cbs/web/static/onboarding-ai.js');
const tick=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
async function click(label){const button=[...root.querySelectorAll('button')].find(e=>e.textContent===label);assert(button,`Missing ${label}`);await button.onclick();await tick();}
async function next(){await root.querySelector('.wizard-actions .primary').onclick();await tick();}
function choose(value){const input=root.querySelector(`input[value="${value}"]`);assert(input);input.checked=true;input.dispatchEvent(new window.Event('change'));}
const helper=()=>[...root.querySelectorAll('button')].find(e=>e.textContent==='AI に手伝ってもらう');
function fallback(){const input=root.querySelector('textarea');assert(input?.readOnly);assert.equal(input.value,guide);assert(!root.querySelector('script,img[src=x]'));assert(root.textContent.includes('全部選んでコピーしてください。'));}

reset();void onboardingAI(root);await tick();assert(!calls.includes('/api/onboarding/setup-helper'));await next();
assert(helper());assert(root.textContent.includes(description));assert(root.textContent.indexOf('説明です。')<root.textContent.indexOf(description));assert(!calls.includes('/api/onboarding/setup-helper'));
let copied;
Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{copied=text;}},configurable:true});
await click('AI に手伝ってもらう');assert.equal(copied,guide);assert([...root.querySelectorAll('[role=status]')].some(e=>e.textContent==='コピーしました。使っている AI の入力欄に貼り付けて、送ってください。'));assert(!root.querySelector('textarea'));
Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true});await click('AI に手伝ってもらう');fallback();
Object.defineProperty(navigator,'clipboard',{value:{writeText:async()=>{throw Error('denied');}},configurable:true});await click('AI に手伝ってもらう');fallback();assert.equal(root.querySelectorAll('textarea').length,1);
helperFailure=true;await click('AI に手伝ってもらう');assert(root.textContent.includes('手引きを用意できませんでした。'));assert(!root.querySelector('textarea'));helperFailure=false;

// A failed connection offers the same help, while a successful connection does not.
async function check(failed){reset();fail=failed;void onboardingAI(root,{repair:true});await tick();choose('key');await next();choose('openai');await next();assert(!helper());await next();}
await check(true);assert.equal(root.querySelector('h2').textContent,'つながりませんでした');assert(helper());assert(root.textContent.includes(description));assert(!calls.includes('/api/onboarding/setup-helper'));await click('AI に手伝ってもらう');fallback();
await check(false);assert(root.textContent.includes('接続の確認が完了しました。'));assert(!helper());
reset();fail=true;void onboardingAI(root,{settingsEntry:true});await tick();assert(!helper());await next();assert(root.textContent.includes('接続できません'));assert(!helper());assert(!calls.includes('/api/onboarding/setup-helper'));
console.log('PASS R10 DOM: AI and failed check only, lazy fetch, clipboard success, missing/denied clipboard with literal readonly text, API failure, settings entry exclusion');
