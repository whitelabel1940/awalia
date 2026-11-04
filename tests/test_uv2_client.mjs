import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<nav id="menu"></nav><main id="screen"></main><p id="error"></p>',{url:'http://unit.invalid/'});
for(const key of ['window','document','location','localStorage','MutationObserver'])globalThis[key]=dom.window[key];
const {shell,action,tidyComponents,failureText}=await import('../cbs/web/static/ui.js');
const {accordion,notice}=await import('../cbs/web/static/onboarding.js');
const root=document.getElementById('screen');
shell();assert.equal(document.querySelectorAll('#menu a svg').length,3);
assert(!/[◌▤⚙]/u.test(document.getElementById('menu').textContent));
assert(!action(root,'副',()=>{}).classList.contains('primary'));
assert(action(root,'主',()=>{},true).classList.contains('primary'));
for(const [heading,label,tag]of [['設定の具体化','設定の具体化','legend'],['声のエンジン','声のエンジン','legend'],['返事を自動で読み上げる？','自動で読み上げる','legend'],['絵柄','絵柄','label']]){
  root.innerHTML=`<section><h2>${heading}</h2><fieldset>${tag==='label'?`<label class="f">${label}<input value="保存値"></label>`:`<legend>${label}</legend><input>`}</fieldset></section>`;
  tidyComponents(root);assert.equal(root.querySelector('.duplicate-heading').textContent,label);
  assert(!root.querySelector('.duplicate-heading').hidden);assert(root.querySelector('.duplicate-heading').classList.contains('sr-only'));
  if(tag==='label')assert.equal(root.querySelector('input').value,'保存値');
  root.querySelector('h2').textContent='別の見出し';tidyComponents(root);assert(!root.querySelector('.duplicate-heading'));
}
root.innerHTML='<details open><summary>設定の具体化</summary><fieldset><legend>設定の具体化</legend></fieldset></details>';
tidyComponents(root);assert(root.querySelector('summary svg'));assert(root.querySelector('legend.sr-only'));
root.replaceChildren();accordion(root,[{title:'話し終えた後',render:p=>{p.innerHTML='<fieldset><legend>話し終えた後</legend></fieldset>';}}]);
tidyComponents(root);assert(root.querySelector('.accordion-row svg'));assert(root.querySelector('legend.sr-only'));assert(!/[⌃⌄]/u.test(root.textContent));
const n=notice(root,'完了');assert(n.querySelector('svg'));assert.equal(n.textContent,'完了');n.update('失敗','error');assert(n.querySelector('svg'));assert.equal(n.getAttribute('role'),'alert');
failureText(document.getElementById('error'),new Error('失敗'));assert(document.querySelector('#error svg'));assert.equal(document.getElementById('error').firstChild.textContent,'失敗');
root.innerHTML='<section><h2>名前</h2><label class="f">名前<input></label></section><section><label class="f">名前<input></label></section>';
await new Promise(resolve=>setTimeout(resolve,50));assert.equal(root.querySelectorAll('.duplicate-heading').length,1);
root.querySelector('h2').textContent='変更';await new Promise(resolve=>setTimeout(resolve,50));assert.equal(root.querySelectorAll('.duplicate-heading').length,0);
const style=document.createElement('style');style.textContent=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');document.head.append(style);
document.body.classList.add('onboarding');root.innerHTML='<section class="card"><section class="card"><section class="card"><label class="choice">選択肢<input type="radio"></label></section><button class="btn">操作</button></section></section>';
for(const card of root.querySelectorAll('.card .card')){const css=window.getComputedStyle(card);assert.equal(css.backgroundColor,'rgba(0, 0, 0, 0)');assert.equal(css.borderRadius,'0px');assert.equal(css.boxShadow,'none');}
assert.equal(window.getComputedStyle(root.querySelector('.btn')).width,'auto');
assert.equal(window.getComputedStyle(root.querySelector('.btn')).borderRadius,'999px');
dom.window.close();console.log('UV2: icons, secondary default, duplicate labels, scopes and dynamic updates PASS');

