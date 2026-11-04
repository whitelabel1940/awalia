import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><html><body><p id="error" hidden></p><main id="screen"></main><input id="message"><button id="send">送る</button></body></html>',{url:'https://unit.invalid/#settings',runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
let downloads=0,phase='running',fail=false;
dom.window.HTMLAnchorElement.prototype.click=function(){downloads++;assert(this.href.endsWith('/api/backup/exports/job1/download'));};
const calls=[],response=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(path,options={})=>{
  const body=options.body?JSON.parse(options.body):undefined;calls.push({path,body,method:options.method});
  if(path==='/api/backup/exports')return response({id:'job1'});
  if(path==='/api/backup/exports/job1')return response({id:'job1',status:phase,filename:'backup.awalia',error:fail?'容量を確認してください。':undefined});
  if(path==='/api/backup/settings'){
    if(body.folder==='missing')return new Response(JSON.stringify({detail:'フォルダを確認してください。'}),{status:422});
    return response({config:body,auto_success:'2026-10-20T12:00:00+09:00'});
  }
  throw Error('Unexpected offline endpoint');
};
const {backupSettings}=await import('../cbs/web/static/backup.js');
const cfg={backup:{folder:'C:\\sync',frequency:'off',daily_keep:7,weekly_keep:4,export_keep:4},backup_status:{jobs:[],scheduled_password_available:false}};
const parent=document.getElementById('screen');
const section=backupSettings(parent,cfg);
assert(section.textContent.includes('記録とパートナーのバックアップ'));
assert(section.textContent.includes('まだありません'));
assert(section.textContent.includes('忘れると元に戻せません'));
assert(section.textContent.includes('自動バックアップはパスワードなしのみ'));
assert(section.textContent.includes('OneDrive や Google ドライブ'));
const password=document.getElementById('backup-password');
assert.equal(password.type,'password');assert.equal(password.autocomplete,'new-password');
assert.equal(password.getAttribute('aria-describedby'),'backup-password-warning');
assert.deepEqual([...document.getElementById('backup-frequency').options].map(o=>o.value),['off','daily','weekly']);
const click=async text=>{await [...section.querySelectorAll('button')].find(b=>b.textContent===text).onclick();};
document.getElementById('backup-folder').value='missing';
await click('バックアップの設定を保存');assert(section.textContent.includes('フォルダを確認してください'));
document.getElementById('backup-folder').value='C:\\sync';document.getElementById('backup-frequency').value='weekly';
await click('バックアップの設定を保存');assert.equal(cfg.backup.frequency,'weekly');assert(section.textContent.includes('保存しました'));
assert(!('password' in calls.find(c=>c.path==='/api/backup/settings').body));
password.value='unit test password';
const exporting=click('今すぐバックアップする');
await new Promise(r=>setImmediate(r));
assert.equal(password.value,'');assert(document.getElementById('backup-export').disabled);
assert(!document.getElementById('message').disabled);assert(!document.getElementById('send').disabled);
assert(document.body.textContent.includes('会話はそのまま使えます'));
// Moving back to the conversation must not hide the eventual download notice.
parent.replaceChildren();
const reopened=backupSettings(parent,{...cfg,backup_status:{...cfg.backup_status,jobs:[{id:'job1',kind:'manual',status:'running'}]}});
assert(reopened.querySelector('#backup-export').disabled);
phase='completed';await exporting;await new Promise(r=>setImmediate(r));
assert(!reopened.querySelector('#backup-export').disabled);
assert.equal(downloads,1);assert(document.querySelector('.backup-notice a'));
assert(document.body.textContent.includes('バックアップができました'));
await document.querySelector('.backup-notice button').onclick();assert(!document.querySelector('.backup-notice'));
for(const theme of ['light','dark']){
  document.documentElement.dataset.theme=theme;window.innerWidth=375;
  parent.replaceChildren();const next=backupSettings(parent,cfg);
  assert.equal(next.querySelectorAll('input[type=password]').length,1);
  assert(next.querySelector('select'));assert(!next.textContent.includes('テスト係'));
}
const css=readFileSync(new URL('../cbs/web/static/screens.css',import.meta.url),'utf8');
assert(css.includes('#backup-settings input'));assert(css.includes('calc(100vw - 2rem)'));assert(css.includes('color:var(--fg)'));
assert(readFileSync(new URL('../cbs/web/static/settings.js',import.meta.url),'utf8').includes('backupSettings(panels.other,cfg,{managed:true})'));
parent.replaceChildren();phase='failed';fail=true;
const failed=backupSettings(parent,cfg);await [...failed.querySelectorAll('button')].find(b=>b.id==='backup-export').onclick();
assert.equal(downloads,1);assert(document.body.textContent.includes('容量を確認してください'));
console.log('PASS BK1 offline DOM: settings, frequency/folder, validation, optional password warning/clear, background conversation/navigation, download, failure, light/dark and phone controls');
