import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom'),dom=new JSDOM('<main id="screen"></main><p id="error"></p>',{url:'https://unit.invalid/?character=fixture#settings'});
for(const k of ['window','document','location','localStorage','sessionStorage','history'])globalThis[k]=dom.window[k];
const root=document.getElementById('screen'),tick=async()=>{for(let i=0;i<15;i++)await new Promise(r=>setImmediate(r));},base=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c',"import json;from pathlib import Path;from cbs.config import load_config;print(json.dumps(load_config(data_dir=Path('.tmp/st4/config')).model_dump()))"],{encoding:'utf8'}));
let cfg={...base,keys:{openrouter:'set'},model_candidates:{actor:[]},stt_status:{enabled:true},backup_status:{jobs:[]},backup:{folder:'C:/sync',frequency:'daily',export_keep:4}},voice={voice:{mode:'design'},warning:null},days={version:1,days:[],habits:[]},requests={version:1,fields:{first_person:'私',second_person:'あなた',user_name:'',register:'普通',length:'短く'},lines:[],limits:{max_field_chars:100,max_lines:10,max_line_chars:100}},writes=[];
globalThis.fetch=async(url,options={})=>{const path=new URL(url,location.href).pathname,body=options.body?JSON.parse(options.body):null;if(body)writes.push({path,body});let value;
if(path==='/api/settings')value=cfg;else if(path==='/api/voice')value=voice;else if(path==='/api/partner/days'){if(body)days={...body,version:days.version+1};value=days;}else if(path==='/api/partner/requests'){if(body)requests={...body,limits:requests.limits,version:requests.version+1};value=requests;}else if(path==='/api/persona')value={ai_question:'honest'};else if(path.endsWith('/augment'))value={questions:[]};else if(path==='/api/sources')value=[];else if(path==='/api/look')value={expressions:[]};else if(path==='/api/costs/notice')value={notice:null};else value={};return new Response(JSON.stringify(value));};
const {settingsShelves}=await import('../cbs/web/static/settings-shelves.js');
async function show(route=''){root.replaceChildren();history.replaceState(null,'','#settings'+route);await settingsShelves(root,'テスト係');}

cfg.autonomy.inner_life=true;
cfg.autonomy_status={real_world:true,world_activity:false,tools:{watch_video:{state:'unavailable',reason:'動画は OpenRouter か Gemini でつないでいる時に観られます。',fix:'/wizard?ai=1'},read_source:{state:'waiting',reason:'よく読むサイトを登録すると読みます。',fix:null}}};
await show('/pace');
const video=[...root.querySelectorAll('.activity-row')].find(e=>e.textContent.includes('動画を観る'));
assert(video.textContent.includes('今は使えません'));assert(video.querySelector('.warn svg'));assert.equal(video.querySelector('.warn a').getAttribute('href'),'/wizard?ai=1');assert(!video.querySelector('input').disabled);assert(video.textContent.includes('よく読むサイトの YouTube チャンネルと、調べ物で見つけた動画から1本観る'));
assert(video.textContent.includes('動画は OpenRouter か Gemini でつないでいる時に観られます。'));
const switchInput=video.querySelector('input');switchInput.click();await tick();assert.equal(writes.at(-1).body.autonomy.tools.watch_video.enabled,false);assert.equal(video.querySelectorAll('.activity-status>svg').length,1);
const source=[...root.querySelectorAll('.activity-row')].find(e=>e.textContent.includes('登録したサイト'));
assert(source.querySelector('.activity-status.muted'));assert(!source.querySelector('.warn'));assert(!root.textContent.includes('テスト係の暮らす世界で'));
await show();assert(root.textContent.includes('（今は使えないもの 1件）'));assert(!root.querySelector('.settings-issues'));
cfg.autonomy.tools.watch_video.enabled=false;await show();assert(!root.textContent.includes('今は使えないもの'));
cfg.autonomy_status.tools.browse_shopping={state:'unavailable',reason:'Gemini に直接つないでいる間は眺めません（検索するサイトを限れないため）。',fix:'/wizard?ai=1'};
cfg.autonomy_status.tools.watch_video.reason='「会話を保存しない提供元だけを使う」にしている間は観られません。';
await show('/pace');
for(const reason of [cfg.autonomy_status.tools.browse_shopping.reason,cfg.autonomy_status.tools.watch_video.reason]){
const row=[...root.querySelectorAll('.activity-row')].find(e=>e.textContent.includes(reason));
assert(row);assert.equal(row.querySelector('.warn a').getAttribute('href'),'/wizard?ai=1');assert(!row.querySelector('input').disabled);
}
cfg.autonomy_status.real_world=false;cfg.autonomy_status.world_activity=true;await show('/pace');assert.equal(root.querySelectorAll('.activity-row').length,1);assert(root.textContent.includes('テスト係の世界は現実と違うため、調べ物・サイト・本・動画・Wikipedia・買い物は行わず、世界の中で過ごします。'));
cfg.autonomy_status.world_activity=false;await show('/pace');assert.equal(root.querySelectorAll('.activity-row').length,0);assert(root.textContent.includes('連絡の取り方が C'));
console.log('PASS ST4 DOM');process.exit(0);
