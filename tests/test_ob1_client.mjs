import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<nav id="menu"></nav><main id="screen"></main><p id="error" hidden></p>',{url:'http://127.0.0.1/wizard'});
for(const key of ['window','document','location','localStorage','sessionStorage','history'])globalThis[key]=dom.window[key];
let scrolls=0;window.scrollTo=()=>scrolls++;
const root=document.getElementById('screen');
const {renderExplanation,sequence,accordion}=await import('../cbs/web/static/onboarding.js');
renderExplanation(root,'# {name}\n\n<img src=x onerror=alert(1)>\n\n- **大切**なこと\n- {name}','ユキ');
assert.equal(root.querySelector('h2').textContent,'ユキ');assert.equal(root.querySelectorAll('img,script').length,0);assert(root.textContent.includes('<img'));assert.equal(root.querySelector('strong').textContent,'大切');assert.equal(root.querySelectorAll('li').length,2);
const tick=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
async function click(label){const b=[...root.querySelectorAll('button')].find(b=>b.textContent===label);assert(b,`Missing ${label}: ${root.textContent}`);await b.onclick();await tick();}
let skipped=false;
const flow=sequence(root,[{id:'now',explanation:true,render:p=>p.append('今'),skip:()=>skipped=true},{id:'question',render:p=>p.append('問い')},{id:'scenes',explanation:true,render:p=>p.append('場面')}]);
await flow.start();await click('飛ばす');assert(skipped);assert(root.textContent.includes('場面'));assert(!root.textContent.includes('問い'));
root.replaceChildren();let a='',b='';accordion(root,[{title:'名前',summary:()=>a,render:p=>{const input=document.createElement('input');input.value=a;input.oninput=()=>a=input.value;p.append(input);}},{title:'地域',disabled:true,reason:'現代日本では聞きません',render:()=>assert.fail()},{title:'呼ばれ方',summary:()=>b,render:p=>{const input=document.createElement('input');input.value=b;p.append(input);}}]);
root.querySelector('input').value='ユキ';root.querySelector('input').oninput();await click('次の項目へ');assert(root.querySelector('.accordion-summary').textContent==='ユキ');assert(root.textContent.includes('現代日本では聞きません'));assert.equal(root.querySelectorAll('[aria-expanded=true]').length,1);assert(root.querySelector('button:disabled'));

