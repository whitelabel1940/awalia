import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<div id="root"></div><p id="error" hidden></p>',{url:'https://unit.invalid/#settings/ai'});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
const cfg=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',
  "import json; from cbs.config import load_config; from pathlib import Path; print(json.dumps(load_config(data_dir=Path('.tmp/s2_node_config')).model_dump()))"],{encoding:'utf8'}));
cfg.advanced_presets=cfg.advanced.presets;cfg.advanced_models=cfg.advanced_presets[0].models;
cfg.models=structuredClone(cfg.advanced_models);cfg.llm.mode='advanced';cfg.keys={openrouter:'set',gemini:'set',openai:'set',anthropic:'set'};
cfg.warnings=[];cfg.reindex_required=false;cfg.tts=null;
const calls=[];let allowReindex=false;
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,body,path});
  let data;
  if(route==='/api/settings'){
    if(body){Object.assign(cfg,body);cfg.advanced_models=structuredClone(body.models);cfg.reindex_required=true;}
    data=cfg;
  }else if(route==='/api/maintenance/reindex')data={status:'completed',completed:1,total:1};
  else if(route==='/api/embedding/download')data={status:'ready',model:'ruri-v3-130m'};
  else if(route==='/api/stt/download')data={status:'ready',model:'haas-ja'};
  else if(route==='/api/settings/connection')data={status:'connected'};
  else if(route==='/api/settings/keys')data={status:'set'};
  else throw new Error('Unexpected fake endpoint '+route);
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.querySelectorAll('button')[allowReindex?1:0].click();};
dom.window.HTMLDialogElement.prototype.close=function(){this.remove();};
const {settingsScreen}=await import('../cbs/web/static/settings.js');
await settingsScreen(document.getElementById('root'),'テスト係',{setup:true});
const input=(label,value)=>{
  const l=[...document.querySelectorAll('label')].find(l=>l.firstChild?.textContent===label);assert(l,label);
  const element=l.querySelector('input,select');element.value=value;element.dispatchEvent(new dom.window.Event('input'));return element;
};
const control=label=>[...document.querySelectorAll('label')].find(l=>l.firstChild?.textContent===label)?.querySelector('input,select');
const keys=()=>[...document.querySelectorAll('input[type=password]')].filter(e=>e.closest('label').isConnected&&e.closest('label').textContent!=='OpenRouter の API キー').map(e=>e.closest('label').textContent);
assert.equal(control('AI の組み合わせ').value,'mixed');
assert(document.body.textContent.includes('Haas-ja は日本語向けの高速な文字起こし'));
assert(document.body.textContent.includes('約0.66 GB'));
assert(!document.body.textContent.includes('whisper.cpp'));
for(const id of ['gemini','claude','openai','mixed']){
  input('AI の組み合わせ',id);
  const preset=cfg.advanced_presets.find(p=>p.id===id);
  for(const [role,title] of [['actor','会話'],['judge','判断'],['steward','記憶の整理'],['embedding','記憶の検索']]){
    const [provider,...model]=preset.models[role].split('/');
    assert.equal(control(`${title}のサービス`).value,provider);assert.equal(control(`${title}のモデル名`).value,model.join('/'));
  }
  assert.equal(control('調べ物のモデル名').value,preset.models.research);
  assert.equal(control('文字起こしのサービス').value,preset.models.stt.split('/')[0]);
  assert.equal(control('文字起こしのモデル名').value,preset.models.stt.split('/').slice(1).join('/'));
  const visible=[...document.querySelectorAll('label')].filter(l=>l.textContent.endsWith('の API キー')&&l.closest('div')?.parentElement?.parentElement?.hidden!==true);
  // The detached key controls of earlier presets must not be submitted.
  assert.equal(keys().length,id==='mixed'?0:1);
  if(id==='openai')assert(document.body.textContent.includes('会話が役になりきりにくい'));
  if(id==='claude')assert(document.body.textContent.includes('初回に取得が要ります'));
  if(id==='claude')assert.equal(control('文字起こしのモデル名').value,'haas-ja');
}
input('会話のモデル名','custom');assert.equal(control('AI の組み合わせ').value,'custom');
input('AI の組み合わせ','claude');
const form=document.querySelector('form');
await form.onsubmit({preventDefault(){}});
assert(!calls.some(c=>c.route==='/api/settings'&&c.body),'Declining reindex leaves settings unchanged');
allowReindex=true;await form.onsubmit({preventDefault(){}});
const saved=calls.find(c=>c.route==='/api/settings'&&c.body);
assert.deepEqual(saved.body.models,cfg.advanced_presets.find(p=>p.id==='claude').models);
assert(!calls.some(c=>c.route==='/api/maintenance/reindex'),'setup defers reindex');
assert(!saved.body.llm.providers?.anthropic,'Built-in Anthropic is never saved as a local endpoint');
input('ローカルの文字起こしで使う装置','cpu');await new Promise(r=>setImmediate(r));
assert(calls.some(c=>c.route==='/api/stt/download'&&c.path.includes('device=cpu')));
input('設定方法','simple');
assert(control('AI の組み合わせ').disabled);
console.log('PASS S2 offline DOM: four config presets, role overrides/custom, required keys, notices, reindex decline/confirm, simple-mode isolation');
