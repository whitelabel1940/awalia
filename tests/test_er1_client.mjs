import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM(readFileSync(new URL('../cbs/web/static/index.html',import.meta.url),'utf8'),{url:'https://unit.invalid/#talk',runScripts:'outside-only',pretendToBeVisual:true});
for(const key of ['window','document','location','localStorage','history'])globalThis[key]=dom.window[key];
window.scrollTo=()=>{};
const polls=[];globalThis.setInterval=fn=>{polls.push(fn);return {unref(){}};};
const tick=async()=>{for(let i=0;i<15;i++)await new Promise(r=>setImmediate(r));};
const response=(x,status=200)=>new Response(JSON.stringify(x),{status,headers:{'Content-Type':'application/json'}});
const $=id=>document.getElementById(id);
const {api,report,apiError}=await import('../cbs/web/static/ui.js');
const {showFailures,refreshFailures}=await import('../cbs/web/static/failures.js');
const limit={kind:'limit',provider:'openrouter',status_code:403,message:'API キーに設定した利用上限に達しています。キーの設定で上限を上げるか外してください。',link:{url:'https://openrouter.ai/settings/keys',label:'キーの設定を開く'}};
report(apiError(limit));assert.equal($('error').querySelector('a').href,limit.link.url);assert($('error').textContent.includes('上限'));assert.equal($('error').querySelector('a').rel,'noopener noreferrer');
report(apiError({...limit,message:'<img src=x onerror=alert(1)>',link:{url:'javascript:alert(1)',label:'危険'}}));assert(!$('error').querySelector('img'));assert(!$('error').querySelector('a'));assert($('error').textContent.includes('<img'));
showFailures([{...limit,message:'ソラが話せない状態です: '+limit.message}]);assert(!$('llm-failures').hidden);assert($('llm-failures').textContent.includes('ソラ'));
showFailures([]);assert($('llm-failures').hidden);assert.equal($('llm-failures').children.length,0);

let notices=[],turns=[],mode='sse',submitted=[];
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0];
  if(route==='/api/partner')return response({partner:{id:'sora',name:'ソラ'}});
  if(route==='/api/settings')return response({ai_ready:true,connection_verified:true,tts:{enabled:false},autonomy:{opening:'none'}});
  if(route==='/api/look')return response({expressions:['neutral','thinking']});
  if(route==='/api/llm/failures')return response({notices});
  if(route==='/api/sessions')return response(options.method?{id:'s'}:[{id:'s'}]);
  if(route==='/api/sessions/s/turns')return response(turns);
  if(route==='/api/proactive/opening')return response({speak:false});
  if(route.endsWith('/messages/stream')){
    const text=JSON.parse(options.body).content;submitted.push(text);
    turns.push({id:turns.length+1,user_text:text,created_at:'2026-10-22T12:00:00+09:00',status:'failed'});
    notices=[{...limit,message:'ソラが話せない状態です: '+limit.message}];
    if(mode==='http')return response({detail:limit.message,...limit},502);
    return new Response(`event: error\ndata: ${JSON.stringify(limit)}\n\n`);
  }
  if(route==='/test-error')return response({detail:limit.message,...limit},422);
  throw Error('Unexpected offline request: '+route);
};
await assert.rejects(api('/test-error'),error=>error.kind==='limit'&&error.link.url===limit.link.url);
await import('../cbs/web/static/user.js');await tick();
assert($('llm-failures').hidden);
function send(text){$('message').value=text;$('composer').dispatchEvent(new window.Event('submit',{cancelable:true}));}
send('SSE でも失われない文');await tick();
assert.equal($('message').value,'SSE でも失われない文');assert(!$('send').disabled);assert($('error').textContent.includes('上限'));assert.equal($('error').querySelector('a').href,limit.link.url);assert(!$('llm-failures').hidden);assert(document.querySelector('.mine').textContent.includes('失われない文'));assert(!document.querySelector('.thinking'));
mode='http';send('HTTP でも失われない文');await tick();
assert.equal($('message').value,'HTTP でも失われない文');assert.equal($('error').querySelector('a').href,limit.link.url);assert.equal(submitted.length,2);
// Backend state drives the banner, including failures occurring while no chat request runs.
notices=[];await refreshFailures();assert($('llm-failures').hidden);
notices=[{...limit,message:'ソラが話せない状態です: '+limit.message}];
await Promise.all(polls.map(fn=>fn()));await tick();assert(!$('llm-failures').hidden);
location.hash='#settings';showFailures(notices);assert($('llm-failures').hidden);
location.hash='#talk';notices=[];await refreshFailures();assert($('llm-failures').hidden);
// Nothing about failure state is persisted in browser storage.
assert(!Object.keys(localStorage).some(key=>/failure|banner/.test(key)));
console.log('PASS ER1 DOM: structured HTTP/SSE reasons, safe fixed links, backend banner polling/recovery, preserved text and restored input');
dom.window.close();