const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json; from cbs.config import load_config; from pathlib import Path; print(json.dumps(load_config(data_dir=Path('.tmp/ob/node-config')).model_dump()))"],{encoding:'utf8'}));
base.advanced_presets=base.advanced.presets;base.advanced_models=base.advanced.presets[0].models;base.keys={};base.ai_ready=false;base.connection_verified=false;
let cfg,calls,fail,consented,downloadReady=true;
function reset(){cfg=structuredClone(base);calls=[];fail=false;consented=false;sessionStorage.clear();history.replaceState(null,'','/wizard');root.replaceChildren();}
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):undefined;calls.push({route,body});let data;
  if(route==='/api/settings'){if(body){if(body.models)cfg.models=body.models;if(body.llm)Object.assign(cfg.llm,body.llm);if(body.llm?.mode==='simple')cfg.models=structuredClone(base.advanced_presets[0].models);}data=cfg;}
  else if(route==='/api/settings/keys'){cfg.keys[body.provider]='set';data={};}
  else if(route==='/api/onboarding/state')data={consent:consented?{at:'test'}:null};
  else if(route==='/api/onboarding/consent'&&body){consented=true;data={consent:{at:'test',version:body.version}};}
  else if(route.startsWith('/api/onboarding/'))data={text:`# ${route.split('/').at(-1)}`,version:'test-version'};
  else if(route==='/api/settings/connection'){if(fail)return new Response(JSON.stringify({detail:'キーを確かめてください。'}),{status:422});data={status:'connected'};}
  else if(route==='/api/setup/voice-step')data={status:'done'};
  else if(route==='/api/embedding/download/plan')data={token:'download-plan',total:140000000,download_bytes:140000000};
  else if(route==='/api/embedding/download')data={status:downloadReady?'ready':body?'downloading':calls.some(c=>c.route==='/api/embedding/download'&&c.body)?'downloading':'missing',model:'ruri-v3-130m',percent:50};
  else assert.fail(`Unexpected fake request ${route}`);
  return new Response(JSON.stringify(data),{status:200});
};
const {onboardingAI,requiredProviders,modelLabel}=await import('../cbs/web/static/onboarding-ai.js');
assert.equal(modelLabel('openrouter/google/example',{...base,model_candidates:{actor:[{model:'openrouter/google/example',label:'会話の標準'}]}}),'会話の標準');assert.equal(modelLabel('openrouter/google/example',{...base,model_candidates:{actor:[{model:'openrouter/google/example',label:'会話の標準'}]}},'research'),'OpenRouter の example');assert.equal(modelLabel('openrouter/google/other',base),'OpenRouter の other');
function input(label,value){const box=[...root.querySelectorAll('fieldset')].find(e=>e.querySelector('legend')?.textContent===label);if(box){const el=[...box.querySelectorAll('input[type=radio]')].find(e=>e.value===value);assert(el,`Missing choice ${value}`);el.checked=true;el.dispatchEvent(new dom.window.Event('change'));return;}const l=[...root.querySelectorAll('label')].find(e=>e.firstChild.textContent===label);assert(l,`Missing field ${label}`);const el=l.querySelector('select,input');el.value=value;el.dispatchEvent(new dom.window.Event('input'));}
const primary=()=>{assert.equal(root.querySelectorAll('.primary').length,1);return root.querySelector('.primary');};
async function next(){await primary().onclick();await tick();}
async function startKey(combination='mixed'){
  reset();void onboardingAI(root);await tick();assert.equal(primary().textContent,'同意してはじめる');assert(root.querySelector('.welcome-consent h2'));await next();assert(consented);assert(root.querySelector('.chapter-caption').textContent.startsWith('AI1 / '),'the AI chapter counts from its explanation');await next();input('設定のしかた','key');await next();input('AI の組み合わせ',combination);await next();
}
await startKey();assert(root.textContent.includes('使うキー'));await next();assert.equal(root.querySelectorAll('input[type=password]').length,1);assert.equal(root.querySelector('a[href="https://openrouter.ai/keys"]').target,'_blank');input('OpenRouter の API キー','fake-test');await click('戻る');assert.equal(root.querySelector('input:checked').value,'router');await next();assert.equal(root.querySelector('input').value,'fake-test');await next();assert(root.textContent.includes('会話を保存しない提供元'));await next();assert(root.textContent.includes('接続の確認が完了'));await next();assert(root.textContent.includes('使うモデルの確認'));assert(calls.some(c=>c.route==='/api/settings/connection'));await next();assert(root.textContent.includes('partner'));assert(calls.some(c=>c.route==='/api/setup/voice-step'&&c.body.skipped));
await startKey();input('使うキー','direct');await next();assert.equal(root.querySelectorAll('input[type=password]').length,2);assert(root.textContent.includes('Gemini')&&root.textContent.includes('OpenAI'));assert(!root.textContent.includes('OpenRouter の API'));
await startKey('gemini');assert.equal(root.querySelectorAll('input[type=radio]').length,3);assert.equal(root.querySelectorAll('select').length,0);assert(root.querySelector('.choice-description'));input('会話のモデル','gemini/gemini-4-argon');await next();assert.equal(root.querySelectorAll('input[type=password]').length,1);await click('戻る');assert.equal(root.querySelector('input:checked').value,'gemini/gemini-4-argon');await next();fail=true;await next();assert(root.textContent.includes('キーを確かめてください'));assert(primary().disabled);await click('戻る');assert(root.querySelector('input[type=password]'));
await startKey('openai');assert(root.querySelector('input[type=password]'),'single actor skips model choice');
await startKey('claude');assert.equal(root.querySelectorAll('input[type=radio]').length,2);await next();assert.equal(root.querySelectorAll('input[type=password]').length,1);assert(root.textContent.includes('Claude の API キー'),'ready downloads are skipped');
await click('戻る');assert.equal(root.querySelectorAll('input[type=radio]').length,2,'back skips the already-ready download screen');await next();assert(root.querySelector('input[type=password]'));
downloadReady=false;await startKey('claude');await next();assert.equal(primary().textContent,'次へ');assert(primary().disabled);await click('取得する');assert(root.querySelector('progress'));assert.equal(root.querySelector('progress').value,50);assert.equal(root.querySelectorAll('dialog').length,0);downloadReady=true;await new Promise(r=>setTimeout(r,800));await tick();assert(root.textContent.includes('の取得が完了しました。次へ進めます。'));assert(!primary().disabled);assert(!root.querySelector('input[type=password]'));await next();assert(root.querySelector('input[type=password]'));assert(calls.some(c=>c.route==='/api/embedding/download/plan'));
await startKey('custom');for(const role of ['会話','判断','記憶の検索','調べもの','画像の読み取り']){assert(root.textContent.includes(role+'のサービス'));assert(!root.textContent.includes('声で入力のモデル'));const model=root.querySelector('.model-role-stack input:not([type=radio])');if(model&&!model.disabled){assert.equal(model.value,'');assert(model.placeholder);const service=root.querySelector('.model-role-stack input[type=radio][value=openai]');if(role==='会話'){model.value='temporary';model.dispatchEvent(new window.Event('input'));service.checked=true;service.dispatchEvent(new window.Event('change'));assert.equal(model.value,'');assert(model.placeholder);}model.value=model.placeholder;model.dispatchEvent(new window.Event('input'));}await next();}assert(root.querySelector('input[type=password]'));assert(requiredProviders({actor:'gemini/x',judge:'openai/y',steward:'openai/y',stt:'anthropic/z',embedding:'local/ruri'}).length===2);
reset();cfg.ai_ready=true;cfg.connection_verified=true;let done=false;void onboardingAI(root,{partnerOnly:true}).then(()=>done=true);await tick();assert(root.textContent.includes('partner'));assert.equal(root.querySelector('.chapter-caption').textContent,'パートナー1 / 6','the handoff page counts as the partner chapter does');assert(!root.textContent.includes('声で入力'));assert(!root.textContent.includes('バックアップ'));assert(!root.querySelector('[aria-current=step]').textContent.includes('AI'));await next();assert(done);assert(!calls.some(c=>c.route==='/api/settings/connection'));
assert(scrolls>10,'navigation scrolls to the top');

