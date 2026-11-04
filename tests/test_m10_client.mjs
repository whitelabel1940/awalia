import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const previewText=JSON.parse(readFileSync(new URL('../cbs/web/static/voice-preview.json',import.meta.url),'utf8')).text;
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<div id="error" hidden></div><main id="screen"></main>',{url:'https://unit.invalid/?character=fixture'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
const calls=[],sounds=[],revoked=[];
let rejectPlay=false,failAudio=false,streaming=false;
let references=[{id:'existing.wav',name:'既存.wav',seconds:12}];
let candidate;
let download={status:'missing',total:1200000000,completed:0,percent:0,reason:'GPU のため q8'};
const referenceResult=()=>({preview_text:previewText,voice,references,total_seconds:references.reduce((n,c)=>n+c.seconds,0)});
globalThis.Audio=class {
  constructor(url){this.url=url;this.paused=false;sounds.push(this);}
  async play(){if(rejectPlay){const e=new Error('blocked');e.name='NotAllowedError';throw e;}if(streaming)setImmediate(()=>this.onended?.());}
  pause(){this.paused=true;}
};
URL.createObjectURL=()=>`blob:test-${sounds.length}`;
URL.revokeObjectURL=url=>revoked.push(url);
const voice={mode:'design',description:'柔らかな声',seed:123,reference:'',external_voice:'alloy',steps:null};
const cfg={tts:{enabled:false,autoplay:true,engine:'speech.cpp',base_url:'http://127.0.0.1:8080/v1',model:'',device:'auto',cache_days:7,styles:{'speech.cpp':{mode:'emoji',values:{smile:'😊'}},external:{mode:'none',values:{smile:'明るく'}}}}};
globalThis.fetch=async(url,options={})=>{
  const route=new URL(url,location.href).pathname,body=typeof options.body==='string'?JSON.parse(options.body):options.body;
  calls.push({route,method:options.method,body});
  const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
  if(route.endsWith('/voice/candidate')){candidate=structuredClone(body);if(candidate.voice.mode==='clone')candidate.voice.reference='combined.gguf';return json({candidate_id:'a'.repeat(32),voice:candidate.voice});}
  if((route==='/api/voice'||route==='/api/setup/voice')&&options.method==='PUT'){Object.assign(voice,body.candidate_id?candidate.voice:body);return json({voice});}
  if(route==='/api/voice'||route==='/api/setup/voice')return json(referenceResult());
  if(route==='/api/voice/download/plan')return json({...download,token:'confirmed-size',model:'q8_0',download_bytes:1200000000});
  if(route==='/api/voice/download/cancel'){download={...download,status:'cancelled'};return json(download);}
  if(route==='/api/voice/download'&&options.method==='POST'){assert(body.confirmed&&body.token==='confirmed-size');download={...download,status:'downloading',stage:'model',completed:300000000,percent:25,eta_seconds:60};return json(download);}
  if(route==='/api/voice/download')return json(download);
  if(route==='/api/voice/references'&&options.method==='DELETE'){references=[];return json(referenceResult());}
  if(route.startsWith('/api/voice/references/')&&options.method==='DELETE'){references=references.filter(c=>c.id!==route.split('/').at(-1));return json(referenceResult());}
  if(route.endsWith('/voice/sample')){candidate.voice.sample_reference='sample-test.wav';return json({voice:candidate.voice});}
  if(route==='/api/setup/voice-step')return json({status:'done'});
  if(route==='/api/voice/design')return json({voice:{...voice,description:'新しい声の説明'}});
  if(route==='/api/voice/reference'){references.push({id:'uploaded.wav',name:'追加.wav',seconds:10});return json({...referenceResult(),uploaded:'uploaded.wav'});}
  if(route==='/api/settings'){Object.assign(cfg.tts,body.tts);return json(cfg);}
  if(streaming&&route.endsWith('/audio/stream'))return new Response('event: audio\ndata: {"wav":"UklGRg=="}\n\nevent: audio\ndata: {"wav":"UklGRg=="}\n\nevent: done\ndata: {}\n\n',{headers:{'Content-Type':'text/event-stream'}});
  if(route.endsWith('/audio/stream')||route.endsWith('/voice/preview')||route.endsWith('/voice-preview'))return failAudio?json({detail:'音声を作れませんでした。'},503):new Response(new Uint8Array([82,73,70,70]),{headers:{'Content-Type':'audio/wav',...(candidate?.voice.mode==='design'?{'X-Voice-Sample':'ready'}:{})}});
  throw new Error(`Unexpected offline endpoint ${route}`);
};
const tick=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));};
const {configureVoice,stopVoice,voiceButton,autoplayTurn,voiceSettings}=await import('../cbs/web/static/voice.js');
const screen=document.getElementById('screen');
configureVoice();voiceButton(screen,1,{autoplay:true});assert.equal(calls.length,0);assert.equal(screen.children.length,0);
configureVoice({...cfg.tts,enabled:true});
const article=document.createElement('article');article.dataset.turnId='2';screen.append(article);
voiceButton(article,2);assert.equal(calls.length,0,'History must not synthesize automatically');
await article.querySelector('button').onclick();assert.equal(sounds.length,1);assert.equal(calls.at(-1).route,'/api/turns/2/audio/stream');
assert(!JSON.stringify(calls.at(-1).body).includes('😊'));
stopVoice();assert(sounds[0].paused);assert(revoked.length);
voiceButton(screen,3,{autoplay:true,pending:true});await tick();assert.equal(sounds.length,1,'Wait for final expression');
autoplayTurn(2);await tick();assert.equal(sounds.length,2);
configureVoice({...cfg.tts,enabled:true,autoplay:false});voiceButton(screen,4,{autoplay:true});await tick();assert.equal(sounds.length,2);
// Speak-ups stay silent unless the separate switch is on.
configureVoice({...cfg.tts,enabled:true});voiceButton(screen,5,{autoplay:true,proactive:true});await tick();assert.equal(sounds.length,2,'Speak-up is not read aloud by default');
configureVoice({...cfg.tts,enabled:true,autoplay_proactive:true});voiceButton(screen,6,{autoplay:true,proactive:true});await tick();assert.equal(sounds.length,3,'Speak-up reads aloud when allowed');
configureVoice({...cfg.tts,enabled:true});rejectPlay=true;
await article.querySelector('button').onclick();assert(article.textContent.includes('再生ボタンを押して'));rejectPlay=false;
failAudio=true;await article.querySelector('button').onclick();assert(article.textContent.includes('音声を作れませんでした。'));failAudio=false;
configureVoice({enabled:false});assert(sounds.at(-1).paused);
screen.replaceChildren();const form=document.createElement('form');screen.append(form);const draft=structuredClone(cfg);
const save=await voiceSettings(form,draft);
assert.equal([...form.querySelectorAll('label')].find(e=>e.firstChild.textContent==='試しに読む文').querySelector('input').value,previewText);
assert(form.textContent.includes('権利を持っている声だけを使ってください。'));
assert(form.textContent.includes('未取得'));
const selectLabel=title=>[...form.querySelectorAll('label')].find(e=>e.firstChild?.textContent===title).querySelector('input,select,textarea');
const input=(title,value)=>{const e=selectLabel(title);e.value=value;e.dispatchEvent(new dom.window.Event('input'));return e;};
const visible=e=>!e.closest('[hidden]');
assert(visible(form.querySelector('.voice-design')));assert(!visible(form.querySelector('.voice-clone')));
assert(!visible(selectLabel('外部の声の名前')));
input('音声のエンジン','external');assert(visible(selectLabel('外部の声の名前')));
input('外部エンジンの声の調子','instructions');assert.equal(draft.tts.styles.external.mode,'instructions');
const pendingTts=await save();assert.equal(pendingTts.engine,'speech.cpp');assert.equal(pendingTts.styles.external.mode,'none','General save preserves applied engine/style settings');
input('音声のエンジン','speech.cpp');assert(!visible(selectLabel('外部の声の名前')));
input('声の説明','静かな声');const ordinaryBefore=calls.length;await save();assert.equal(calls.length,ordinaryBefore,'General save must not apply the candidate');
await [...form.querySelectorAll('button')].find(e=>e.textContent==='人物の設定から声の説明を作り直す').onclick();assert.equal(selectLabel('声の説明').value,'新しい声の説明');
const drop=form.querySelector('.voice-drop');drop.ondrop({preventDefault(){},dataTransfer:{files:[new Blob(['wav'],{type:'audio/wav'})]}});await tick();
assert(form.querySelector('.voice-references').textContent.includes('追加.wav'));assert(form.querySelector('.voice-references').textContent.includes('既存.wav'));
input('声の作り方','clone');assert(visible(form.querySelector('.voice-clone')));assert(!visible(form.querySelector('.voice-design')));
const enabled=[...form.querySelectorAll('label')].find(e=>e.textContent==='音声を使う').querySelector('input');enabled.checked=true;enabled.onchange();
input('試しに読む文','こんばんは。');await [...form.querySelectorAll('.voice-expressions button')].find(e=>e.textContent==='悲しい').onclick();assert.equal(form.querySelector('.voice-expressions [aria-pressed=true]').textContent,'悲しい');
input('再生の音量','40');assert.equal(localStorage.getItem('cbs.voiceVolume'),'0.4');assert.equal(form.querySelector('audio').volume,.4);
const appliedBefore=structuredClone(voice),previewStart=calls.length;
await [...form.querySelectorAll('button')].find(e=>e.textContent==='声を試す').onclick();
assert(!calls.slice(previewStart).some(c=>c.route==='/api/settings'||c.route==='/api/voice'));
assert.deepEqual(voice,appliedBefore);assert.equal(candidate.voice.mode,'clone');
assert.deepEqual(calls.at(-1).body,{text:'こんばんは。',expression:'sad',candidate_id:'a'.repeat(32)});
assert.equal(sounds.at(-1).volume,.4);input('再生の音量','25');assert.equal(sounds.at(-1).volume,.25);
input('声の作り方','design');assert([...form.querySelectorAll('button')].some(e=>e.textContent==='この説明で声を作る'));
await [...form.querySelectorAll('button')].find(e=>e.textContent==='この説明で声を作る').onclick();
assert([...form.querySelectorAll('button')].some(e=>e.textContent==='作り直す'));assert.deepEqual(voice,appliedBefore);
input('声の説明','もう少し低い声');await [...form.querySelectorAll('button')].find(e=>e.textContent==='声を試す').onclick();assert.equal(candidate.voice.description,'もう少し低い声');assert.deepEqual(voice,appliedBefore);
input('声の作り方','clone');
stopVoice();streaming=true;configureVoice({...cfg.tts,enabled:true});const before=sounds.length;await article.querySelector('button').onclick();assert.equal(sounds.length,before+2,'Stream sentences in order');assert(sounds.slice(before).every(s=>s.volume===.25));streaming=false;
stopVoice();
// Draft voice preview moved from the unconfirmed review to OB3's saved voice editor.
// M10b: a second click confirms the quoted capacity, with progress/cancel/resume visible.
screen.replaceChildren(form);
const button=text=>[...form.querySelectorAll('button')].find(e=>e.textContent===text);
const beforeDownload=calls.filter(c=>c.route==='/api/voice/download'&&c.method==='POST').length;
await button('音声モデルを取得する').onclick();
assert.equal(calls.filter(c=>c.route==='/api/voice/download'&&c.method==='POST').length,beforeDownload);
assert(form.textContent.includes('1200.0 MB'));
await button('この容量で取得する').onclick();assert(form.textContent.includes('25%')&&form.textContent.includes('残り約1分'));
await button('取得を取り消す').onclick();assert(button('途中から再開する'));
const refBefore=voice.reference;await button('すべて外す').onclick();assert.equal(voice.reference,refBefore);
assert(form.textContent.includes('参照音声はまだありません'));
drop.ondrop({preventDefault(){},dataTransfer:{files:[new Blob(['wav'],{type:'audio/wav'}),new Blob(['wav'],{type:'audio/wav'})]}});await tick();
assert.equal(form.querySelectorAll('.voice-references audio').length,2);
assert.equal(voice.reference,refBefore,'Adding clips must leave the applied voice alone');
await button('声を試す').onclick();assert.equal(voice.reference,refBefore);assert.equal(candidate.voice.reference,'combined.gguf');
await button('この声で決める').onclick();assert.equal(voice.reference,'combined.gguf');assert(calls.some(c=>c.route==='/api/voice'&&c.body?.apply_references===true));
// First-run voice is optional and the editor waits for a ready model.
screen.replaceChildren();download={...download,status:'missing'};
const setupCfg=structuredClone(cfg);setupCfg.tts.enabled=false;
await voiceSettings(screen,setupCfg,{setup:true});
assert([...screen.querySelectorAll('label')].find(e=>e.textContent.startsWith('声の作り方')).parentElement.hidden);
const setupInput=(title,value)=>{const e=[...screen.querySelectorAll('label')].find(e=>e.firstChild?.textContent===title).querySelector('input,select,textarea');e.value=value;e.dispatchEvent(new dom.window.Event('input'));};
const setupToggle=[...screen.querySelectorAll('label')].find(e=>e.textContent==='音声を使う').querySelector('input');setupToggle.checked=true;setupToggle.onchange();
setupInput('音声のエンジン','external');assert(!screen.querySelector('.voice-editor').hidden,'External engine must be usable before local download');
setupInput('声の作り方','design');setupInput('声の説明','はじめの候補');
const setupStart=calls.length;await [...screen.querySelectorAll('button')].find(e=>e.textContent==='声を試す').onclick();
assert(calls.slice(setupStart).some(c=>c.route==='/api/setup/voice/preview'));assert(!calls.slice(setupStart).some(c=>c.route==='/api/settings'));
await [...screen.querySelectorAll('button')].find(e=>e.textContent==='この声で決める').onclick();
console.log('PASS M10b offline DOM: size confirmation, progress, cancel/resume, explicit reference list/add/remove/clear/apply, optional setup editor');
console.log('PASS M10 offline DOM: opt-in, history/manual/autoplay, late expression, stop, blocked audio, errors, design, WAV drop, settings/welcome preview');
stopVoice();
const reloaded=await import('../cbs/web/static/voice.js?volume-reload');reloaded.configureVoice({enabled:true});screen.replaceChildren();reloaded.voiceButton(screen,9);await screen.querySelector('button').onclick();assert.equal(sounds.at(-1).volume,.25);reloaded.stopVoice();
console.log('PASS M10c offline DOM: method/engine visibility, candidate preview/retry/sample/decision, setup external editor, device volume/live changes/reload/chat streams');
