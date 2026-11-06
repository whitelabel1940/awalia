import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<main id="screen"></main><p id="error" hidden></p>',{url:'http://127.0.0.1/wizard?look=1'});
for(const key of ['window','document','location','history','localStorage','sessionStorage'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
window.HTMLCanvasElement.prototype.getContext=()=>({clearRect(){},drawImage(){}});
window.HTMLCanvasElement.prototype.toDataURL=()=> 'data:image/png;base64,AA==';
window.HTMLCanvasElement.prototype.toBlob=fn=>fn(new Blob(['png'],{type:'image/png'}));
globalThis.createImageBitmap=async()=>({width:1024,height:1024});
const root=document.getElementById('screen'),calls=[];
const catalog=JSON.parse(execFileSync('.venv/Scripts/python.exe',['-c','import json;from cbs.look import field_catalog;print(json.dumps(field_catalog()))'],{encoding:'utf8'}));
const help=readFileSync(new URL('../onboarding/look_import_help.md',import.meta.url),'utf8');
let saved,description,describeError,finished;
function reset(meta={}){root.replaceChildren();saved={person:{},image:{},expressions:[],...meta};description={assignments:[{path:'person.hair.color',value:'銀色'},{path:'person.style.base',value:'水彩'}],filled:2};describeError=null;calls.length=0;finished=0;document.getElementById('error').textContent='';}
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=typeof options.body==='string'?JSON.parse(options.body):options.body;calls.push({route,body,method:options.method});let result;
  if(route==='/api/look')result=saved;
  else if(route==='/api/look/fields')result=catalog;
  else if(route==='/api/look/prompt')result={prompt:'指示文'};
  else if(route==='/api/look/describe'){if(describeError)return new Response(JSON.stringify({detail:describeError}),{status:409});result=description;}
  else if(route==='/api/look/meta'){saved={...saved,...structuredClone(body)};result=saved;}
  else if(route.startsWith('/api/look/')&&options.method==='PUT'){const slot=route.split('/').at(-1);if(slot==='cover')saved.cover=true;else saved.expressions=[...new Set([...saved.expressions,slot])];result=saved;}
  else if(route==='/api/onboarding/look_import_help')result={text:help};
  else if(route.startsWith('/api/onboarding/'))result={text:'# 見た目'};
  else if(route==='/api/look/draft')result={assignments:[],suggestions:[]};
  else if(route==='/api/partner')result={partner:{id:'fixture',name:'ユキ'}};
  else if(route==='/api/settings')result={tts:{enabled:false,engine:'speech.cpp'}};
  else if(route==='/api/voice')result={voice:{mode:'design'}};
  else throw Error('Unexpected offline endpoint '+route);
  return new Response(JSON.stringify(result));
};
const tick=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
const visible=e=>{for(let at=e;at&&at!==root;at=at.parentElement)if(at.hidden)return false;return true;};
async function click(text){const b=[...root.querySelectorAll('button')].find(b=>visible(b)&&b.textContent===text);assert(b,text);await b.onclick();await tick();assert.equal(document.getElementById('error').textContent,'');}
async function next(){await tick();const b=root.querySelector('.wizard-actions>.primary');assert(b&&!b.disabled);await b.onclick();await tick();assert.equal(document.getElementById('error').textContent,'');}
function chooseImport(){const input=root.querySelector('input[value=import]');assert(input);input.checked=true;input.onchange();}
const {onboardingFinishing}=await import('../cbs/web/static/onboarding-finishing.js');
const {newLook,lookFieldsEmpty,applyLookDescription}=await import('../cbs/web/static/look-editor.js');
const {face,selectPartner}=await import('../cbs/web/static/ui.js');selectPartner('fixture');

// Both the initial setup and the settings entry expose the same two routes.
for(const lookOnly of [false,true]){
  reset({person:{hair:{color:'黒'}},image:{use:'single'}});const flow=await onboardingFinishing(root,{name:'ユキ',lookOnly,onLookDone:()=>finished++});
  assert(root.textContent.includes('これから見た目を決めて、画像を作る'));assert(root.textContent.includes('もう画像がある'));
  chooseImport();assert([...root.querySelectorAll('button')].find(b=>b.textContent==='設定文と性格から下書きする').hidden);
  await next();assert.equal(root.querySelector('h2').textContent,'画像の取り込み・確認');
  assert(visible(root.querySelector('.drop')),'owned-image route accepts a sheet even after previously making a single face');
  assert(root.querySelector('.look-import-help strong').textContent==='取り込む画像の形');
  assert.equal(root.querySelector('.look-import-help li').textContent,help.split('\n')[1].slice(2));
  await click('戻る');assert(root.querySelector('input[value=import]').checked);await next();
  await next();assert.equal(root.querySelector('h2').textContent,'画像の取り込み');assert(visible(root.querySelector('.look-cover')));
  assert(!calls.some(c=>c.route==='/api/look/describe'));
  if(lookOnly){await next();assert.equal(finished,1);}else await flow.go('look');
}

// One selected image is neutral, can advance, and offers description B only while fields are empty.
reset();let state=newLook(saved),flow=await onboardingFinishing(root,{name:'ユキ',lookOnly:true,lookState:state,onLookDone:()=>finished++});
chooseImport();await next();
const separate=root.querySelector('input[type=file][multiple]');
// The separate picker is identified by its label; the sheet picker is deliberately single.
assert(separate);Object.defineProperty(separate,'files',{value:[{type:'image/png',size:100}],configurable:true});await separate.onchange();await tick();
assert(state.slots[0]&&state.slots.slice(1).every(s=>!s));
assert(root.textContent.includes('『ふつう』だけでも使えます。'));
await click('『ふつう』の画像を添付して表情シートを作る');assert.equal(state.image.from_reference,true);assert.equal(root.querySelector('h2').textContent,'表情シートを作る');
await click('戻る');await next();assert(root.textContent.includes('会話でもユキがこの見た目に沿って話せます。'));assert(saved.expressions.includes('neutral'));
const writes=calls.filter(c=>c.route==='/api/look/meta').length;
await click('画像から書き起こす');assert.equal(root.querySelector('h2').textContent,'見た目の確認');assert(root.querySelector('.onboarding-summary').textContent.includes('銀色'));
assert.equal(calls.filter(c=>c.route==='/api/look/meta').length,writes,'description is not saved');
const hairRow=[...root.querySelectorAll('.summary-row')].find(r=>r.textContent.includes('髪の色'));await hairRow.querySelector('button').onclick();await tick();
let input=root.querySelector('[data-look-path="person.hair.color"]');input.value='白銀';input.dispatchEvent(new window.Event('input'));await next();
assert.equal(root.querySelector('h2').textContent,'見た目の確認');assert(!saved.person.hair?.color);
await next();assert.equal(saved.person.hair.color,'白銀');assert.equal(root.querySelector('h2').textContent,'画像の取り込み');

// Declining B goes directly to the cover import. Expressions alone do not suppress B.
reset({expressions:['smile'],person:{expressions:{smile:'目を細める'}}});state=newLook(saved);
flow=await onboardingFinishing(root,{name:'ユキ',lookOnly:true,lookState:state});chooseImport();await next();
assert(root.textContent.includes('取り込んである最初の顔を代わりに表示'));await next();await click('今はしない');assert.equal(root.querySelector('h2').textContent,'画像の取り込み');

// C is visible only for saved faces and empty person fields; styles count, expression details do not.
const {settingsShelves}=await import('../cbs/web/static/settings-shelves.js');
for(const [meta,shown] of [[{expressions:['smile']},true],[{cover:true},false],[{expressions:['neutral'],person:{style:{base:'水彩'}}},false],[{expressions:['shy'],person:{expressions:{shy:'照れ'}}},true]]){
  reset(meta);history.replaceState(null,'','/#settings/look');await settingsShelves(root,'ユキ');
  assert.equal(root.textContent.includes('AI はまだユキの見た目を知りません。'),shown,root.textContent);
  assert.equal([...root.querySelectorAll('a')].some(a=>new URL(a.href).searchParams.has('describe')),shown);
}
reset({expressions:['smile']});history.replaceState(null,'','/wizard?look=1&describe=1');
const browserLocation=globalThis.location;globalThis.location={search:'?look=1&describe=1',hash:'',href:browserLocation.href};
const {editLook}=await import('../cbs/web/static/settings.js');
await editLook(root);assert.equal(root.querySelector('h2').textContent,'見た目の確認');assert.equal(saved.person.hair,undefined);await next();
assert.equal(saved.person.hair.color,'銀色');assert.equal(location.href,'/#settings/look');globalThis.location=browserLocation;

// An unavailable vision model stays in the flow with an actionable message and no writes.
reset({expressions:['neutral']});describeError='写真の読み取りを使う設定にすると使えます';
await onboardingFinishing(root,{name:'ユキ',lookOnly:true,describe:true});assert(root.querySelector('[role=alert]').textContent.includes(describeError));assert(!calls.some(c=>c.route==='/api/look/meta'));

// The client also protects hand edits/explicitly cleared fields, and face() orders fallback slots.
state=newLook({person:{hair:{color:'黒'}}});state.touched.add('person.hair.length');applyLookDescription(state,{assignments:[{path:'person.hair.color',value:'銀'},{path:'person.hair.length',value:'長髪'}]});assert.equal(state.person.hair.color,'黒');assert.equal(state.person.hair.length,undefined);
assert(lookFieldsEmpty({person:{expressions:{smile:'笑う'}}}));assert(!lookFieldsEmpty({person:{face:{habits:'穏やか'}}}));
assert(face('ユキ','sad',['shy','smile']).querySelector('img').src.includes('/smile?'));
assert(face('ユキ','sad',['shy','neutral']).querySelector('img').src.includes('/neutral?'));
assert(!face('ユキ','sad',[]).querySelector('img'));
const {partnerHeader}=await import('../cbs/web/static/note-header.js');root.replaceChildren();partnerHeader(root,{name:'ユキ',current_expression:'sad'},{expressions:['shy']});assert(root.querySelector('.note-portrait img').src.includes('/neutral?'));
console.log('PASS R11 DOM: shared route/back/import help, single neutral/guide/reference, B conditions/decline/edit/explicit save, C conditions/entry/return, disabled vision and face fallback');
