import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/wizard.html',import.meta.url),'utf8'),{url:'http://unit.invalid/'});
for(const k of ['window','document','location','localStorage','sessionStorage','history'])globalThis[k]=dom.window[k];window.scrollTo=()=>{};
URL.createObjectURL=()=> 'blob:fake';URL.revokeObjectURL=()=>{};globalThis.Audio=class{async play(){}pause(){}};
const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json;from pathlib import Path;from cbs.config import load_config;print(json.dumps(load_config(data_dir=Path('.tmp/st1/config')).model_dump()))"],{encoding:'utf8'}));
let cfg,calls=[],fail=false,candidate;const v={mode:'design',description:'柔らかい声',seed:123,reference:'',external_voice:'alloy'};
function reset(){cfg={...structuredClone(base),keys:{openrouter:'set'},advanced_models:base.models,advanced_presets:base.advanced.presets,ai_ready:true,connection_verified:true};cfg.stt.candidates={'openai/whisper-1':true};cfg.models.stt='local/haas-ja';cfg.tts.enabled=true;calls=[];root.replaceChildren();}
globalThis.fetch=async(url,options={})=>{const path=new URL(url,location.href).pathname,body=options.body?JSON.parse(options.body):null;calls.push({path,method:options.method,body});let data;
 if(path==='/api/settings'){if(body){for(const [k,value]of Object.entries(body))Object.assign(cfg[k],value);}data=cfg;}
 else if(path==='/api/onboarding/state')data={consent:true};
 else if(path==='/api/settings/connection'){if(fail)return new Response(JSON.stringify({detail:'fake failure'}),{status:422});data={status:'connected'};}
 else if(path==='/api/voice/candidate'){candidate=body.voice;data={voice:candidate,candidate_id:'a'.repeat(32)};}
 else if(path==='/api/voice/preview')return new Response(new Uint8Array([82]),{headers:{'Content-Type':'audio/wav'}});
 else if(path==='/api/voice')data={voice:body||v,references:[],preview_text:'こんにちは'};
 else if(path.endsWith('/download'))data={status:'ready',model:path.includes('/stt/')?'haas-ja':'q8_0'};
 else throw Error('Unexpected fake '+path);return new Response(JSON.stringify(data));};
const {onboardingAI}=await import('../cbs/web/static/onboarding-ai.js'),{onboardingFinishing}=await import('../cbs/web/static/onboarding-finishing.js');
const {entryNotice,returnToSettings,entryTargets,entryMessages}=await import('../cbs/web/static/settings-entry.js');
const root=document.getElementById('screen'),tick=async()=>{for(let i=0;i<15;i++)await new Promise(r=>setImmediate(r));};
const button=text=>[...root.querySelectorAll('button')].find(b=>b.textContent===text);const next=async()=>{await root.querySelector('.wizard-actions .primary').onclick();await tick();};
const edit=async title=>{const row=[...root.querySelectorAll('.summary-row')].find(r=>r.firstChild.textContent===title);assert(row,title);await row.querySelector('button').onclick();await tick();};
const writes=()=>calls.filter(c=>c.method==='PUT');
for(const kind of ['ai','voice','input']){reset();if(kind==='ai'){void onboardingAI(root,{settingsEntry:true});await tick();}else await onboardingFinishing(root,{name:'テスト係',settingsEntry:kind});
 assert(root.querySelector('h2').textContent.startsWith('今の'));assert.equal(root.querySelectorAll('.chapter-progress span').length,1);assert(root.querySelector('.primary').disabled);assert.equal(button('設定に戻る').textContent,'設定に戻る');assert.equal(writes().length,0);
 if(kind==='ai'){await edit('会話を保存しない提供元');const input=[...root.querySelectorAll('input[type=radio]')].find(i=>!i.checked);input.checked=true;input.onchange();await next();assert(root.querySelector('h2').textContent.startsWith('今の'));assert(!root.querySelector('.primary').disabled);fail=true;await next();assert(root.textContent.includes('fake failure'));assert.equal(writes().length,0);fail=false;}
 if(kind==='voice'){await edit('声の説明');const input=root.querySelector('textarea');input.value='新しい声';input.dispatchEvent(new window.Event('input'));await next();assert(root.querySelector('.primary').disabled);await button('設定に戻る').onclick();assert.equal(writes().length,0);await button('声を確かめる').onclick();await tick();await button('声を試す').onclick();await next();assert(!root.querySelector('.primary').disabled);await next();assert(calls.some(c=>c.path==='/api/voice'&&c.method==='PUT'));assert(!calls.some(c=>c.path.startsWith('/api/setup/')));}
 if(kind==='input'){await edit('使う装置');const select=root.querySelector('select');select.value='cpu';select.dispatchEvent(new window.Event('input'));await next();assert(root.querySelector('h2').textContent.startsWith('今の'));assert(!root.querySelector('.primary').disabled);}
 const before=writes().length;if(button('設定に戻る'))await button('設定に戻る').onclick();assert.equal(writes().length,before);if(kind!=='voice'){await next();assert(writes().length>before);}assert.equal(location.hash,'#settings/'+entryTargets[kind]);
 sessionStorage.removeItem('cbs.settingsEntry');returnToSettings(kind,true);assert.equal(entryTargets[kind],{ai:'ai',voice:'look',input:'screen'}[kind]);const host=document.createElement('div');entryNotice(host);assert(host.textContent.includes(entryMessages[kind]));assert.equal(host.querySelector('[role=status]').textContent,entryMessages[kind]);const again=document.createElement('div');entryNotice(again);assert.equal(again.textContent,'');
}
reset();cfg.llm.mode='advanced';cfg.models.judge='gemini/independent-judge';cfg.models.steward='gemini/independent-steward';void onboardingAI(root,{settingsEntry:true});await tick();assert(root.textContent.includes('カスタム（役ごとに指定）'));await edit('AI の組み合わせ');await next();
for(const title of ['会話','判断','記憶の整理','記憶の検索','写真の読み取り','文字起こし','調べもの'])assert(root.textContent.includes(title+'のモデル名'),title);
const judge=[...root.querySelectorAll('label')].find(l=>l.firstChild.textContent==='判断のモデル名').querySelector('input');judge.value='changed-judge';judge.dispatchEvent(new window.Event('input'));const steward=[...root.querySelectorAll('label')].find(l=>l.firstChild.textContent==='記憶の整理のモデル名').querySelector('input');assert.equal(steward.value,'independent-steward');await next();await root.querySelector('.wizard-actions button').onclick();await tick();assert(root.querySelector('h2').textContent.startsWith('今の'));assert.equal(writes().length,0);
reset();delete cfg.models.stt;await onboardingFinishing(root,{name:'テスト係',settingsEntry:'input'});assert(root.querySelector('.primary').disabled);assert(root.textContent.includes('未設定'));
const wizard=readFileSync(new URL('../cbs/web/static/wizard.js',import.meta.url),'utf8');assert(wizard.includes("if(!partner){location.replace('/wizard');return;}"));
console.log('PASS ST1 DOM: three summaries, edit/return, unchanged disabled, cancel without save, failed AI no writes, current partner voice, return targets and one-time notices');

