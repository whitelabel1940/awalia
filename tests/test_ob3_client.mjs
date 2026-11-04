import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<main id="screen"></main><p id="error" hidden></p>',{url:'http://127.0.0.1/wizard'});
for(const k of ['window','document','location','localStorage','sessionStorage'])globalThis[k]=dom.window[k];
window.scrollTo=()=>{};
window.HTMLCanvasElement.prototype.getContext=()=>({clearRect(){},drawImage(){}});
const root=document.getElementById('screen'),calls=[];
const catalog=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c','import json;from cbs.look import field_catalog;print(json.dumps(field_catalog()))'],{encoding:'utf8'}));
const baseline={models:{actor:'gemini/speech',judge:'openai/text',stt:'gemini/speech'},llm:{mode:'simple'},tts:{enabled:false,autoplay:true,autoplay_proactive:false,engine:'speech.cpp',local_model:'auto',styles:{external:{mode:'none'}}},stt:{candidates:{'gemini/speech':true,'openai/whisper':true},local_models:{'haas-ja':{}},device:'auto',max_seconds:60,silence_seconds:2,auto_send:false},backup:{folder:'C:/backup',frequency:'weekly'},backup_status:{trial:false}};
let cfg,voiceReady=true,sttReady=true,voiceValue,savedLook;
const tick=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
function reset(){savedLook={person:{},image:{},expressions:[]};cfg=structuredClone(baseline);voiceValue={mode:'design',description:'保存された声の下書き',external_voice:'test'};root.replaceChildren();calls.length=0;document.getElementById('error').textContent='';voiceReady=sttReady=true;}
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,body,method:options.method});let result;
  if(route.startsWith('/api/onboarding/'))result={text:`# ${route.split('/').at(-1)}\n\n{name}の準備`};
  else if(route==='/api/settings'){if(body?.tts)cfg.tts=structuredClone(body.tts);result=cfg;}
  else if(route==='/api/setup/voice-step')result={status:'done'};
  else if(route==='/api/look')result=structuredClone(savedLook);
  else if(route==='/api/look/fields')result=catalog;
  else if(route==='/api/look/prompt')result={prompt:'画像の指示文'};
  else if(route==='/api/look/expression-suggestions')result={suggestions:[{path:'person.expressions.smile',value:'目を細める',quote:'よく笑う'}]};
  else if(route==='/api/look/meta'){savedLook=structuredClone(body);result=savedLook;}
  else if(route==='/api/look/draft')result={assignments:[{path:'person.style.base',value:'水彩',quote:'設定の引用'}],suggestions:[]};
  else if(route==='/api/voice/download'){if(body)voiceReady=true;result={status:voiceReady?'ready':'missing',download_bytes:1048576,reason:'取得後に GPU を調べます。自動では q8 の容量を上限として確認します。'};}
  else if(route==='/api/voice/download/plan')result={token:'voice-plan'};
  else if(route==='/api/voice'){if(body)voiceValue=body;result={voice:voiceValue,references:[],preview_text:'こんにちは'};}
  else if(route==='/api/voice/candidate')result={voice:body.voice,candidate_id:'candidate'};
  else if(route==='/api/voice/design')result={voice:{...voiceValue,description:'性格からの下書き'}};
  else if(route==='/api/stt/download'){if(body)sttReady=true;result={status:sttReady?'ready':'missing',model:'haas-ja'};}
  else if(route==='/api/stt/download/plan')result={token:'stt-plan'};
  else if(route==='/api/backup/settings')result={config:{folder:body.folder,frequency:body.frequency}};
  else throw Error('Unexpected fake endpoint: '+route);
  return new Response(JSON.stringify(result));
};
function visible(e){for(let at=e;at&&at!==root;at=at.parentElement)if(at.hidden)return false;return true;}
async function click(text){const b=[...root.querySelectorAll('button')].find(e=>visible(e)&&e.textContent===text);assert(b,`Missing ${text}: ${root.textContent}`);await b.onclick();await tick();assert(!document.getElementById('error').textContent,document.getElementById('error').textContent);}
async function next(){await tick();const list=[...root.querySelectorAll('.primary')].filter(visible);assert.equal(list.length,1,root.textContent);assert(!list[0].disabled);await list[0].onclick();await tick();assert(!document.getElementById('error').textContent,document.getElementById('error').textContent);}
function choice(title,value){const box=[...root.querySelectorAll('fieldset')].find(e=>visible(e)&&e.querySelector('legend').textContent===title);assert(box,title);if(value){const e=[...box.querySelectorAll('input')].find(e=>e.value===value);e.checked=true;e.dispatchEvent(new window.Event('change'));}return box.querySelector('input:checked').value;}
function field(label){const input=[...root.querySelectorAll('label.f')].find(e=>visible(e)&&e.firstChild.textContent===label)?.querySelector('input,textarea,select');assert(input,`Missing ${label}`);return input;}
function input(label,value){const e=field(label);e.value=value;e.dispatchEvent(new window.Event('input'));}
const {onboardingFinishing}=await import('../cbs/web/static/onboarding-finishing.js');
const {defaultSTTModel}=await import('../cbs/web/static/stt.js');
assert.equal(defaultSTTModel(baseline),'gemini/speech');
assert.equal(defaultSTTModel({...baseline,models:{actor:'openai/text'}}),'openai/whisper');
assert.equal(defaultSTTModel({...baseline,models:{actor:'anthropic/text',stt:'gemini/speech'}}),'local/haas-ja');
// Skip each chapter, preserving the confirmed partner and completing the voice flag.
reset();let flow=await onboardingFinishing(root,{name:'ユキ'});assert(!root.querySelector('.progress'));assert.equal(root.querySelectorAll('.chapter-progress>span').length,7);assert.equal(root.querySelector('[aria-current=step]').getAttribute('aria-label'),'見た目');assert.equal(root.querySelector('.chapter-caption strong').textContent,'見た目');assert.equal(root.querySelector('.chapter-caption>span').textContent,'1 / 1');assert.deepEqual([...root.querySelectorAll('.wizard-actions button')].map(b=>b.textContent),['戻る','飛ばす','次へ']);await click('飛ばす');await click('あとで');assert.equal(calls.findLast(c=>c.route==='/api/setup/voice-step').body?.skipped,true);await click('あとで');await click('あとで');assert.equal(root.querySelector('.primary').textContent,'ユキと話しはじめる');assert(root.querySelector('.onboarding-done .face img').src.endsWith('/static/brand/symbol.svg'));assert.equal(root.querySelector('.done-actions').lastElementChild.textContent,'戻る');const browserLocation=globalThis.location;globalThis.location={href:''};await next();assert.equal(location.href,'/#talk');globalThis.location=browserLocation;
reset();flow=await onboardingFinishing(root,{name:'ユキ',secondPartner:true});await click('飛ばす');await click('あとで');assert(!root.textContent.includes('声で入力'));assert(!root.textContent.includes('バックアップ'));assert(root.textContent.includes('ユキと話しはじめる'));
// Look pages use the saved editor, retain fields on Back, fold image settings, and save incrementally.
reset();localStorage.setItem('cbs.look.details.style','true');flow=await onboardingFinishing(root,{name:'ユキ'});await click('設定文と性格から下書きする');assert(root.textContent.includes('下書きが完了'));await next();assert.equal(field('絵柄').value,'水彩');assert.equal(savedLook.person.style.base,'水彩');assert.equal(root.querySelector('details').open,false);input('絵柄','鉛筆');await next();assert(root.textContent.includes('人物像'));assert.equal(savedLook.person.style.base,'鉛筆');assert.equal((await (await fetch('/api/look')).json()).person.style.base,'鉛筆');await click('戻る');assert.equal(field('絵柄').value,'鉛筆');await next();await next();await next();await next();await next();assert(root.textContent.includes('見た目の確認'));assert(root.querySelector('.summary-row').textContent.includes('鉛筆'));assert(root.querySelector('.summary-row button').textContent==='直す');await click('直す');assert.equal(field('絵柄').value,'鉛筆');input('絵柄','水彩に修正');await next();assert.equal(savedLook.person.style.base,'水彩に修正');await flow.go('look-confirm');await next();assert([...root.querySelectorAll('details>summary')].some(e=>e.textContent==='細かい設定'));
assert(!root.querySelector('pre').closest('details'),'prompt stays expanded');
const beforePrompts=calls.filter(c=>c.route==='/api/look/prompt').length;
await click('8つの表情を性格から下書きする');
assert(root.textContent.includes('1欄に下書きしました'));assert(calls.filter(c=>c.route==='/api/look/prompt').length>beforePrompts);
assert.equal(calls.filter(c=>c.route==='/api/look/prompt').at(-1).body.person.expressions.smile,'目を細める');
await click('うまく表情シートが出ない時は');assert.equal(calls.filter(c=>c.route==='/api/look/prompt').at(-1).body.image.use,'single');await next();const faceInput=[...root.querySelectorAll('input[type=file]')].find(e=>e.multiple===false&&e.closest('label').textContent.includes('別々'));assert(faceInput);assert(![...root.querySelectorAll('input[type=file]')].some(visible));let opened=0;faceInput.click=()=>opened++;await click('顔の画像を1枚選ぶ');assert.equal(opened,1);await next();await next();await next();assert(root.textContent.includes('voice'));assert(calls.filter(c=>c.route==='/api/look/meta').length>=8);
// Local voice: already-installed download is bypassed; draft text and shared candidate decision are reused.
await next();assert(root.querySelector('h2').textContent==='返事を自動で読み上げる？');assert.equal(choice('自動で読み上げる'),'yes');assert(![...root.querySelectorAll('label')].filter(visible).some(e=>e.textContent.includes('自分から')));await next();await next();assert.equal(choice('声の作り方'),'design');await next();assert.equal(field('声の説明').value,'保存された声の下書き');await click('設定と性格から下書き');assert.equal(field('声の説明').value,'性格からの下書き');await next();assert.equal(field('再生の音量').type,'range');assert(field('再生の音量').parentElement.querySelector('output'));assert(![...root.querySelectorAll('select')].filter(visible).length);await click('笑顔');assert.equal(root.querySelector('.voice-expressions [aria-pressed=true]').textContent,'笑顔');assert.equal(root.querySelectorAll('.voice-preview-actions button').length,2);input('再生の音量','37');assert.equal(localStorage.getItem('cbs.voiceVolume'),'0.37');await next();assert(calls.some(c=>c.route==='/api/voice'&&c.method==='PUT'));assert(calls.some(c=>c.route==='/api/setup/voice-step'&&c.body.skipped===false));assert.equal(cfg.tts.autoplay_proactive,false);
// Service STT, shared advanced settings, simple-mode save, backup accordion and confirmation.
await next();assert(root.querySelector('input:checked').value==='service');await next();assert.equal(root.querySelectorAll('button[aria-expanded=false]').length,2);assert(root.textContent.includes('60 秒'));assert(!root.querySelector('details'));assert.equal(choice('話し終えた後'),'review');choice('話し終えた後','send');await next();await next();assert(calls.some(c=>c.route==='/api/settings'&&c.body?.simple?.stt==='gemini/speech'));await next();assert(root.querySelectorAll('button[aria-expanded]').length===3);await next();assert(root.textContent.includes('バックアップの確認'));await next();assert(root.textContent.includes('ユキと話しはじめる'));
// Downloads use one acquire action and auto advance; Claude-only defaults to Haas.
reset();voiceReady=false;cfg.models={actor:'anthropic/text',stt:'local/haas-ja'};flow=await onboardingFinishing(root,{name:'ユキ'});await click('飛ばす');await next();await next();await next();assert(root.textContent.includes('声に必要'));assert(root.textContent.includes('1.0 MB'));assert(!root.textContent.includes('取得後に GPU'));assert(root.querySelector('.wizard-actions>.primary').disabled);await click('取得する');await new Promise(r=>setTimeout(r,800));await tick();assert(root.textContent.includes('声に必要'));await next();assert.equal(choice('声の作り方'),'design');await flow.go('voice');await click('あとで');await next();assert(root.querySelector('input:checked').value==='local');await next();assert(field('装置'),'installed Haas skips download');await next();await next();await next();
reset();sttReady=false;cfg.backup_status.trial=true;cfg.models={actor:'anthropic/text'};flow=await onboardingFinishing(root,{name:'ユキ'});await flow.go('voice_input');await next();await next();assert.equal(root.querySelector('.primary').textContent,'次へ');assert(root.querySelector('.primary').disabled);await click('取得する');assert(root.textContent.includes('準備完了'));await next();assert(field('装置'));await flow.go('export');cfg.backup_status.trial=true;await next();assert(root.textContent.includes('お試しでは'));assert(!calls.some(c=>c.route==='/api/backup/settings'));await next();
// Reference voice follows the selected method and keeps it on Back.
reset();flow=await onboardingFinishing(root,{name:'ユキ'});await flow.go('voice');await next();await next();await next();choice('声の作り方','clone');await next();assert([...root.querySelectorAll('input[type=file]')].some(visible));await click('戻る');assert.equal(choice('声の作り方'),'clone');await next();assert(root.querySelector('h2').textContent==='参照音声');
// External voice uses four separate fields and the same confirmation/candidate API.
reset();flow=await onboardingFinishing(root,{name:'ユキ'});await flow.go('voice');await next();await next();choice('声のエンジン','external');await next();input('外部の音声サーバーURL（/v1 まで）','http://127.0.0.1:8080/v1');await next();input('外部のモデル名（任意）','voice-model');await next();input('外部の声の名前','ユキ');await next();assert(field('外部エンジンの声の調子'));await next();assert([...root.querySelectorAll('button')].some(e=>visible(e)&&e.textContent==='声を試す'));await next();assert(calls.some(c=>c.route==='/api/voice'&&c.body?.external_voice==='ユキ'));
reset();flow=await onboardingFinishing(root,{name:'ユキ'});await flow.go('look-cover');assert(root.textContent.includes('扉絵はなくても使えます'));await click('飛ばす');assert(root.textContent.includes('voice'));assert(!root.querySelector('input[type=file]'));await flow.go('look-confirm');assert(root.textContent.includes('まだ決めていません'));
// UV4 shared image pickers stay hidden and each visible tile opens its own input.
reset();let returned=0;flow=await onboardingFinishing(root,{name:'ユキ',onBack:()=>returned++});await click('戻る');assert.equal(returned,1);await flow.go('look-faces');assert.equal(root.querySelectorAll('.expression-picker').length,8);assert(![...root.querySelectorAll('input[type=file]')].some(visible));
let picked=0;const sheetInput=root.querySelector('.drop input');sheetInput.click=()=>picked++;await click('ファイルを選ぶ');assert.equal(picked,1);
for(const picker of root.querySelectorAll('.expression-picker')){const input=picker.parentElement.querySelector('input[type=file]');input.click=()=>picked++;await picker.onclick();}assert.equal(picked,9);
const {selectPartner}=await import('../cbs/web/static/ui.js');selectPartner('unit');savedLook.expressions=['neutral','smile'];await flow.go('done');assert(root.querySelector('.onboarding-done .face img').src.includes('/smile'));savedLook.expressions=['neutral'];await flow.go('done');assert(root.querySelector('.onboarding-done .face img').src.includes('/neutral'));selectPartner('');
console.log('PASS OB3 DOM: chapter skips, done, second partner, shared look steps/back/save, voice local/external/download/default text, STT defaults/installed/download/simple save, backup/trial');
