import {api, scoped, node, report} from './ui.js';

export function photoHistory(parent, photos=[], onDelete=()=>{}) {
  if(!photos.length)return;
  const gallery=node('div',undefined,'photo-gallery');
  for(const photo of photos){
    const item=node('figure'),image=node('img');image.src=scoped(`/api/photos/${photo.id}`);image.alt='見せた写真';image.loading='lazy';image.width=photo.width;image.height=photo.height;
    const details=node('details');details.append(node('summary','写真の説明'),node('p',photo.description));
    const remove=node('button','写真と説明を削除','btn');remove.type='button';remove.onclick=async()=>{
      // This action is explicit and also forgets description-derived memory.
      remove.disabled=true;
      try{await api(`/api/photos/${photo.id}`,{},'DELETE');item.remove();await onDelete(photo.id);}catch(error){remove.disabled=false;report(error);}
    };
    item.append(image,details,remove);gallery.append(item);
  }
  if(parent.querySelector('.message-meta'))parent.querySelector('.text').before(gallery);else parent.append(gallery);
}

// Shown in your bubble right after sending, from the local copies, until the saved photos arrive.
export function sendingPhotos(parent, taken=[]) {
  if(!taken.length)return null;
  const gallery=node('div',undefined,'photo-gallery photo-sending');
  for(const file of taken){const item=node('figure'),image=node('img');image.src=file.preview;image.alt='見せた写真';item.append(image);gallery.append(item);}
  if(parent.querySelector('.message-meta'))parent.querySelector('.text').before(gallery);else parent.append(gallery);return gallery;
}

export function photoComposer({composer,input,button,picker,previews,status,onError,active=()=>true}) {
  let cfg={enabled:false,reason:'写真の設定を確認しています。'},files=[],busy=false,notice=false;
  function render(){
    button.disabled=busy||!cfg.enabled;button.title=cfg.reason||'写真を見せる';
    status.textContent=cfg.enabled?(notice&&!cfg.notice_seen?'写真は画像を読むモデルの提供元に送られます':''):cfg.reason;
    previews.replaceChildren();
    for(const file of files){const item=node('span',undefined,'photo-pending'),image=node('img');image.src=file.preview;image.alt='送る写真';const remove=node('button','外す','btn');remove.type='button';remove.disabled=busy;remove.onclick=()=>{URL.revokeObjectURL(file.preview);files=files.filter(f=>f!==file);render();};item.append(image,remove);previews.append(item);}
    input.required=!files.length;
  }
  async function add(incoming){
    if(!active()||busy)return;
    if(!cfg.enabled){onError(new Error(cfg.reason));return;}
    const images=[...incoming].filter(f=>f.type.startsWith('image/'));
    if(!images.length)return;
    if(files.length+images.length>cfg.max_count){onError(new Error(`写真は一度に${cfg.max_count}枚までです。`));return;}
    if(images.some(f=>!['image/png','image/jpeg','image/webp'].includes(f.type)||f.size>cfg.max_bytes)){onError(new Error('容量の上限内の PNG・JPEG・WebP を選んでください。'));return;}
    files.push(...images.map(file=>({file,preview:URL.createObjectURL(file)})));render();
    if(!cfg.notice_seen&&!notice){notice=true;status.textContent='写真は画像を読むモデルの提供元に送られます';}
  }
  button.onclick=()=>picker.click();picker.onchange=()=>{void add(picker.files);picker.value='';};
  input.addEventListener('paste',e=>{const images=[...(e.clipboardData?.files||[])];if(images.length){e.preventDefault();void add(images);}});
  const drop=e=>{if(!active()||![...(e.dataTransfer?.files||[])].length)return;e.preventDefault();void add(e.dataTransfer.files);};
  window.addEventListener('drop',drop);window.addEventListener('dragover',e=>{if(active()&&[...(e.dataTransfer?.types||[])].includes('Files'))e.preventDefault();});
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{
    if(composer.offsetHeight)document.documentElement.style.setProperty('--composer-space',`${composer.offsetHeight+36}px`);
  }).observe(composer);
  render();
  return {
    configure(value){cfg=value||{enabled:false,reason:'写真を読むモデルが設定されていません。'};if(!cfg.enabled){for(const file of files)URL.revokeObjectURL(file.preview);files=[];}render();},
    setBusy(value){busy=value;render();},
    get count(){return files.length;},
    // Send-time hand-off: the input is emptied together with the text; restore() puts them back on failure.
    take(){const taken=[...files];files=[];render();return taken;},
    restore(taken){if(!taken?.length)return;files=[...taken,...files];render();},
    release(taken){for(const file of taken||[])URL.revokeObjectURL(file.preview);},
    async payload(selected=[...files]){
      const data=await Promise.all(selected.map(({file})=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('写真を読み込めませんでした。'));reader.readAsDataURL(file);})));
      if(data.length)cfg.notice_seen=true;
      return data;
    },
    clear(){for(const file of files)URL.revokeObjectURL(file.preview);files=[];render();},
  };
}
