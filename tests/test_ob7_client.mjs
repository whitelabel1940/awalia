import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/wizard.html',import.meta.url),'utf8'),{url:'http://unit.invalid/wizard'});
for(const key of ['window','document','location','localStorage','sessionStorage','history'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
const {sequence,choiceCards}=await import('../cbs/web/static/onboarding.js');
const root=document.getElementById('screen'),progress=document.getElementById('progress');
const flow=sequence(root,[{id:'welcome',render:p=>p.append('表紙')},{id:'explain',explanation:true,render:p=>p.append('説明')},{id:'check',centered:true,render:p=>p.append('確認')},{id:'input',render:p=>choiceCards(p,'選択',[['a','このまま']], 'a',()=>{})},{id:'done',explanation:true,render:p=>p.append('完了')}]);
for(const id of ['welcome','explain','check','input','done','welcome']){
 await flow.start(id);assert.equal(root.classList.contains('onboarding-centered'),id==='welcome');
 const body=root.querySelector(':scope>section');assert.equal(body.classList.contains('card'),!['welcome','done'].includes(id));
 assert.equal(body.lastElementChild,progress);assert.equal(document.querySelectorAll('#progress').length,1);
 progress.textContent='人物像の生成を実行中です。';progress.classList.add('working');
}
const css=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');
assert.match(css,/\.progress\.working::before\{[^}]*animation:bootstrap-working/);
const radio=css.match(/\.choice input\[type=radio\]\{([^}]+)\}/)[1];
for(const rule of ['box-sizing:border-box','flex:0 0 18px','width:18px','min-width:18px','max-width:18px','height:18px','min-height:18px','max-height:18px','padding:0'])assert(radio.includes(rule),rule);
assert.match(css,/\.onboarding-unframed\{[^}]*background:transparent;border:0;box-shadow:none/);
assert(!readFileSync(new URL('../cbs/web/bootstrap_api.py',import.meta.url),'utf8').includes('時間がかかる段'));
// The likes editor's generic input padding must not distort the shared radio.
const style=document.createElement('style');style.textContent='.srow input{padding:5px 8px}'+css.match(/\.choice input\[type=radio\]\{[^}]+\}/)[0];document.head.append(style);
for(const nested of [false,true]){
 const host=document.createElement('div');if(nested)host.className='srow';root.append(host);choiceCards(host,'この好みの設定',[['a','膨らませてよい'],['b','このまま']], 'a',()=>{});
 for(const input of host.querySelectorAll('input')){const computed=window.getComputedStyle(input);assert.equal(computed.width,'18px');assert.equal(computed.height,'18px');assert.equal(computed.padding,'0px');assert.equal(computed.flexShrink,'0');}
 host.remove();
}
const wizard=readFileSync(new URL('../cbs/web/static/wizard.js',import.meta.url),'utf8'),runningJobs=new Map([[1,{background:false,progress:'質問の生成を実行中です。'}]]);
const updateJob=new Function('runningJobs','$',`let pending;${wizard.match(/function jobProgress\(\)\{[^\n]+/)[0]};jobProgress();`);
await flow.start('input');document.body.classList.add('onboarding');document.getElementById('content').append(progress);
updateJob(runningJobs,id=>document.getElementById(id));assert.equal(root.querySelector('section.card').lastElementChild,progress);assert.equal(progress.textContent,'質問の生成を実行中です。');assert(progress.classList.contains('working'));assert(root.inert);
runningJobs.clear();updateJob(runningJobs,id=>document.getElementById(id));assert.equal(progress.textContent,'');assert(!progress.classList.contains('working'));assert(!root.inert);
// Hold the fake request open so the pending state is inspected before completion.
const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json;from pathlib import Path;from cbs.config import load_config;print(json.dumps(load_config(data_dir=Path('.tmp/ob7/config')).model_dump()))"],{encoding:'utf8'}));
const cfg={...base,keys:{openrouter:'set'},advanced_models:base.models,advanced_presets:base.advanced.presets,ai_ready:false,connection_verified:false};
let resolveCheck,fail=false,checks=0;
globalThis.fetch=async(path,options={})=>{
 let result;const body=options.body?JSON.parse(options.body):null;
 if(path==='/api/settings'){if(body?.llm)Object.assign(cfg.llm,body.llm);result=cfg;}
 else if(path==='/api/onboarding/state')result={consent:true};
 else if(path.startsWith('/api/onboarding/'))result={text:'# 説明'};
 else if(path==='/api/settings/connection'){checks++;await new Promise(r=>resolveCheck=r);if(fail)return new Response(JSON.stringify({detail:'fake failure'}),{status:400});result={ok:true};}
 else throw Error('Unexpected fake endpoint '+path);
 return new Response(JSON.stringify(result));
};
const tick=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
const next=()=>root.querySelector('.wizard-actions .primary').onclick();
const {onboardingAI}=await import('../cbs/web/static/onboarding-ai.js');
for(const failure of [false,true]){
 fail=failure;void onboardingAI(root,{repair:true});await tick();await next();await next();
 const checking=next();await tick();
 const waiting=root.querySelector('section.card .progress.working:not(#progress)');assert(waiting);assert.equal(waiting.getAttribute('role'),'status');
 assert(waiting.textContent.includes('会話・判断・記憶の整理・記憶の検索の4つの役'));assert(waiting.textContent.includes('数秒〜30秒'));
 assert(root.querySelector('.primary').disabled);assert(!root.classList.contains('onboarding-centered'));
 resolveCheck();await checking;await tick();assert(!waiting.isConnected);
 if(failure){assert(root.querySelector('[role=alert]'));assert(root.querySelector('.primary').disabled);}else{assert(root.querySelector('.success'));assert(!root.querySelector('.primary').disabled);}
}
assert.equal(checks,2);
const aiFetch=globalThis.fetch;globalThis.fetch=async(path,options)=>path==='/api/look'?new Response(JSON.stringify({expressions:[]})):aiFetch(path,options);
const {onboardingFinishing}=await import('../cbs/web/static/onboarding-finishing.js');
const finishing=await onboardingFinishing(root,{name:'ユキ',secondPartner:true});await finishing.go('done');
const done=root.querySelector('.onboarding-unframed');assert(done.firstElementChild.classList.contains('face'));assert(!progress.classList.contains('done-message'));assert.equal(done.lastElementChild,progress);assert(!root.classList.contains('onboarding-centered'));
console.log('PASS OB7 DOM: cover centering, unframed cover/done, progress retained inside body, fixed radios, pending connection and success/failure');

