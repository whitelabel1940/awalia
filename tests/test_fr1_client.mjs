import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><html><body><main id="screen"></main></body></html>',{url:'http://127.0.0.1:18001/'});
for(const key of ['window','document','location','localStorage'])globalThis[key]=dom.window[key];
globalThis.fetch=()=>{throw Error('Trial controls must never request backups or exports');};
const {backupSettings}=await import('../cbs/web/static/backup.js');
for(const theme of ['light','dark']){
  document.documentElement.dataset.theme=theme;
  const parent=document.getElementById('screen');parent.replaceChildren();
  const section=backupSettings(parent,{backup_status:{trial:true}});
  assert(section.textContent.includes('お試しではバックアップを行いません。'));
  assert.equal(section.querySelectorAll('button,input,select,a').length,0);
}
const main=readFileSync(new URL('../desktop/src-tauri/src/main.rs',import.meta.url),'utf8');
const corner=readFileSync(new URL('../desktop/src-tauri/src/corner.rs',import.meta.url),'utf8');
assert(main.includes('if !trial {'));
for(const text of [main,corner]){
  assert(text.includes('.disable_drag_drop_handler()'));
  assert(text.includes('data_directory(state.options.data_dir.join("webview"))'));
  assert(text.includes('WEBVIEW_ARGS'));
}
console.log('PASS FR1 offline DOM: trial backup controls omitted, light/dark, native profile/drag-drop/browser-args guards');
