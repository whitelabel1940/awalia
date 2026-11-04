import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const source=readFileSync(new URL('../cbs/web/static/type-labels.js',import.meta.url),'utf8');
function setup(query){
  const dom=new JSDOM('<body><p>本文</p><button>送る</button><span hidden>非表示</span></body>',{url:'https://unit.invalid/'+query,runScripts:'outside-only'}),w=dom.window;
  let mutations=0,resizes=0,queue=[];
  w.MutationObserver=class{constructor(fn){this.fn=fn;w.changed=fn;mutations++;}observe(){}disconnect(){}};
  w.ResizeObserver=class{constructor(){resizes++;}observe(){}};
  w.requestAnimationFrame=fn=>queue.push(fn);
  w.getComputedStyle=e=>e===w.document.documentElement?{getPropertyValue:key=>({'--type-body':'16px','--type-ui':'14.5px'}[key]||'')}:{fontSize:e.tagName==='BUTTON'?'14.5px':'16px',visibility:'visible',display:'block'};
  w.HTMLElement.prototype.getBoundingClientRect=()=>({left:20,top:30,bottom:50,width:100,height:20});
  w.eval(source);
  return {w,flush(){const q=queue;queue=[];q.forEach(fn=>fn());},counts:()=>[mutations,resizes]};
}
const off=setup('');assert.deepEqual(off.counts(),[0,0]);assert(!off.w.document.querySelector('#type-labels'));
const on=setup('?type-labels');on.flush();
assert.deepEqual([...on.w.document.querySelectorAll('.type-label')].map(e=>e.textContent),['本文','UI']);
assert.equal(on.w.document.querySelector('#type-labels').getAttribute('aria-hidden'),'true');
const p=on.w.document.createElement('p');p.textContent='追加';on.w.document.body.append(p);on.w.changed();on.flush();assert.equal(on.w.document.querySelectorAll('.type-label').length,3);
on.w.dispatchEvent(new on.w.Event('resize'));on.flush();assert.equal(on.w.document.querySelectorAll('.type-label').length,3);
console.log('UV1: flag off, tier labels, hidden text, dynamic text and resize PASS');
