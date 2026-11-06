import {notice,renderExplanation} from './onboarding.js';
import {api, scoped, node, field, select, action, card, partnerId, icon} from './ui.js';
export const expressions = ['neutral','smile','shy','surprised','sad','troubled','thinking','pout'];
const labels = ['ふつう','笑顔','照れ','驚き','悲しい','困った','考え中','すねる'];
export function newLook(meta = {}) { return {version:2,person:structuredClone(meta.person||{}),image:structuredClone(meta.image||{}),features:meta.features||'',slots:Array(8).fill(null),cover:null,existingCover:Boolean(meta.cover),removedCover:false,removed:new Set(),existing:meta.expressions || [],touched:new Set(),initialized:new Set(),quotes:{},suggestions:[]}; }
export function drawCover(canvas,image){
  const scale=Math.min(1,1600/image.width);canvas.width=Math.round(image.width*scale);canvas.height=Math.round(image.height*scale);
  canvas.getContext('2d').drawImage(image,0,0,canvas.width,canvas.height);
}
const valueAt=(state,path)=>path.split('.').reduce((v,k)=>v?.[k],state);
function setAt(state,path,value){const keys=path.split('.');let target=state;for(const k of keys.slice(0,-1))target=target[k]??={};target[keys.at(-1)]=value;}
export function lookPayload(state){return {version:2,person:state.person,image:state.image,features:state.features};}
export function lookFieldsEmpty(state){
  const filled=value=>typeof value==='string'?Boolean(value.trim()):Object.entries(value||{}).some(([key,v])=>key!=='expressions'&&filled(v));
  return !filled(state.person);
}
export function applyLookDescription(state,result){
  for(const item of result.assignments){if(state.touched.has(item.path)||valueAt(state,item.path)?.trim())continue;setAt(state,item.path,item.value);}
}
const imageIds=new WeakMap();let imageSerial=0;
function imageId(image){if(!image)return null;if(!imageIds.has(image))imageIds.set(image,++imageSerial);return imageIds.get(image);}
export function lookSnapshot(state){return JSON.stringify({...lookPayload(state),slots:state.slots.map(s=>s?{...s,image:imageId(s.image)}:null),cover:imageId(state.cover),removed:[...state.removed].sort(),removedCover:state.removedCover});}
export function drawSlot(canvas, slot) {
  canvas.width=canvas.height=512; const ctx=canvas.getContext('2d'); ctx.clearRect(0,0,512,512);
  if(!slot) return;
  const size=Math.min(slot.w,slot.h)/slot.zoom;
  const x=slot.sx+(slot.w-size)*(slot.x+100)/200, y=slot.sy+(slot.h-size)*(slot.y+100)/200;
  ctx.drawImage(slot.image,x,y,size,size,0,0,512,512);
}
async function readImage(file) {
  if(!['image/png','image/jpeg','image/webp'].includes(file.type)) throw new Error('PNG / JPEG / WebP の画像を選んでください。');
  if(file.size>40*1024*1024) throw new Error('取り込む画像は40MB以下にしてください。');
  return createImageBitmap(file);
}
function slot(image, sx=0, sy=0, w=image.width, h=image.height) {return {image,sx,sy,w,h,x:0,y:0,zoom:1};}
const isExpression=path=>path==='person.face.habits'||path.startsWith('person.expressions.');
export function applyLookDraft(state,result){for(const item of result.assignments){if(state.touched.has(item.path)||valueAt(state,item.path))continue;setAt(state,item.path,item.value);state.quotes[item.path]=item;}state.suggestions=[...state.suggestions.filter(i=>isExpression(i.path)),...result.suggestions];}
export async function mountLook(parent, state, {view, onboarding=false, onSingle, onSheet, world, draftPath='/api/look/draft', expressionPath='/api/look/expression-suggestions'}={}) {
  const screenParent=parent;
  function pendingButton(){if(onboarding){const next=screenParent.nextElementSibling?.querySelector('.primary');if(next)next.disabled=!!state.pending;}}
  async function request(...args){state.pending=(state.pending||0)+1;pendingButton();try{return await api(...args);}finally{state.pending--;pendingButton();}}
  const host=node('div',undefined,'look-editor');parent.append(host);parent=host;
  const catalog=await request('/api/look/fields');
  if(!host.isConnected)return;
  const features=card(parent,'1. どんな姿にする？'), draftStatus=onboarding?notice(features,''):node('p','','status');
  const inputs=new Map(),details=new Map(),tabs=new Map(),panels=new Map(),suggestions=new Map();
  const expressionDetails=node('details');expressionDetails.append(node('summary','8つの表情の描き分け'));
  action(features,'設定文と性格から下書きする',async()=>{
    if(onboarding)draftStatus.update('設定文と人物像を読んでいます。','waiting');else draftStatus.textContent='設定文と人物像を読んでいます。';
    try{
      const result=await request(draftPath,{});
      if(!host.isConnected)return;
      applyLookDraft(state,result);
      draftStatus.textContent=result.dropped?'引用や欄を確認できない候補を除いて下書きしました。':'下書きしました。引用を確かめて、自由に直せます。';
      if(onboarding)draftStatus.update(draftStatus.textContent);sync(true);refreshPreview();
    }catch(e){draftStatus.textContent='下書きを作れませんでした。入力はそのまま残っています。';if(onboarding)draftStatus.update(draftStatus.textContent,'error');throw e;}
  });features.append(draftStatus);
  const expressionStatus=onboarding?notice(features,''):node('p','','status');expressionStatus.setAttribute('role','status');
  const proposeExpressions=action(features,'性格から提案',async()=>{
    if(onboarding)expressionStatus.update('性格と場面での反応を読んでいます。','waiting');else expressionStatus.textContent='性格と場面での反応を読んでいます。';
    try{
      const result=await request(expressionPath,{});if(!host.isConnected)return;
      state.suggestions=[...state.suggestions.filter(i=>!isExpression(i.path)),...result.suggestions];
      expressionStatus.textContent=result.suggestions.length?(result.dropped?'引用や欄を確認できない候補を除いて提案しました。':'提案しました。引用を確かめて「使う」で欄に入れられます。'):'根拠を確認できる表情の提案はありませんでした。';
      if(onboarding&&view==='sheet'){let count=0;for(const item of [...state.suggestions].filter(i=>isExpression(i.path)))if(applySuggestion(item))count++;expressionStatus.textContent=`${count}欄に下書きしました。入力済みの欄はそのままです。`;}
      if(onboarding)expressionStatus.update(expressionStatus.textContent);sync(true);if(onboarding&&view==='sheet')refreshPreview();
    }catch(e){expressionStatus.textContent='提案を作れませんでした。入力と前の提案はそのまま残っています。';if(onboarding)expressionStatus.update(expressionStatus.textContent,'error');throw e;}
  });
  const useAll=action(features,'全部使う',()=>{
    let applied=0;for(const item of [...state.suggestions].filter(i=>isExpression(i.path)))if(applySuggestion(item))applied++;
    expressionStatus.textContent=`${applied}欄に入れました。入力済みの欄はそのまま残しています。`;sync(true);refreshPreview();
  });features.append(expressionStatus);
  function applySuggestion(item){
    if(state.touched.has(item.path)||valueAt(state,item.path))return false;
    setAt(state,item.path,item.value);state.touched.add(item.path);state.quotes[item.path]=item;
    state.suggestions=state.suggestions.filter(s=>s!==item);return true;
  }
  const tablist=node('div',undefined,'look-tabs');tablist.setAttribute('role','tablist');tablist.setAttribute('aria-label','見た目の項目');features.append(tablist);
  const appearanceTabs=catalog.tabs.filter(tab=>tab.id!=='image');
  function activate(id){state.tab=id;for(const [key,panel]of panels){panel.hidden=key!==id;tabs.get(key).setAttribute('aria-selected',String(key===id));tabs.get(key).tabIndex=key===id?0:-1;}}
  function rememberOpen(id,open){try{localStorage.setItem(`cbs.look.details.${id}`,String(open));}catch{}}
  for(const tab of catalog.tabs){
    const panel=node('div',undefined,'look-panel');panel.id=`look-panel-${tab.id}`;
    if(tab.id!=='image'){
      const button=action(tablist,tab.title,()=>activate(tab.id));button.id=`look-tab-${tab.id}`;button.setAttribute('role','tab');button.setAttribute('aria-controls',panel.id);tabs.set(tab.id,button);
      button.onkeydown=e=>{const index=appearanceTabs.findIndex(t=>t.id===tab.id);let target;if(e.key==='ArrowRight')target=(index+1)%appearanceTabs.length;else if(e.key==='ArrowLeft')target=(index+appearanceTabs.length-1)%appearanceTabs.length;else if(e.key==='Home')target=0;else if(e.key==='End')target=appearanceTabs.length-1;if(target!==undefined){e.preventDefault();activate(appearanceTabs[target].id);tabs.get(appearanceTabs[target].id).focus();}};
      panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',button.id);panels.set(tab.id,panel);
    }
    features.append(panel);
    const more=node('details');more.append(node('summary','さらにこだわる'));more.ontoggle=()=>{if(!onboarding)rememberOpen(tab.id,more.open);};details.set(tab.id,more);
    try{more.open=!onboarding&&localStorage.getItem(`cbs.look.details.${tab.id}`)==='true';}catch{}
    for(const spec of catalog.fields.filter(f=>f.tab===tab.id)){
      if(!state.initialized.has(spec.path)){
        if(valueAt(state,spec.path)===undefined){
          let initial=spec.initial;if(initial&&typeof initial==='object'){const v=world?.[initial.world]||'';initial=initial.values?(initial.values[v]||''):(initial.except?.includes(v)?'':v);}
          setAt(state,spec.path,initial);
        }state.initialized.add(spec.path);
      }
      const host=spec.level==='expressions'?expressionDetails:spec.level==='detail'?more:panel;
      const change=v=>{setAt(state,spec.path,v);state.touched.add(spec.path);delete state.quotes[spec.path];sync();refreshPreview();};
      let input;
      if(spec.type==='select')input=select(host,spec.title,spec.options,valueAt(state,spec.path),change);
      else {input=field(host,spec.title,valueAt(state,spec.path),spec.type==='checkbox'?()=>{}:change);if(spec.type==='checkbox'){input.type='checkbox';input.onchange=()=>change(input.checked);}else{input.placeholder=spec.placeholder||'';input.maxLength=4000;}}
      input.dataset.lookPath=spec.path;inputs.set(spec.path,input);
      if(spec.help)host.append(node('p',spec.help,'small muted'));
      // Per-choice help (e.g. what each image is for) follows the current selection in sync().
      if(spec.option_help){const help=node('p','','small muted look-option-help');help.dataset.lookHelp=spec.path;host.append(help);}
      const notes=node('div',undefined,'look-evidence');host.append(notes);suggestions.set(spec.path,notes);
    }
    panel.append(more);
    if(tab.id==='image')panel.append(expressionDetails);
  }
  const referenceGuide=node('div',undefined,'look-reference');referenceGuide.append(node('p','この画像を画像生成サービスに一緒に添付してください。'));
  const imagePanel=host.querySelector('#look-panel-image');
  const download=node('a','「ふつう」の画像を保存','btn');download.download='neutral.png';referenceGuide.append(download);imagePanel.append(referenceGuide);
  const promptCard=card(parent,'2. 指示文のプレビュー'), prompt=node('pre','','copybox');
  prompt.tabIndex=0; promptCard.append(imagePanel,prompt);
  let generated='',revision=0,previewReady;
  function refreshPreview(){const current=++revision;prompt.setAttribute('aria-busy','true');previewReady=request('/api/look/prompt',{...lookPayload(state),...(world?{world}:{})}).then(result=>{if(current===revision){generated=result.prompt;prompt.textContent=generated;prompt.setAttribute('aria-busy','false');}}).catch(e=>{if(current===revision){generated='';prompt.textContent=e.message;prompt.setAttribute('aria-busy','false');}throw e;});state.previewReady=previewReady;previewReady.catch(()=>{});}
  let copyTimer;
  const copyButton=action(promptCard,'コピー',async()=>{await previewReady;if(!generated)return;clearTimeout(copyTimer);try{await navigator.clipboard.writeText(generated);copyButton.replaceChildren(icon('success'),node('span','コピーしました'));copyStatus.textContent='コピーしました。';copyTimer=setTimeout(()=>{copyButton.textContent='コピー';},2000);}catch{copyButton.textContent='コピー';copyStatus.textContent='本文を選択してコピーしてください。';const range=document.createRange();range.selectNodeContents(prompt);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);}});
  const copyStatus=node('p','','status');copyStatus.setAttribute('role','status');promptCard.append(copyStatus);
  const guide=node('ol',undefined,'steps-guide');
  for(const text of ['画像を作れるサービス（例: Gemini、ChatGPT）を開く','指示文を貼り付けて、選んだ用途の画像を作ってもらう','画像を保存して、この画面に戻る'])guide.append(node('li',text));
  promptCard.append(guide);
  const imports=card(parent,'3. 画像を取り込む'),importTabs=node('div',undefined,'look-tabs'),expressionBox=node('div'),drop=node('div',undefined,'drop');
  importTabs.setAttribute('role','tablist');importTabs.setAttribute('aria-label','取り込む画像');imports.append(importTabs,expressionBox);
  drop.append(icon('send'),node('p','表情シートをここにドロップ（4列 × 2行）'));
  const sheet=field(drop,'表情シートを1枚選ぶ','',()=>{}); sheet.type='file';sheet.accept='image/png,image/jpeg,image/webp';expressionBox.append(drop);sheet.parentElement.hidden=true;action(drop,'ファイルを選ぶ',()=>sheet.click()).classList.add('quiet');
  const separate=field(expressionBox,'別々の画像をまとめて選ぶ（1枚なら「ふつう」）','',()=>{}); separate.type='file';separate.multiple=true;separate.accept=sheet.accept;separate.parentElement.hidden=true;const separateButton=action(expressionBox,'別々の画像をまとめて選ぶ',()=>separate.click());separateButton.classList.add('quiet');
  const status=node('p','','status');status.setAttribute('role','status');imports.append(status);expressionBox.append(node('p','並び順は、ふつう・笑顔・照れ・驚き・悲しい・困った・考え中・すねる。空いた表情は「ふつう」で代用します。','small muted'));
  const grid=node('div',undefined,'grid8');expressionBox.append(grid,separateButton);
  const fallbackGuide=node('div',undefined,'look-fallback-guide');expressionBox.append(fallbackGuide);
  const coverBox=node('section',undefined,'set look-cover');coverBox.append(node('h3','ノートの扉絵（横長）'),node('p','横長のまま取り込みます。表情の8つの顔とは別に保存します。','small muted'));
  const coverDrop=node('div',undefined,'drop');coverDrop.append(node('p','扉絵をここにドロップ（横長）'));coverBox.append(coverDrop);
  const coverInput=field(coverDrop,'扉絵を1枚選ぶ','',()=>{});coverInput.type='file';coverInput.accept=sheet.accept;coverInput.parentElement.hidden=true;action(coverDrop,'ファイルを選ぶ',()=>coverInput.click()).classList.add('quiet');
  const coverPreview=node('div',undefined,'cover-preview');coverBox.append(coverPreview);imports.append(coverBox);
  try{const help=await request('/api/onboarding/look_import_help');for(const box of [expressionBox,coverBox]){const copy=node('div',undefined,'look-import-help');renderExplanation(copy,help.text||'');box.prepend(copy);}}catch{/* Optional author copy may be unavailable. */}
  const importPanels=[expressionBox,coverBox],importButtons=[];
  function activateImport(index){state.importTab=index;importPanels.forEach((panel,i)=>{panel.hidden=i!==index;importButtons[i].setAttribute('aria-selected',String(i===index));importButtons[i].tabIndex=i===index?0:-1;});}
  for(const [i,title]of ['表情','扉絵'].entries()){
    const button=action(importTabs,title,()=>activateImport(i));button.id=`look-import-tab-${i}`;button.setAttribute('role','tab');button.setAttribute('aria-controls',`look-import-panel-${i}`);importButtons.push(button);
    importPanels[i].id=`look-import-panel-${i}`;importPanels[i].setAttribute('role','tabpanel');importPanels[i].setAttribute('aria-labelledby',button.id);
    button.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?1:1-i;activateImport(next);importButtons[next].focus();}};
  }
  activateImport(state.importTab??0);
  async function importCover(files){state.pending=(state.pending||0)+1;try{if(!files.length)return;const image=await readImage(files[0]);if(image.width<=image.height)throw new Error('横長の画像を選んでください。');state.cover=image;state.removedCover=false;renderCover();status.textContent='扉絵を取り込みました。保存するとノートに表示します。';}catch(e){status.textContent=e.message;}finally{state.pending--;}}
  coverInput.onchange=()=>importCover(coverInput.files);
  coverDrop.ondragover=e=>{e.preventDefault();};coverDrop.ondrop=e=>{e.preventDefault();return importCover(e.dataTransfer.files);};
  action(coverBox,'扉絵を外す',()=>{state.cover=null;state.removedCover=true;renderCover();});
  function renderCover(){
    coverPreview.replaceChildren();
    if(state.cover){const canvas=node('canvas');canvas.setAttribute('aria-label','扉絵のプレビュー');drawCover(canvas,state.cover);coverPreview.append(canvas);}
    else if(state.existingCover&&!state.removedCover){const img=node('img');img.src=`/looks/${encodeURIComponent(partnerId)}/cover?v=${Date.now()}`;img.alt='保存済みの扉絵';coverPreview.append(img);}
    else coverPreview.append(node('p','扉絵がないときは、ノートに今の表情を表示します。','small muted'));
  }
  renderCover();
  async function importFiles(files,isSheet) {
    state.pending=(state.pending||0)+1;
    try {
      if(!files.length)return;
      if(isSheet){const image=await readImage(files[0]);for(let i=0;i<8;i++)state.slots[i]=slot(image,(i%4)*image.width/4,Math.floor(i/4)*image.height/2,image.width/4,image.height/2);}
      else {if(files.length>8)throw new Error('一度に選べるのは8枚までです。');const images=await Promise.all([...files].map(readImage));images.forEach((image,i)=>state.slots[i]=slot(image));}
      state.slots.forEach((s,i)=>{if(s)state.removed.delete(expressions[i]);});renderSlots();status.textContent='取り込みました。表情と位置を確認してください。';
    }catch(e){status.textContent=e.message;}finally{state.pending--;}
  }
  sheet.onchange=()=>importFiles(sheet.files,true);separate.onchange=()=>importFiles(separate.files,false);
  drop.ondragover=e=>{e.preventDefault();}; drop.ondrop=e=>{e.preventDefault();importFiles(e.dataTransfer.files,true);};
  function renderSlots() {
    sync();refreshPreview();
    grid.replaceChildren();
    const available=expressions.filter((e,i)=>state.slots[i]||(state.existing.includes(e)&&!state.removed.has(e)));
    fallbackGuide.replaceChildren();
    if(available.length===1&&available[0]==='neutral'){
      fallbackGuide.append(node('p','『ふつう』だけでも使えます。空いた表情は『ふつう』で表示します。ほかの表情は、それぞれの欄を押すと 1 枚ずつ足せます。'));
      action(fallbackGuide,'『ふつう』の画像を添付して表情シートを作る',()=>{state.image.from_reference=true;state.onboardingSheetUse='sheet';state.image.use='sheet';if(onSheet)return onSheet();sync();refreshPreview();promptCard.scrollIntoView?.({block:'start'});}).classList.add('quiet');
    }else if(available.length&&!available.includes('neutral'))fallbackGuide.append(node('p','「ふつう」がない時は、取り込んである最初の顔を代わりに表示します。'));
    expressions.forEach((expression,i)=>{
      const box=node('div',undefined,'expr'), canvas=node('canvas');canvas.setAttribute('aria-label',`${labels[i]}のプレビュー`);const picker=action(box,labels[i],()=>replace.click());picker.classList.add('expression-picker');picker.append(canvas);drawSlot(canvas,state.slots[i]);
      if(!state.slots[i]){if(state.existing.includes(expression)&&!state.removed.has(expression)){const img=node('img');img.src=`/looks/${encodeURIComponent(partnerId)}/${expression}`;img.alt=labels[i];img.style.width='100%';canvas.replaceWith(img);}else picker.append(node('span',available.length?(available.includes('neutral')?'ふつうで代用':'最初の顔で代用'):'未設定','muted'));canvas.hidden=true;}
      const replace=field(box,`${labels[i]}を差し替え`,'',()=>{});replace.type='file';replace.accept=sheet.accept;replace.parentElement.hidden=true;if(state.slots[i]||(state.existing.includes(expression)&&!state.removed.has(expression)))picker.append(node('span','画像','muted'));
      replace.onchange=async()=>{state.pending=(state.pending||0)+1;try{if(replace.files.length){state.slots[i]=slot(await readImage(replace.files[0]));state.removed.delete(expression);renderSlots();}}catch(e){status.textContent=e.message;}finally{state.pending--;}};
      if(state.slots[i]) {
        const details=node('details'); details.append(node('summary','位置を調整'),node('p','少し拡大すると、上下左右へ動かせます。','small muted'));box.append(details);
        for(const [key,title,min,max,step] of [['x','左右',-100,100,1],['y','上下',-100,100,1],['zoom','拡大',1,3,.05]]){
          const e=field(details,`${labels[i]}・${title}`,state.slots[i][key],v=>{state.slots[i][key]=Number(v);drawSlot(canvas,state.slots[i]);sync();});e.type='range';e.min=min;e.max=max;e.step=step;e.value=state.slots[i][key];
        }
        select(details,'入れ替える表情',Object.fromEntries(labels.map((label,index)=>[index,label])),String(i),v=>{const j=Number(v);[state.slots[i],state.slots[j]]=[state.slots[j],state.slots[i]];state.removed.delete(expressions[j]);renderSlots();});
      }
      // Only a slot that holds an image can lose it.
      if(state.slots[i]||(state.existing.includes(expression)&&!state.removed.has(expression)))action(box,'画像を外す',()=>{state.slots[i]=null;state.removed.add(expression);renderSlots();});grid.append(box);
    });
  }
  function sync(openFilled=false){
    expressionDetails.hidden=state.image.use!=='sheet';
    const expressionSpecs=catalog.fields.filter(f=>f.level==='expressions');
    if(openFilled&&expressionSpecs.some(f=>valueAt(state,f.path)||state.suggestions.some(i=>i.path===f.path)))expressionDetails.open=true;
    useAll.hidden=!state.suggestions.some(i=>isExpression(i.path));
    const hasReference=Boolean(state.slots[0]||(state.existing.includes('neutral')&&!state.removed.has('neutral')));
    if(!hasReference)state.image.from_reference=false;
    inputs.get('image.from_reference').parentElement.hidden=!hasReference;
    referenceGuide.hidden=!hasReference||!state.image.from_reference;
    if(hasReference){if(state.slots[0]){const canvas=node('canvas');drawSlot(canvas,state.slots[0]);download.href=canvas.toDataURL('image/png');}else download.href=`/looks/${encodeURIComponent(partnerId)}/neutral`;}
    for(const spec of catalog.fields){
      const input=inputs.get(spec.path);if(spec.type==='checkbox')input.checked=Boolean(valueAt(state,spec.path));else input.value=valueAt(state,spec.path)||'';
      if(spec.use)input.parentElement.hidden=state.image.use!==spec.use;
      if(spec.option_help){const help=input.parentElement.parentElement.querySelector(`[data-look-help="${spec.path}"]`)||document.querySelector(`[data-look-help="${spec.path}"]`);if(help)help.textContent=spec.option_help[valueAt(state,spec.path)]||'';}
      const notes=suggestions.get(spec.path);notes.replaceChildren();const quote=state.quotes[spec.path];if(quote)notes.append(node('blockquote',quote.quote));
      for(const item of state.suggestions.filter(i=>i.path===spec.path)){
        notes.append(node('p',`性格からの提案: ${item.value}`),node('blockquote',item.quote));
        action(notes,'使う',()=>{if(!applySuggestion(item)){notes.append(node('p','入力済みです。使う場合は欄を編集してください。'));return;}sync(true);refreshPreview();});
      }
    }
    for(const tab of catalog.tabs){const specs=catalog.fields.filter(f=>f.tab===tab.id);const filled=specs.some(f=>f.type?valueAt(state,f.path)!==f.initial:Boolean(valueAt(state,f.path)));if(tabs.has(tab.id))tabs.get(tab.id).textContent=tab.title+(filled?' ●':'');if(openFilled&&specs.some(f=>f.level==='detail'&&(Boolean(valueAt(state,f.path))||state.suggestions.some(i=>i.path===f.path))))details.get(tab.id).open=true;}
  }
  activate(tabs.has(state.tab)?state.tab:appearanceTabs[0].id);sync(!onboarding);renderSlots();
  if(view){
    tablist.hidden=true;features.hidden=true;promptCard.hidden=true;imports.hidden=true;promptCard.querySelector('h3').hidden=true;imports.querySelector('h3').hidden=true;
    if(panels.has(view)){
      features.hidden=false;for(const child of features.children)child.hidden=true;
      const panel=panels.get(view);panel.hidden=false;details.get(view).open=!onboarding;
    }else if(view==='sheet'||view==='single-prompt'||view==='cover-prompt'){
      promptCard.hidden=false;if(view==='sheet')state.onboardingSheetUse??=state.image.use==='single'?'single':'sheet';state.image.use=view==='sheet'?state.onboardingSheetUse:view==='single-prompt'?'single':'cover';sync();refreshPreview();
      const fine=node('details',undefined,'look-fine');fine.append(node('summary','細かい設定'),imagePanel);promptCard.prepend(fine);
      for(const spec of catalog.fields.filter(f=>f.tab==='image'&&f.path==='image.use'))inputs.get(spec.path).parentElement.hidden=true;
      if(view==='sheet'){proposeExpressions.textContent='8つの表情を性格から下書きする';promptCard.insertBefore(proposeExpressions,prompt);promptCard.insertBefore(expressionStatus,prompt);}
      if(view==='sheet')action(promptCard,'うまく表情シートが出ない時は',()=>{if(onSingle)return onSingle();state.onboardingSheetUse='single';state.image.use='single';sync();inputs.get('image.use').parentElement.hidden=true;refreshPreview();fine.open=true;}).classList.add('quiet');
    }else if(view==='faces'||view==='cover-import'){
      imports.hidden=false;importTabs.hidden=true;activateImport(view==='faces'?0:1);
      if(view==='faces'&&state.image.use==='single'){drop.hidden=true;separate.multiple=false;separateButton.textContent='顔の画像を1枚選ぶ';}
    }
  }

}
export async function saveLook(state) {
  if(state.skip)return;
  for(const expression of state.removed)await api(`/api/look/${expression}`,{},'DELETE');
  if(state.removedCover)await api('/api/look/cover',{},'DELETE');
  if(state.cover){
    const canvas=node('canvas');drawCover(canvas,state.cover);const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
    if(!blob)throw new Error('扉絵を書き出せませんでした。');
    const r=await fetch(scoped('/api/look/cover'),{method:'PUT',headers:{'Content-Type':'image/png'},body:blob});
    if(!r.ok)throw new Error('扉絵の保存に失敗しました。もう一度保存してください。');
  }
  for(let i=0;i<8;i++)if(state.slots[i]){
    const canvas=node('canvas');drawSlot(canvas,state.slots[i]);const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
    if(!blob)throw new Error('画像を書き出せませんでした。');
    const r=await fetch(scoped(`/api/look/${expressions[i]}`),{method:'PUT',headers:{'Content-Type':'image/png'},body:blob});
    if(!r.ok)throw new Error('画像の保存に失敗しました。もう一度保存してください。');
  }
  await api('/api/look/meta',lookPayload(state),'PUT');
}
