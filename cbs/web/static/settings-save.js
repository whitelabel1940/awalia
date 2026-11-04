import {node,action,icon} from './ui.js';

// Each control owns its acknowledgement and retry value. Requests for that control
// are serialized so a slow earlier response cannot overwrite the last choice.
export function saveOnEdit(input,save,{read=()=>['checkbox','radio'].includes(input.type)?input.checked:input.value,immediate=['checkbox','radio','range'].includes(input.type)||input.tagName==='SELECT'}={}){
  let baseline=JSON.stringify(read()),pending,working=false,timer;
  const badge=node('span','','saved-badge'),label=input.closest('label');badge.setAttribute('role','status');label?.classList.add('save-field');label?.append(badge);
  async function flush(){
    if(working)return;working=true;
    while(pending){const item=pending;pending=null;if(item.signature===baseline)continue;clearTimeout(timer);
      try{if(await save(item.value)===false){badge.replaceChildren();continue;}baseline=item.signature;badge.className='saved-badge';badge.replaceChildren(icon('check'),node('span','保存しました'));timer=setTimeout(()=>badge.replaceChildren(),2000);}
      catch{badge.className='saved-badge warn';badge.replaceChildren(icon('warning'),node('span','保存できませんでした · '));action(badge,'もう一度',()=>{pending=item;return flush();}).classList.add('quiet');}
    }working=false;
  }
  const commit=()=>{if(!input.reportValidity())return;const value=structuredClone(read()),signature=JSON.stringify(value);if(signature===baseline&&!working)return;pending={value,signature};return flush();};
  input.addEventListener(immediate?'change':'blur',commit);
  if(!immediate)input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();void commit();}});
  return commit;
}
