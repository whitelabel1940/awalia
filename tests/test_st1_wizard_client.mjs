import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const kind=process.argv[2];if(!kind){for(const value of ['ai','voice','input'])execFileSync(process.execPath,[new URL(import.meta.url).pathname.slice(1),value],{stdio:'inherit'});process.exit(0);}
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/wizard.html',import.meta.url),'utf8'),{url:'http://unit.invalid/wizard?'+kind+'=1'});
for(const k of ['window','document','location','localStorage','sessionStorage','history'])globalThis[k]=dom.window[k];window.scrollTo=()=>{};
URL.createObjectURL=()=> 'blob:fake';URL.revokeObjectURL=()=>{};globalThis.Audio=class{async play(){}pause(){}};
const base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json;from pathlib import Path;from cbs.config import load_config;print(json.dumps(load_config(data_dir=Path('.tmp/st1/config')).model_dump()))"],{encoding:'utf8'}));
let cfg,calls=[],fail=false,candidate;const v={mode:'design',description:'柔らかい声',seed:123,reference:'',external_voice:'alloy'};
function reset(){cfg={...structuredClone(base),keys:{openrouter:'set'},advanced_models:base.models,advanced_presets:base.advanced.presets,ai_ready:true,connection_verified:true};cfg.stt.candidates={'openai/whisper-1':true};cfg.models.stt='local/haas-ja';cfg.tts.enabled=true;calls=[];root.replaceChildren();}
globalThis.fetch=async(url,options={})=>{const path=new URL(url,location.href).pathname,body=options.body?JSON.parse(options.body):null;calls.push({path,method:options.method,body});let data;
 if(path==='/api/partner')data={partner:{id:'fixture',name:'テスト係'}};
 else if(path==='/api/settings'){if(body){for(const [k,value]of Object.entries(body))Object.assign(cfg[k],value);}data=cfg;}
 else if(path==='/api/onboarding/state')data={consent:true};
 else if(path==='/api/settings/connection'){if(fail)return new Response(JSON.stringify({detail:'fake failure'}),{status:422});data={status:'connected'};}
 else if(path==='/api/voice/candidate'){candidate=body.voice;data={voice:candidate,candidate_id:'a'.repeat(32)};}
 else if(path==='/api/voice/preview')return new Response(new Uint8Array([82]),{headers:{'Content-Type':'audio/wav'}});
 else if(path==='/api/voice')data={voice:body||v,references:[],preview_text:'こんにちは'};
 else if(path.endsWith('/download'))data={status:'ready',model:path.includes('/stt/')?'haas-ja':'q8_0'};
 else throw Error('Unexpected fake '+path);return new Response(JSON.stringify(data));};

const root=document.getElementById('screen');reset();await import('../cbs/web/static/wizard.js');for(let i=0;i<30;i++)await new Promise(r=>setImmediate(r));
assert.equal(document.getElementById('error').textContent,'');assert(root.querySelector('h2')?.textContent.startsWith('今の'),kind+' boots through wizard.js');assert.equal(root.querySelector('.chapter-caption span'),null);assert.equal(root.querySelectorAll('.chapter-progress span').length,1);
assert(!root.querySelector('.setup-helper'),kind+' settings entry excludes help');
if(kind==='input')assert(root.textContent.includes('自動（おすすめ）'));
if(kind==='voice')assert(root.textContent.includes('自動（GPUならq8、CPUならq4）'));
assert(!calls.some(c=>c.method==='PUT'));console.log('PASS ST1 wizard.js entry '+kind);process.exit(0);
