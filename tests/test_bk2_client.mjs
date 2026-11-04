import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><html><body><p id="error" hidden></p><main id="screen"></main></body></html>',{url:'https://unit.invalid/#settings',runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
let mode='password',staged=false,notice=true;const calls=[];
const response=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(path,options={})=>{
  calls.push({path,options});
  if(path==='/api/restore')return response({staged,max_bytes:1024*1024,trial:false});
  if(path==='/api/restore/preview'){
    assert(!path.includes('password'));assert(options.body.size===100);
    const value=JSON.parse(decodeURIComponent(options.headers['X-Awalia-Restore-Password']));
    if(mode==='password')return response({password_required:true});
    if(mode==='error')return response({detail:'パスワード・ファイルを確認してください。'},422);
    assert.equal(value,'日本語パスワード');
    return response({id:'a'.repeat(32),created_at:'2026-10-21T12:00:00+09:00',partners:[{name:'<script>人物</script>'}],turns:12,bytes:2000});
  }
  if(path.endsWith('/stage')){staged=true;return response({status:'staged'},202);}
  if(path.startsWith('/api/restore/previews/'))return response({status:'cancelled'});
  if(path==='/api/restore/notice'){const created_at=notice?'2026-10-21T12:00:00+09:00':null;notice=false;return response({created_at});}
  throw Error(`Unexpected endpoint ${path}`);
};
const {restoreEntry,showRestoreNotice}=await import('../cbs/web/static/restore.js');
const parent=document.getElementById('screen');
const click=async(section,text)=>{const button=[...section.querySelectorAll('button')].find(b=>b.textContent===text);assert(button,text);await button.onclick();};
for(const theme of ['light','dark']){
  document.documentElement.dataset.theme=theme;window.innerWidth=375;parent.replaceChildren();
  const section=restoreEntry(parent,{welcome:theme==='dark'});
  assert(section.textContent.includes(theme==='dark'?'バックアップから始める':'バックアップから元に戻す'));
  await section.querySelector('button').onclick();
  assert(!section.textContent.includes('テスト係'));assert(section.querySelector('input[type=file]'));
}
const section=parent.firstChild;
await click(section,'中身を確かめる');assert(section.textContent.includes('ファイルを選んでください'));
const file=section.querySelector('#restore-file');
Object.defineProperty(file,'files',{configurable:true,value:[{size:1024*1024+1}]});await file.onchange();
await click(section,'中身を確かめる');assert(section.textContent.includes('大きすぎ'));
Object.defineProperty(file,'files',{configurable:true,value:[{size:100}]});await file.onchange();
await click(section,'中身を確かめる');
const password=section.querySelector('#restore-password');assert(!password.parentElement.hidden);assert.equal(password.type,'password');
mode='error';password.value='wrong';await click(section,'中身を確かめる');assert.equal(password.value,'');assert(section.textContent.includes('パスワード・ファイル'));
mode='ok';password.value='日本語パスワード';await click(section,'中身を確かめる');
assert.equal(password.value,'');assert(section.textContent.includes('今の記録はバックアップしてから入れ替えます'));
assert(section.textContent.includes('会話 約12件'));assert(section.textContent.includes('<script>人物</script>'));assert(!section.querySelector('script'));
await click(section,'やめる');assert(section.textContent.includes('やめました'));assert(calls.some(c=>c.options.method==='DELETE'));
password.value='日本語パスワード';await click(section,'中身を確かめる');await click(section,'戻す');
assert(staged);assert(section.textContent.includes('次の起動で記録を入れ替えます'));assert(!section.querySelector('input[type=password]'));
let restarted=0;window.__TAURI__={core:{invoke:async command=>{assert.equal(command,'resident_restore_restart');restarted++;}}};
parent.replaceChildren();const resident=restoreEntry(section);await resident.querySelector('button').onclick();
assert.equal(restarted,1);assert(resident.textContent.includes('再起動しています'));
await showRestoreNotice();await showRestoreNotice();assert.equal(document.querySelectorAll('.backup-notice').length,1);
assert(document.body.textContent.includes('2026年10月21日にバックアップした記録から戻しました'));
const read=name=>readFileSync(new URL(name,import.meta.url),'utf8');
assert(read('../cbs/web/static/wizard.js').includes('onWelcome:welcomeRestore'));
assert(read('../cbs/web/static/backup.js').includes('restoreEntry(section,'));
assert(read('../cbs/web/static/screens.css').includes('.restore-settings input{box-sizing:border-box;max-width:100%'));
const rust=read('../desktop/src-tauri/src/main.rs'),config=JSON.parse(read('../desktop/src-tauri/tauri.conf.json'));
assert(rust.includes('restore_restart.swap(false'));assert(rust.includes('backend-restore-restarting'));
assert(config.app.security.capabilities[0].permissions.includes('allow-resident-restore-restart'));
assert(read('../desktop/src-tauri/build.rs').includes('"resident_restore_restart"'));
assert(rust.includes('.additional_browser_args(WEBVIEW_ARGS)'));assert(rust.includes('.disable_drag_drop_handler()'));
console.log('PASS BK2 DOM: both entries, bounded file, password retries/clear, escaped confirmation, cancel, staging, browser/resident restart, one-time notice, light/dark/375px and IPC declarations');
