import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const mode=process.argv[2];
if(!mode){for(const variant of ['flow','more','skip','import','revise','no-conflicts','retry'])execFileSync(process.execPath,[fileURLToPath(import.meta.url),variant],{stdio:'inherit'});process.exit(0);}
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/wizard.html',import.meta.url),'utf8'),{url:'http://127.0.0.1/wizard'});
for(const k of ['window','document','location','history','localStorage','sessionStorage'])globalThis[k]=dom.window[k];
window.scrollTo=()=>{};
const root=document.getElementById('screen'),calls=[];
const q=(id,group,section)=>({id,group,section,domain:'R01',scene:`場面 ${id}`,choices:{leave_open:'あなたのパートナーに任せる',A:'話を聞く',unknown:'まだ分からない'}});
const questions=[q('L1now','now','now_life'),q('R1','answers','relationship'),q('R2','answers','relationship'),q('A1','answers','depth'),q('Q1','life','life')];
const conflict={setting:'world',topic:'世界',quote:'空を飛ぶ島',detail:'世界の設定を確かめてください。'};
const plan={questions,reader:{items:[],setting_conflicts:[conflict],voice:{}},coverage:{R01:'open'},confirmation_required:[],warnings:[]};
if(mode==='no-conflicts'){plan.reader.setting_conflicts=[];Object.assign(questions[0],{choices:{...questions[0].choices,B:'あなたと暮らしている'},contact_choices:{B2:['B'],default:['A']}});}
const cfg={ai_ready:true,connection_verified:true,models:{},simple:{},llm:{providers:{}},keys:{},onboarding:{actor_choices:{}},advanced_presets:[]};
const result={name:'ユキ',introduction:'紹介',open_domains:[],open_past:[],open_now:[],seed_memories:[],author_review:[]};
if(mode==='revise')result.author_review=[{ref:'review:1',supplemented:'暮らし',based_on:'世界の設定'}];
let failConfirm=true;
let failedCreate=false,failedRevise=false,planCalls=0,failedAdaptive=false;
let releaseAdaptive;
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body?JSON.parse(options.body):null;calls.push({route,body});let value;
  if(route==='/api/partner')value={partner:{id:'existing',name:'先輩'}};
  else if(route==='/api/settings')value=cfg;
  else if(route==='/api/onboarding/state')value={consent:{at:'test'}};
  else if(route.startsWith('/api/onboarding/'))value={text:`# ${route.split('/').at(-1)}\n\n{name}の説明`};
  else if(route==='/api/bootstrap/questionnaire')value={questionnaire:{common_choices:{leave_open:'あなたのパートナーに任せる'},sections:[{id:'now_life',questions:[questions[0]]}]}};
  else if(route==='/api/bootstrap/plan'){planCalls++;if(mode==='stop'&&[1,4].includes(planCalls)){value={id:'failed',status:'failed',stage:'reading'};}else value={id:'plan',status:'done',result:plan};}
  else if(route==='/api/bootstrap/read')value={id:'read',status:'done',result:{...plan,reader:{...plan.reader,profile:{name:{value:'読んだ名前'},world:{region:{value:'異世界'}}},voice:{first_person:{value:'ぼく'}}}}};
  else if(route==='/api/bootstrap/adaptive'&&mode==='stop'&&!failedAdaptive){failedAdaptive=true;value={id:'failed',status:'failed',stage:'questions'};}
  else if(route==='/api/bootstrap/adaptive'){if(mode==='skip')await new Promise(r=>releaseAdaptive=r);value={id:'adaptive',status:'done',result:{questions:['more','skip'].includes(mode)?[{...q('AQ1','adaptive_answers','more'),parent_question:'author:R1'}]:[],parents:{}}};}
  else if(route==='/api/bootstrap/create'||route.endsWith('/revise')){
    if(mode==='retry'&&route==='/api/bootstrap/create'&&!failedCreate){failedCreate=true;value={id:'failed',status:'failed',stage:'building'};}
    else if(mode==='revise'&&route.endsWith('/revise')&&!failedRevise){failedRevise=true;value={id:'failed',status:'failed',stage:'building'};}
    else value={id:'draft',status:'done',result:route.endsWith('/revise')?{...result,author_review:[]}:result};
  }
  else if(route.endsWith('/about/review'))value={token:'about-token',candidates:[{id:'0',destination:'fact',destination_label:'あなたのこと',content:'紅茶が好き',quote:'紅茶が好き',private:false,existing_facts:[]}]};
  else if(route.startsWith('/api/bootstrap/import/'))value=[];
  else if(route.endsWith('/confirm')){if(failConfirm){failConfirm=false;return new Response(JSON.stringify({detail:'追加の保存が途中です'}),{status:502});}value={id:'created'};}
  else throw Error('Unexpected offline endpoint '+route);
  return new Response(JSON.stringify(value));
};
const tick=async()=>{for(let i=0;i<30;i++)await new Promise(r=>setImmediate(r));};
async function click(label){const b=[...root.querySelectorAll('button')].find(e=>e.textContent===label);assert(b,`Missing ${label}: ${root.textContent}`);await b.onclick();await tick();}
async function next(){const b=root.querySelector('.wizard-actions>.primary');assert(b,root.textContent);await b.onclick();await tick();}
function input(label,value){const l=[...root.querySelectorAll('label.f')].find(e=>e.firstChild.textContent===label||e.querySelector('[aria-label]')?.getAttribute('aria-label')===label);assert(l,`Missing field ${label}`);const e=l.querySelector('input,textarea');e.value=value;e.dispatchEvent(new window.Event('input'));}
async function choose(value){const e=[...root.querySelectorAll('input[type=radio]')].find(e=>e.value===value);assert(e,`Missing radio ${value}`);e.checked=true;e.dispatchEvent(new window.Event('change'));await tick();}
async function heading(prefix){const b=[...root.querySelectorAll('button[aria-expanded]')].find(b=>b.textContent.startsWith(prefix));assert(b,prefix);await b.onclick();await tick();}
const last=route=>calls.filter(c=>c.route===route).at(-1);
await import('../cbs/web/static/wizard.js');await tick();
assert(root.textContent.includes('partner'));assert(!root.textContent.includes('声で入力'));assert(!calls.some(c=>c.route==='/api/settings/connection'));assert(document.getElementById('menu').hidden);
await next();assert(root.textContent.includes('持っているものはありますか？'));assert.equal(root.querySelectorAll('select').length,0);
// All four branches are cards; profile reading still feeds the existing fields.
assert.equal(root.querySelectorAll('input[type=radio]').length,4);
if(mode==='import'){
  await choose('log');await next();assert(root.querySelector('input[type=file]'));assert(root.textContent.includes('あなたについてのことや、お願い'));
  await click('会話ファイルなしで人物像づくりへ');assert(root.textContent.includes('取り込みが完了'));assert(!root.querySelector('.primary').disabled);await next();assert(root.textContent.includes('world'));await click('戻る');assert(root.querySelector('input[type=file]'));await click('戻る');assert(root.textContent.includes('持っているものはありますか？'));
  await choose('both');await next();input('設定文（任意）','名前はユキ');await next();assert(root.querySelector('input[type=file]'));assert(root.querySelector('textarea').value==='名前はユキ');await click('会話ファイルなしで人物像づくりへ');assert(root.textContent.includes('取り込みが完了'));assert(!root.querySelector('.primary').disabled);await next();assert(root.textContent.includes('world'));
  console.log('PASS OB2 import: log-only and both branches retain existing openImport and Reader');process.exit(0);
}
if(mode==='no-conflicts'){await next();assert(root.textContent.includes('world'));await next();assert(!last('/api/bootstrap/read'));}else {await choose('text');await next();input('設定文（任意）','名前はユキ。空を飛ぶ島。');await next();assert(last('/api/bootstrap/read'));await next();assert(root.textContent.includes('読んだ名前'));assert(root.textContent.includes('異世界'));}
input('名前','ユキ');await heading('世界観');
await choose('現代日本');assert(root.textContent.includes('現代日本では聞きません'));assert.equal(root.querySelectorAll('button[aria-expanded]:disabled').length,2);
await choose('現代の日本以外');assert.equal(root.querySelectorAll('button[aria-expanded]:disabled').length,1);
await choose('SF');assert.equal(root.querySelectorAll('button[aria-expanded]:disabled').length,0);
await choose('その他');input('どんな世界ですか','水に浮かぶ街');
await heading('年齢・性別');input('年齢（任意）','200歳');input('性別（任意）','未定');input('月','2');input('日','29');
await heading('返答の長さ');input('会話のルール（1行にひとつ）','短く話す\n絵文字を使わない');await next();input('あなたの名前','アキ');await heading('あなたの誕生日');input('月','11');input('日','1');input('年（任意）','2000');await heading('知っておいてほしいこと');input('知っておいてほしいこと','紅茶が好き');
await click('戻る');assert(root.textContent.includes('200歳'));assert(root.textContent.includes('水に浮かぶ街'));await next();assert(root.textContent.includes('紅茶が好き'));await next();
assert(root.textContent.includes('パートナーとあなたの確認'));assert(!root.textContent.includes('項目ごとに、どの内容で迎えるか'));assert(root.textContent.includes('決めていない項目は'));assert(!root.textContent.includes('指定なし'));assert(root.querySelector('.onboarding-summary'));
const ageRow=[...root.querySelectorAll('.summary-row')].find(p=>p.textContent.startsWith('年齢・性別・誕生日'));await ageRow.querySelector('button').onclick();await tick();assert(root.querySelector('input').value==='200歳','edit opens the selected accordion item');await next();await next();assert(root.textContent.includes('パートナーとあなたの確認'));
await next();if(mode==='stop'){assert(root.textContent.includes('読み取りで止まりました。下の「もう一度試す」から'),'a stopped read offers a retry in place');assert(root.textContent.includes('パートナーとあなたの確認'));await click('もう一度試す');}assert(root.textContent.includes('now'));assert(!root.textContent.includes('世界の設定を確かめてください'));
if(mode==='no-conflicts'){await next();assert(root.textContent.includes('好きなこと'));await click('好きなことを追加');input('好きなこと','読書');await next();assert(root.textContent.includes('場面 L1now'));await click('戻る');assert(root.querySelector('input').value==='読書');await next();assert(root.textContent.includes('場面 L1now'));
  // Living apart offers the default choices; living together swaps them and drops an answer that no longer fits.
  assert(root.textContent.includes('話を聞く'));assert(!root.textContent.includes('あなたと暮らしている'));await choose('A');
  await click('戻る');await click('戻る');await click('戻る');assert(root.textContent.includes('パートナーとあなたの確認'),root.textContent);const contactRow=[...root.querySelectorAll('.summary-row')].find(p=>p.textContent.startsWith('あなたとの接点'));await contactRow.querySelector('button').onclick();await tick();await choose('B2');await next();await next();assert(root.textContent.includes('パートナーとあなたの確認'),root.textContent);await next();await next();await next();
  assert(root.textContent.includes('場面 L1now'));assert(root.textContent.includes('あなたと暮らしている'));assert(!root.textContent.includes('話を聞く'));assert(!root.querySelector('input[value=A]'));
  assert(root.querySelector('input[value=leave_open]').checked,'the stale answer was dropped');
  await click('このまとまりの残りは決めないでおく');}else {await next();await click('好きなことを追加');await click('戻る');await click('飛ばす');}assert(root.textContent.includes('scenes'));await next();assert(root.textContent.includes('決めなかった場面'));await next();assert(root.textContent.includes('場面 R1'));assert.equal(root.querySelector('.chapter-caption').textContent,'場面の質問2 / 3AI に相談','the explanation is the first page of its part');await choose('A');await next();assert(root.textContent.includes('場面 R2'));await click('戻る');assert(root.querySelector('input[value=A]').checked);await next();
