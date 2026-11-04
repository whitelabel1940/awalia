import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<main id="screen"></main><p id="error"></p>',{url:'http://unit.invalid/wizard'});
for(const k of ['window','document','location','localStorage','sessionStorage','history'])globalThis[k]=dom.window[k];
window.scrollTo=()=>{};
const {node,field,select,selectPartner}=await import('../cbs/web/static/ui.js');
const {questionnaireNamed}=await import('../cbs/web/static/questionnaire.js');
const root=document.getElementById('screen'),source=readFileSync(new URL('../cbs/web/static/wizard.js',import.meta.url),'utf8');
const questionSource=source.slice(source.indexOf('function question('),source.indexOf('\nfunction ',source.indexOf('function question(')+10));
for(const group of ['answers','adaptive_answers','life','now'])for(const multi of [false,true]){
 const data={basics:{name:'ユキ'},answers:{},adaptive_answers:{},life:{},now:{life:{}}},values=group==='now'?data.now.life:data[group];
 const question=new Function('data','node','field','select','questionnaireNamed','invalidateChoice','clearAdaptiveUnless','policy','report','contactChoices',questionSource+';return question;')(data,node,field,select,questionnaireNamed,()=>{},()=>{},()=>{},e=>{throw e;},()=>null);
 const q={id:'R1',scene:'場面',multi,choices:{A:'答える',unknown:'まだ分からない',leave_open:'任せる',B:'待つ'}};
 root.replaceChildren();let commit=question(root,q,group);
 assert.deepEqual([...root.querySelectorAll('input')].map(e=>e.value),['leave_open','A','B']);
 assert(root.querySelector('input').checked);assert.equal(root.querySelector('label span').textContent,'決めないでおく（話していくうちに分かる）');
 const choose=v=>{const e=root.querySelector(`input[value=${v}]`);e.checked=true;e.onchange();};
 choose('A');if(multi){choose('B');assert.equal(root.querySelectorAll('input:checked').length,2);}choose('leave_open');assert.equal(root.querySelectorAll('input:checked').length,1);commit();assert.deepEqual(values.R1.answer,['leave_open']);
 values.R1={answer:['unknown']};root.replaceChildren();commit=question(root,q,group);assert(root.querySelector('input[value=leave_open]').checked);commit();assert.deepEqual(values.R1.answer,['unknown']);
}
let persona={ai_question:'honest'},writes=[];
globalThis.fetch=async(url,options={})=>{const path=new URL(url,location.href).pathname,body=options.body?JSON.parse(options.body):null;let result;
 if(path.startsWith('/api/onboarding/'))result={text:readFileSync(new URL('../onboarding/05_scenes.md',import.meta.url),'utf8')};
 else if(path==='/api/persona'){if(body){writes.push(body);persona=body;}result=persona;}
 else if(path==='/api/settings')result={days:{}};
 else if(path==='/api/partner/requests')result={version:1,fields:{},lines:[],limits:{max_field_chars:100,max_lines:10,max_line_chars:100}};
 else if(path.endsWith('/augment'))result={questions:[]};else throw Error(path);
 return new Response(JSON.stringify(result));};
const {onboardingPartner}=await import('../cbs/web/static/onboarding-partner.js');
const data={basics:{name:'ユキ'},world:{preset:'現代日本'},persona:{ai_question:'immersive',open_style:'hinted'},now:{likes:[],life:{}},answers:{},life:{},adaptive_questions:[]};
const flow=await onboardingPartner(root,{data,setRender(){},questions:id=>id==='scenes'?[{id:'R1',group:'answers'}]:[],question:p=>p.append(node('p','最初の質問')),renderConfirmation:()=>new Map()});
const next=()=>root.querySelector('.wizard-actions .primary').onclick();
await flow.go('scenes');assert.equal(root.querySelector('h2').textContent,'場面の質問');assert(root.textContent.includes('ユキがいろいろな場面でどう反応するか'));assert(!root.textContent.includes('{name}'));
assert(root.textContent.includes('近いと感じる答えを選んでください。正解はありません。'));assert(root.textContent.includes('話していくうちにユキらしさが見えてきます。'));
await next();assert.equal(root.querySelector('h2').textContent,'決めなかった場面');assert.equal(root.querySelectorAll('input').length,2);assert(root.querySelector('input[value=hinted]').checked);assert(!root.querySelector('legend'));assert(!root.querySelector('.badge'));assert(root.textContent.includes('このあとの質問で「決めないでおく」にしたところで、ユキがどう振る舞うかを選びます。あとから設定で変えられます。'));
const free=root.querySelector('input[value=free]');free.checked=true;free.onchange();assert.equal(data.persona.open_style,'free');await next();assert(root.textContent.includes('最初の質問'));
await flow.go('confirmation');assert(root.querySelector('.summary-row').textContent.includes('性格に寄せすぎず、その場で決める'));await root.querySelector('.summary-row button').onclick();assert.equal(root.querySelector('h2').textContent,'決めなかった場面');await next();assert(root.querySelector('.summary-row'));
const {settingsShelves}=await import('../cbs/web/static/settings-shelves.js');selectPartner('fixture');history.replaceState(null,'','#settings/relationship');
const tick=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
for(const style of [null,'hinted','free']){
 persona={ai_question:'honest',...(style?{open_style:style}:{})};root.replaceChildren();await settingsShelves(root,'ユキ');
 const control=[...root.querySelectorAll('select')].find(e=>e.closest('label').textContent.includes('決めなかった場面'));assert(control);assert.equal(control.options.length,style?2:3);assert.equal(control.value,style||'legacy');
 if(!style)assert.equal(control.selectedOptions[0].textContent,'迎えた時に質問ごとに選んだとおり');
 control.value=style==='hinted'?'free':'hinted';control.dispatchEvent(new window.Event('change'));await tick();assert.equal(writes.at(-1).open_style,control.value);assert.equal(control.options.length,2);assert([...root.querySelectorAll('button')].find(b=>b.textContent==='話し方とお願いを保存').hidden);assert(!control.querySelector('option[value=legacy]'));
 if(!style)assert(root.textContent.includes('迎えた時に「任せる」を選んだ場面にはヒントがありません。'));
}
console.log('PASS OB8 DOM: all question routes/default/exclusion/legacy, explanation, screen order/default/no badge/legend, confirmation edit, immediate settings and legacy transition');process.exit(0);
