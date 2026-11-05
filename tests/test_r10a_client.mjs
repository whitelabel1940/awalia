import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<main id="screen"></main><p id="error" hidden></p>',{url:'http://unit.invalid/wizard'});
for(const key of ['window','document','location','localStorage','sessionStorage','history','navigator'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
window.scrollTo=()=>{};
const root=document.getElementById('screen'),guide='<!-- awalia-setup-helper 12 -->\r\n<script>alert(1)</script> & <img src=x>\n手引き';
let calls=0,copied,fail=false,expire;
globalThis.fetch=async path=>{assert.equal(path,'/api/onboarding/setup-helper');calls++;return new Response(JSON.stringify(fail?{detail:'offline'}:{text:guide,source:'bundled'}),{status:fail?500:200});};
Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{copied=text;}},configurable:true});
const realTimeout=globalThis.setTimeout;
globalThis.setTimeout=(callback,delay,...args)=>{assert.equal(delay,6000);expire=callback;return realTimeout(()=>{},0);};
const {sequence,setupHelper}=await import('../cbs/web/static/onboarding.js');
const screens=[
  {id:'welcome',render:()=>{}},
  {id:'question',chapter:'くわしく',render:body=>{body.innerHTML='<fieldset><legend>朝にしたいこと</legend></fieldset>'; }},
  {id:'heading',chapter:'声',render:body=>{body.innerHTML='<fieldset><legend>後回しの質問</legend></fieldset><h2>声を確かめる</h2><h2>次の見出し</h2>'; }},
  {id:'empty',chapter:'見た目',render:body=>body.append('見出しのない説明')},
  {id:'ai',chapter:'AI',render:body=>{body.innerHTML='<h2>AI について</h2>';setupHelper(body,{describe:false});}},
  {id:'done',render:()=>{}}
];
const flow=sequence(root,screens,{progressFor:s=>s.id==='question'?{label:'今のこと',current:2,total:5}:{current:1,total:3}});
const help=()=>root.querySelector('.chapter-caption button');
const success='コピーしました。ChatGPT や Claude など、普段使っている AI に貼って質問してみてください。';
const payload=line=>`【Awalia の初期設定について相談です】\n${line}\n\n<script>alert(1)</script> & <img src=x>\n手引き`;
function literalFallback(expected){const field=root.querySelector('textarea');assert(field?.readOnly);assert.equal(field.value,expected);assert(!root.querySelector('script,img'));assert(root.textContent.includes('全部選んでコピーしてください。'));}
async function close(){const button=[...root.querySelectorAll('button')].find(b=>b.textContent==='閉じる');assert(button);await button.onclick();assert(!root.querySelector('textarea'));assert(!root.textContent.includes('全部選んでコピーしてください。'));}

await flow.start('welcome');assert(!help());assert(!root.querySelector('.chapter-caption'));
await flow.go('done');assert(!help());
await flow.go('question');assert.equal(help().textContent,'AI に相談');assert.equal(calls,0);
await help().onclick();const question=payload('いま「今のこと」の 2 / 5 ページ、見出し「朝にしたいこと」の画面にいます。');assert.equal(copied,question);assert(!copied.includes('awalia-setup-helper'));
const status=root.querySelector('.setup-helper-status');assert.equal(status.getAttribute('role'),'status');assert.equal(status.textContent,success);assert(!status.hidden);expire();assert(status.hidden);assert.equal(status.textContent,'');
await flow.go('heading');await help().onclick();assert.equal(copied,payload('いま「声」の 1 / 3 ページ、見出し「声を確かめる」の画面にいます。'));
root.querySelector('h2').textContent='変更された見出し';await help().onclick();assert(copied.includes('見出し「変更された見出し」'));
await flow.go('empty');await help().onclick();assert.equal(copied,payload('いま「見た目」の 1 / 3 ページの画面にいます。'));
const noCount=sequence(root,screens,{showCount:false});
await noCount.start('heading');await help().onclick();assert.equal(copied,payload('いま「声」の見出し「声を確かめる」の画面にいます。'));
await noCount.go('empty');await help().onclick();assert.equal(copied,payload('いま「見た目」の画面にいます。'));
await flow.go('ai');await help().onclick();const aiCopy=copied;
const inline=[...root.querySelectorAll('button')].find(b=>b.textContent==='AI に手伝ってもらう');await inline.onclick();assert.equal(copied,aiCopy);assert.equal(copied,payload('いま「AI」の 1 / 3 ページ、見出し「AI について」の画面にいます。'));
// Both unavailable and denied clipboard paths keep the entire contextual text literal and closable.
await flow.go('question');
for(const clipboard of [undefined,{writeText:async()=>{throw Error('denied');}}]){
  Object.defineProperty(navigator,'clipboard',{value:clipboard,configurable:true});await help().onclick();literalFallback(question);await help().onclick();assert.equal(root.querySelectorAll('textarea').length,1);await close();
}
fail=true;await help().onclick();assert(root.textContent.includes('手引きを用意できませんでした。'));assert(!root.querySelector('textarea'));fail=false;
// Disabling the help controls does not depend on whether counts are displayed.
const settings=sequence(root,screens,{setupHelp:false,showCount:false});await settings.start('question');assert(!help());
globalThis.setTimeout=realTimeout;
console.log('PASS R10a DOM: caption help, cover/done exclusions, displayed chapter/count/h2/legend, omitted fields, marker removal, shared inline format, timed status, literal closable fallback, lazy fetch and errors');
