import {node,action,report} from './ui.js';

let current=null,asking=null;
const values=root=>JSON.stringify({controls:[...root.querySelectorAll('input,textarea,select')]
  .filter(e=>!e.dataset.transient)
  .map(e=>[e.type,e.type==='file'?[...e.files].map(f=>[f.name,f.size,f.lastModified]):e.type==='checkbox'||e.type==='radio'?e.checked:e.value])
  // A form that only opened (empty fields) is not a change; clearing a filled field still is.
  .filter(([,v])=>!(v===''||v===false||(Array.isArray(v)&&!v.length))),
  pending:[root,...root.querySelectorAll('section')].filter(e=>e.settingsValue).map(e=>e.settingsValue())});

export function settingsNavigation(){
  const pages=new Map();let active;
  const controller={hash:location.hash,
    register(key,root,save,snapshot=()=>values(root)){const page={root,save,snapshot,baseline:snapshot()};pages.set(key,page);return ()=>page.baseline=snapshot();},
    activate(key){active=key;},
    dirty(){const p=pages.get(active);return !!p&&p.snapshot()!==p.baseline;},
    async leave(){
      if(!controller.dirty())return true;
      if(asking)return asking;
      asking=new Promise(resolve=>{
        const dialog=node('dialog'),buttons=node('div',undefined,'row'),status=node('p','','status');
        function close(value){dialog.close();dialog.remove();asking=null;resolve(value);}
        action(buttons,'保存して移る',async()=>{try{const p=pages.get(active);if(await p.save()===false)return;p.baseline=p.snapshot();close(true);}catch(e){status.textContent=e.message;}},true);
        action(buttons,'捨てて移る',()=>{const p=pages.get(active);p.baseline=p.snapshot();close(true);});action(buttons,'とどまる',()=>close(false));
        dialog.append(node('p','保存していない変更があります'),buttons,status);
        dialog.oncancel=e=>{e.preventDefault();close(false);};document.body.append(dialog);dialog.showModal();
      });return asking;
    }
  };current=controller;return controller;
}
export async function leaveSettings(){return current?current.leave():true;}
export function settingsHash(){return current?.hash;}
export function clearSettingsNavigation(){current=null;}
window.addEventListener('beforeunload',event=>{if(current?.dirty()){event.preventDefault();event.returnValue='';}});
// Hash changes are guarded by view(); full-page links need the same three choices.
document.addEventListener('click',event=>{
  const a=event.target.closest?.('a');if(!a||event.defaultPrevented||a.target==='_blank'||a.download||!current?.dirty())return;
  const url=new URL(a.href,location.href);
  if(url.origin===location.origin&&url.pathname===location.pathname&&url.search===location.search&&url.hash)return;
  event.preventDefault();void leaveSettings().then(ok=>{if(ok){clearSettingsNavigation();location.assign(url.href);}}).catch(report);
});
