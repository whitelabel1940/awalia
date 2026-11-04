import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<nav id="menu"></nav><main><header class="head"></header><div id="screen"></div></main>',{url:'http://unit.invalid/#news'});
for(const k of ['window','document','location','localStorage','sessionStorage','history'])globalThis[k]=dom.window[k];
const {shell}=await import('../cbs/web/static/ui.js');
const {newsEntries,paintNewsBadges,newsScreen,refreshNews}=await import('../cbs/web/static/news.js');
let writes=[];
let data={current_version:'0.1.0',has_update:true,latest:{version:'0.2.0',notes:'<b>new</b>',url:'https://example.com/download'},feedback_url:'https://example.com/form',unread_count:2,
 notices:[{id:'old',date:'2026-11-01',title:'old',body:'old',links:[],read:true},{id:'new',date:'2026-11-04',title:'<img src=x>',body:'<b>hello</b>\nnext',links:[{label:'<b>link</b>',url:'https://example.com/details'},{label:'bad',url:'javascript:alert(1)'}],read:false}]};
globalThis.fetch=async(url,options={})=>{
 if(options.body){writes.push(JSON.parse(options.body));return new Response(JSON.stringify({...data,unread_count:0}));}
 return new Response(JSON.stringify(data));
};
shell('news','Partner');newsEntries('news');
assert.equal(document.querySelector('.news-menu').nextElementSibling.className,'partner');
assert.equal(document.querySelector('.head .news-mobile').getAttribute('aria-label'),'お知らせ');
for(const state of [{unread_count:0,has_update:false},{unread_count:2,has_update:false},{unread_count:0,has_update:true}]){
 paintNewsBadges(state);assert.equal(document.querySelectorAll('[data-news-link] .badge').length,state.unread_count||state.has_update?2:0);
}
const screen=document.getElementById('screen');
const mark=await newsScreen(screen);
assert.equal(writes.length,0);
assert.deepEqual([...screen.querySelectorAll('h3')].map(h=>h.firstChild.textContent),['新しい版 0.2.0','<img src=x>','old','フィードバックをお願いします']);
assert(screen.textContent.includes('<b>hello</b>\nnext'));assert(!screen.querySelector('b,img'));
assert.equal(screen.querySelectorAll('a').length,3);assert([...screen.querySelectorAll('a')].every(a=>a.target==='_blank'&&a.protocol==='https:'));
const normalFetch=globalThis.fetch;let deliver;
globalThis.fetch=(url,options={})=>options.body?normalFetch(url,options):new Promise(resolve=>{deliver=()=>resolve(new Response(JSON.stringify(data)));});
const pendingRefresh=refreshNews();
await mark();assert.deepEqual(writes,[{ids:['old','new']}]);
deliver();await pendingRefresh;globalThis.fetch=normalFetch;
assert.equal(document.querySelector('[data-news-link] .badge').textContent,'新','old GET must not undo the read badge');
assert.equal(document.querySelectorAll('[data-news-link] .badge').length,2); // update remains
data={...data,has_update:false,notices:[],unread_count:0};screen.replaceChildren();await (await newsScreen(screen))();
assert.equal(screen.children.length,1);assert.equal(screen.querySelector('h3').textContent,'フィードバックをお願いします');
assert.equal(document.querySelectorAll('[data-news-link] .badge').length,0);
data.feedback_url='';screen.replaceChildren();await newsScreen(screen);assert.equal(screen.children.length,0);
document.querySelector('.head').hidden=true;const band=document.createElement('header');band.className='chat-band';document.querySelector('main').prepend(band);newsEntries('talk');
assert.equal(document.querySelectorAll('.news-mobile').length,1);assert(band.querySelector('.news-mobile'));
console.log('PASS R3 DOM: PC/mobile badges, header placement, order, feedback-only, text safety, HTTPS links, read on open');