// Exercise the actual /wizard entry and handoff, with every request still faked.
reset();cfg.ai_ready=true;cfg.connection_verified=true;consented=true;
const entryDOM=new JSDOM(readFileSync(new URL('../cbs/web/static/wizard.html',import.meta.url),'utf8'),{url:'http://127.0.0.1/wizard'});
for(const key of ['window','document','location','localStorage','sessionStorage','history'])globalThis[key]=entryDOM.window[key];
window.scrollTo=()=>{};
const fakeFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
  if(path==='/api/partner')return new Response(JSON.stringify({partner:null}));
  if(path==='/api/bootstrap/questionnaire')return new Response(JSON.stringify({questionnaire:{sections:[],common_choices:{}},domains:[]}));
  return fakeFetch(path,options);
};
await import('../cbs/web/static/wizard.js');await tick();
const entry=document.getElementById('screen');
assert.equal(entry.querySelector('.primary').textContent,'はじめる');
assert([...entry.querySelectorAll('button')].some(b=>b.textContent==='バックアップから始める'));
for(const label of ['はじめる','次へ','次へ','次へ']){assert.equal(entry.querySelector('.primary').textContent,label);await entry.querySelector('.primary').onclick();await tick();}
assert(entry.textContent.includes('持っているものはありますか？'),'handoff opens the OB2 choices');
assert(entry.textContent.includes('過去のチャットログがある'),'import remains reachable after the new first-run flow');
assert(!document.body.textContent.includes('声（任意）'));
assert(!calls.some(c=>c.route==='/api/onboarding/consent'&&c.body),'accepted consent is not shown again');
console.log('PASS OB1 DOM: safe copy, skip, accordion, consent, all presets/key counts, retained input, download, failure/back, automatic check, second partner');