await click('このまとまりの残りは決めないでおく');assert(root.textContent.includes('depth'));await click('飛ばす');assert(root.textContent.includes('life'));await click('飛ばす');assert(root.textContent.includes('more'));
assert(last('/api/bootstrap/adaptive'),'adaptive starts on the explanation');
if(mode==='stop'){await tick();assert(root.textContent.includes('質問の生成で止まりました'),'stopped extra questions say so on the explanation');await click('もう一度試す');}
if(mode!=='skip'){await tick();assert(root.textContent.includes(mode==='more'?'追加の質問ができました':'追加で聞くことはありませんでした'),'the explanation says when the extra questions are ready');}
if(mode==='skip'){await click('飛ばす');releaseAdaptive();await tick();}else {await next();if(mode==='more'){assert(root.textContent.includes('場面 AQ1'));await choose('A');await click('戻る');assert(root.textContent.includes('more'));await click('飛ばす');}}
if(mode==='stop'){assert(root.textContent.includes('読み取りで止まりました'),'the checks screen offers a retry when its read stops');assert(root.querySelector('.wizard-actions>.primary').disabled);await click('もう一度試す');}
if(mode==='no-conflicts'){assert(!root.textContent.includes('確かめてほしいこと'));}else {assert(root.textContent.includes('確かめてほしいこと'));assert(root.textContent.includes('空を飛ぶ島'));const radio=root.querySelector('input[type=radio]');radio.checked=true;radio.dispatchEvent(new window.Event('change'));await next();}
assert(root.textContent.includes('review'));if(mode!=='retry'){assert(root.textContent.includes('まとめができました'));await next();}assert(last('/api/bootstrap/create'),'creation starts on review explanation');
const created=last('/api/bootstrap/create').body;assert.deepEqual(created.now.likes,mode==='no-conflicts'?[{topic:'読書',elaborate:'may_elaborate'}]:[],'an empty like row is not sent');assert.equal(created.basics.age,'200歳');assert.equal(created.basics.gender,'未定');assert.equal(created.world.preset,'水に浮かぶ街');assert.equal(created.world.reality,null);assert.deepEqual(created.answers.R1.answer,['A']);for(const a of [created.answers.R2,created.answers.A1,created.life.Q1,created.now.life.L1now])assert.deepEqual(a.answer,['leave_open']);
if(mode==='more')assert.deepEqual(created.adaptive_answers.AQ1.answer,['A']);if(mode==='skip')assert.deepEqual(created.adaptive_questions,[]);
if(mode==='retry'){assert(root.textContent.includes('人物像の生成'));assert.equal(calls.filter(c=>c.route==='/api/bootstrap/create').length,1,'no automatic retry after a failure');}
if(mode==='retry'){await click('もう一度まとめる');await next();}assert(root.textContent.includes('あなたのこと: 紅茶が好き'));assert(!root.querySelector('.face'));assert.equal(root.querySelectorAll('select').length,0);
if(mode==='revise'){await choose('supplement');input('足したいこと','暮らしを補足');assert.equal(root.querySelector('.wizard-actions>.primary').textContent,'補足を反映して確認する');await next();assert(calls.some(c=>c.route.endsWith('/revise')));assert.equal(last('/api/bootstrap/jobs/draft/revise').body.review_notes['review:1'],'暮らしを補足');assert(root.textContent.includes('人物像の生成'));assert.equal(calls.filter(c=>c.route.endsWith('/revise')).length,1);await click('もう一度まとめる');assert(root.textContent.includes('まとめができました'));await next();assert.equal(root.querySelector('.wizard-actions>.primary').textContent,'この内容で迎える');assert.equal(calls.filter(c=>c.route.endsWith('/revise')).length,2);}
await next();assert(root.textContent.includes('追加の保存が途中です。下の「もう一度試す」から'),'a failed save offers a retry in place');assert(!root.textContent.includes('AI の設定を見直す'));assert(root.textContent.includes('あなたのこと: 紅茶が好き'));await click('もう一度試す');assert(root.textContent.includes('look'));
const confirms=calls.filter(c=>c.route.endsWith('/confirm'));assert.equal(confirms.length,2);assert.deepEqual(confirms[0].body,confirms[1].body);assert.equal(confirms[0].body.user_name,'アキ');assert.equal(confirms[0].body.days[0].kind,'partner_birthday');assert.deepEqual(confirms[0].body.about_keys,['0']);assert.equal(calls.filter(c=>c.route==='/api/bootstrap/create').length,mode==='retry'?2:1);
assert.equal(confirms[0].body.days[1].kind,'user_birthday');assert.equal(confirms[0].body.days[1].year,2000);assert.deepEqual(confirms[0].body.rules,['短く話す','絵文字を使わない']);
assert(!document.getElementById('error').textContent.includes('Unexpected'));
console.log(`PASS OB2 DOM ${mode}: reused Reader/questions/checks/confirmation, world branches, free text, author fields, skip defaults, retained answers, final-only conflicts, background generation, classified facts, confirm retry, second partner`);
