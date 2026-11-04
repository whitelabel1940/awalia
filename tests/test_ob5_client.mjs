import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<nav id="menu"></nav><main id="screen"></main><p id="error"></p>',{url:'http://localhost/wizard'});
for(const k of ['window','document','location','history','localStorage','sessionStorage'])globalThis[k]=dom.window[k];
let scrolls=0,scrolled;window.scrollTo=()=>scrolls++;window.HTMLElement.prototype.scrollIntoView=function(){scrolled=this;};
const root=document.getElementById('screen');
const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json;from pathlib import Path;from cbs.config import load_config;print(json.dumps(load_config(data_dir=Path('.tmp/ob5/config')).model_dump()))"],{encoding:'utf8'}));
const cfg={...base,keys:{openrouter:'set'},advanced_models:base.models,advanced_presets:base.advanced.presets,ai_ready:true,connection_verified:true};
let checks=0,downloadState='ready';
globalThis.fetch=async(path,options={})=>{
 const body=options.body?JSON.parse(options.body):null;let result;
 if(path==='/api/settings'){if(body?.llm)Object.assign(cfg.llm,body.llm);if(body?.models)cfg.models=body.models;result=cfg;}
 else if(path==='/api/onboarding/state')result={consent:true};
 else if(path.startsWith('/api/onboarding/'))result={text:'# '+path.split('/').at(-1)};
 else if(path==='/api/settings/connection'){checks++;result={ok:true};}
 else if(path.startsWith('/api/embedding/download'))result={status:downloadState,model:'ruri-v3-130m',completed:0,total:0,error:downloadState==='error'?'fake download failure':''};
 else if(path==='/api/restore')result={trial:false,max_bytes:1048576};
 else if(path==='/api/settings/keys')result={};
 else throw Error('Unexpected fake endpoint '+path);
 return new Response(JSON.stringify(result));
};
const tick=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
async function click(text){const b=[...root.querySelectorAll('button')].find(b=>b.textContent===text);assert(b,text);await b.onclick();await tick();}
async function next(){const b=root.querySelector('.wizard-actions .primary');assert(!b.disabled);await b.onclick();await tick();}
async function choose(value){const e=[...root.querySelectorAll('input[type=radio]')].find(e=>e.value===value);assert(e,value);e.checked=true;e.dispatchEvent(new window.Event('change'));await tick();}
const {notice,policyDescriptions,choiceCards}=await import('../cbs/web/static/onboarding.js');
const n=notice(root,'完了');assert.equal(n.getAttribute('role'),'status');assert(n.classList.contains('success'));assert(n.querySelector('svg'));assert.equal(n.textContent,'完了');n.update('失敗','error');assert.equal(n.getAttribute('role'),'alert');assert(n.classList.contains('error'));assert.notEqual(policyDescriptions.keep,policyDescriptions.may_elaborate);
root.replaceChildren();choiceCards(root,'設定',Object.entries(policyDescriptions).map(([v,d])=>[v,v,d]),'keep',()=>{});assert.equal(new Set([...root.querySelectorAll('.choice-description')].map(e=>e.textContent)).size,2);
const {embeddingDownload}=await import('../cbs/web/static/embedding-download.js');
root.replaceChildren();const download=embeddingDownload(root,()=> 'local/ruri-v3-130m',{onboarding:true});await download.refresh();
assert(root.querySelector('.success').textContent.includes('ruri-v3-130mの取得が完了しました。次へ進めます。'));
assert(root.querySelector('.status').hidden);downloadState='error';await download.refresh();assert(root.querySelector('[role=alert]'));assert(root.querySelector('.status').hidden);
const {welcomeRestore}=await import('../cbs/web/static/restore.js');
const {sequence}=await import('../cbs/web/static/onboarding.js');
await sequence(root,[{id:'welcome',label:'はじめる',render:p=>p.append('本文')}],{onWelcome:welcomeRestore}).start();
assert.deepEqual([...root.querySelectorAll('.wizard-actions button')].map(e=>e.textContent),['はじめる','バックアップから始める']);
assert(root.querySelector('.welcome-actions'));assert(!root.querySelector('.restore-settings'));
await click('バックアップから始める');assert(root.querySelector('#restore-file'));assert(root.querySelector('.onboarding-unframed').textContent.startsWith('本文'));
const {onboardingPartner}=await import('../cbs/web/static/onboarding-partner.js');
const data={id:'draft',basics:{name:'ユキ'},world:{preset:'現代日本',contact:'B2'},persona:{ai_question:'immersive'},text:'設定を保持',now:{likes:[],life:{}},answers:{q:{answer:['A']}},life:{},elaborate:{default:'keep'},adaptive_questions:[],adaptive_answers:{},review_notes:{check:'保持'}};
let attempts=0,fail=true;
const core={data,setRender(){},editWorld(k,v){data.world[k]=v;},forgetSettingChecks(){},create:async()=>{attempts++;if(fail)throw Object.assign(Error('fake failure'),{stage:'人物像の生成'});},questions:()=>[]};
const flow=await onboardingPartner(root,core);
await flow.go('partner-fields');await click([...root.querySelectorAll('button')].find(e=>e.textContent.startsWith('世界観')).textContent);
const before=scrolls;await choose('SF');assert.equal(scrolls,before);
await click('次の項目へ');assert.equal(document.activeElement,scrolled);assert(scrolled.textContent.startsWith('地域'));
await click([...root.querySelectorAll('button')].find(e=>e.textContent.startsWith('あなたとの接点')).textContent);
assert.equal(root.querySelector('.badge').closest('label').querySelector('input').value,'C');assert(root.querySelector('input[value=B2]').checked);
await click([...root.querySelectorAll('button')].find(e=>e.textContent.startsWith('「AI なの？')).textContent);
assert.equal(new Set([...root.querySelectorAll('.choice-description')].map(e=>e.textContent)).size,3);
await click([...root.querySelectorAll('button')].find(e=>e.textContent.startsWith('世界観')).textContent);await choose('現代の日本以外');
await click([...root.querySelectorAll('button')].find(e=>e.textContent.startsWith('あなたとの接点')).textContent);
assert.equal(root.querySelector('.badge').closest('label').querySelector('input').value,'A');assert(root.querySelector('input[value=B2]').checked);
const snapshot=JSON.stringify(data);
await flow.go('review');assert(root.querySelector('[role=alert]'));assert(!root.textContent.includes('AI の設定を見直す'));assert(root.textContent.includes('下の「もう一度まとめる」から、もう一度試してください'));assert(!root.textContent.includes('AI の設定を確'));
await click('もう一度まとめる');assert(!root.textContent.includes('AI の設定を見直す'));
await click('もう一度まとめる');assert.equal(attempts,3);assert(root.textContent.includes('AI の設定を見直す'));assert(root.textContent.includes('何度か止まっています'));
const repair=[...root.querySelectorAll('button')].find(b=>b.textContent==='AI の設定を見直す').onclick();await tick();await choose('key');await next();await choose('gemini');await next();if(root.textContent.includes('会話のモデル'))await next();await next();assert.equal(checks,1);assert(root.querySelector('.success'));await next();assert(root.textContent.includes('使うモデルの確認'));assert(root.textContent.includes('（'+cfg.models.actor.split('/').at(-1)+'）'));
fail=false;await next();await repair;await tick();assert.equal(JSON.stringify(data),snapshot);assert(root.textContent.includes('まとめができました'));assert.equal(attempts,4);
fail=true;await flow.go('review'); // successful cached work is retained
assert(!root.textContent.includes('AI の設定を見直す'));
const {onboardingAI}=await import('../cbs/web/static/onboarding-ai.js');void onboardingAI(root,{repair:true});await tick();await next();
const login=[...root.querySelectorAll('button')].find(e=>e.textContent==='OpenRouter と連携する');assert(login.disabled);assert(login.nextElementSibling.classList.contains('success'));
console.log('PASS OB5 DOM: notices, distinct descriptions, same-screen scroll, accordion focus, recommendations, 3 failures, AI repair/connection and retained draft');

// Moving to the next or previous question re-shows the same screen but is still navigation.
{let q=0;root.replaceChildren();const flow=sequence(root,[{id:'question',render:p=>p.append('設問'+q),next:async()=>{q++;await flow.start('question',{top:true});return false;},back:()=>{q--;return 'question';}}]);
await flow.start('question');let at=scrolls;await flow.start('question');assert.equal(scrolls,at,'repaint keeps scroll');
await next();assert.equal(scrolls,at+1,'next question scrolls to top');assert(root.textContent.includes('設問1'));
await click('戻る');assert.equal(scrolls,at+2,'previous question scrolls to top');assert(root.textContent.includes('設問0'));}
console.log('PASS OB5 DOM: question navigation scrolls to top');
