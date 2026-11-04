import {saveOnEdit} from './settings-save.js';
// Native state is authoritative: document.visibilityState alone misses an unfocused window.
export const isResident=()=>typeof window.__TAURI__?.core?.invoke==='function';
let context={visible:false,minimized:true,focused:false,foreground:false,everShown:false},firstShow,openTalk,warning,focusInput,focusPending=false;
const invoke=(command,args)=>window.__TAURI__.core.invoke(command,args);
const ready=isResident()?(async()=>{
  await window.__TAURI__.event.listen('resident-visibility',event=>{
    Object.assign(context,event.payload);
    if(context.everShown)firstShow?.();
    window.dispatchEvent(new window.Event('resident-visibility-change'));
  });
  await window.__TAURI__.event.listen('resident-open-talk',()=>{void deliverTalk();});
  await window.__TAURI__.event.listen('resident-warning',event=>warning?.(new Error(event.payload)));
  await window.__TAURI__.event.listen('resident-focus-input',()=>{focusPending=true;if(focusInput){focusPending=false;void focusInput();}});
  Object.assign(context,await invoke('resident_context'));
  // A cached page stopped its timers on pagehide; reload its history and wiring.
  window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
  // Keep the live WebView eligible for work while hidden (no OS wake/sleep promise).
  if(navigator.locks)void navigator.locks.request('awalia-resident',()=>new Promise(()=>{}));
})():Promise.resolve();

// Native keeps a click until the destination page has installed its handler.
async function deliverTalk(){
  if(!openTalk)return;
  try{const target=await invoke('resident_take_talk');if(target)await openTalk(target);}catch(error){warning?.(error);}
}

export const residentForeground=()=>!isResident()||context.foreground;
export const residentVisible=()=>!isResident()||(context.visible&&!context.minimized);
export async function desktopHeaders(){await ready;return isResident()?{'X-Awalia-Desktop':context.token}:{};}
export async function markResidentReady(){if(isResident()){await ready;await invoke('resident_ui_ready');}}
export async function refreshForeground(){
  if(isResident()){await ready;Object.assign(context,await invoke('resident_context'));}
  return residentForeground();
}
export async function refreshResidentVisible(){
  if(isResident()){await ready;Object.assign(context,await invoke('resident_context'));}
  return residentVisible();
}
export function firstSentence(text){
  const line=String(text||'').trim();
  // Punctuation formatting only; decisions about whether/what to say stay with the judge.
  const first=typeof Intl.Segmenter==='function'?[...new Intl.Segmenter('ja',{granularity:'sentence'}).segment(line)][0]?.segment:line.match(/^[\s\S]*?[。！？!?](?:[」』”"])?(?=\s|[^。！？!?]|$)|^[^\n]+/u)?.[0];
  return [...(first||line).trim()].slice(0,300).join('');
}
export async function notifyResident(partner,result,sessionId,{outsideTalk=false}={}){
  if(!isResident()||(await refreshForeground()&&!outsideTalk))return;
  await syncResidentFace();
  await invoke('resident_notify',{title:partner.name,body:firstSentence(result.assistant_text),turnId:result.turn_id,sessionId,characterId:partner.id,...(outsideTalk?{outsideTalk:true}:{}),...(result.habit?.allow_quiet?{allowQuiet:true}:{})});
}
export async function syncResidentFace(){
  if(!isResident())return;
  const headers=await desktopHeaders(),started=performance.now();
  const response=await fetch('/api/resident/face',{headers,signal:AbortSignal.timeout(5000)});
  if(!response.ok)throw new Error('端のパートナーの表情を読み込めませんでした。');
  const face=await response.json();
  face.validForMs=Math.max(0,Math.floor(face.validForMs-(performance.now()-started)));
  if(face.replyRemainingMs!=null)face.replyRemainingMs=Math.max(0,Math.floor(face.replyRemainingMs-(performance.now()-started)));
  await invoke('resident_sync',{face});
}
export async function residentControls(parent,{notifications=parent,conversation=parent,ptt=conversation,autosave=false}={}){
  if(!isResident())return null;
  await ready;
  const status=await invoke('resident_preferences'),prefs=structuredClone(status.preferences);
  const group=document.createElement('section');group.className='set';parent.append(group);
  const helpText=(host,text)=>{const p=document.createElement('p');p.className='small muted';p.textContent=text;host.append(p);};
  const label=document.createElement('label'),always=document.createElement('input'),text=document.createElement('span');
  label.className='switch';always.type='checkbox';always.id='resident-always-visible';always.checked=prefs.alwaysVisible;
  text.textContent='画面の端にいつも顔を出す';label.append(always,text);group.append(label);
  const help=document.createElement('p');help.className='small muted';help.textContent='話す時は吹き出しを表示します。顔はドラッグで動かせます。全画面のアプリ使用中と静かな時間帯は隠れます。';group.append(help);
  const sizeLabel=document.createElement('label'),size=document.createElement('select');
  sizeLabel.textContent='顔の大きさ';size.id='resident-face-size';
  for(const [value,title] of [[64,'小（64px）'],[96,'中（96px）'],[128,'大（128px）']]){
    const option=document.createElement('option');option.value=String(value);option.textContent=title;size.append(option);
  }
  size.value=String(prefs.faceSize??96);sizeLabel.append(size);group.append(sizeLabel);
  helpText(group,'常駐アプリで画面の端に出す顔の大きさを変えます。');
  const soundLabel=document.createElement('label'),sound=document.createElement('input'),soundText=document.createElement('span');
  soundLabel.className='switch';sound.type='checkbox';sound.id='resident-chime-enabled';sound.checked=prefs.chimeEnabled??true;
  soundText.textContent='話しかけの音を鳴らす';soundLabel.append(sound,soundText);notifications.append(soundLabel);
  helpText(notifications,'常駐アプリから話しかける時に、短い音で知らせます。');
  const volumeLabel=document.createElement('label'),volume=document.createElement('input'),volumeValue=document.createElement('span');
  volumeLabel.textContent='話しかけの音量';volume.type='range';volume.id='resident-chime-volume';volume.min='0';volume.max='100';volume.step='1';volume.value=String(prefs.chimeVolume??40);
  volumeLabel.append(volume,volumeValue);notifications.append(volumeLabel);
  helpText(notifications,'話しかけを知らせる音の大きさを変えます。');
  const updateVolume=()=>{volume.disabled=!sound.checked;volumeValue.textContent=`${volume.value}%`;};
  sound.onchange=updateVolume;volume.oninput=updateVolume;updateVolume();
  const motionLabel=document.createElement('label'),motion=document.createElement('input'),motionText=document.createElement('span');
  motionLabel.className='switch';motion.type='checkbox';motion.id='resident-idle-motion';
  motion.checked=prefs.idleMotion??!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  motionText.textContent='端の顔に小さな動きをつける';motionLabel.append(motion,motionText);group.append(motionLabel);
  helpText(group,'常駐アプリの顔に、まばたきなどの小さな動きをつけます。');
  const shortcutLabel=document.createElement('label'),enabled=document.createElement('input'),shortcutText=document.createElement('span');
  shortcutLabel.className='switch';enabled.type='checkbox';enabled.id='resident-shortcut-enabled';enabled.checked=prefs.shortcut!==null;
  shortcutText.textContent='キーで会話を開く・隠す';shortcutLabel.append(enabled,shortcutText);conversation.append(shortcutLabel);
  helpText(conversation,'ほかのアプリを使っている時も、決めたキーで常駐アプリの会話を開閉できます。');
  const keyLabel=document.createElement('label'),keys=document.createElement('input');
  keyLabel.textContent='呼び出しのキー（例: Ctrl+Alt+A）';keys.id='resident-shortcut';keys.type='text';keys.maxLength=100;keys.value=prefs.shortcut||'Ctrl+Alt+A';keys.disabled=!enabled.checked;keyLabel.append(keys);
  conversation.append(keyLabel);helpText(conversation,'会話を開閉するキーの組み合わせを指定します。');
  enabled.onchange=()=>{keys.disabled=!enabled.checked;};
  const pttStatus=await invoke('resident_ptt_preferences');
  const pttLabel=document.createElement('label'),pttKey=document.createElement('input');
  pttLabel.textContent='押して話すキー（空欄なら使わない）';pttKey.id='resident-ptt-shortcut';pttKey.type='text';pttKey.maxLength=100;pttKey.value=pttStatus.shortcut||'';pttLabel.append(pttKey);ptt.append(pttLabel);
  helpText(ptt,'指定したキーを押している間、マイクで声を入力します。');
  const pttReason=document.createElement('p');pttReason.setAttribute('role','status');pttReason.textContent=pttStatus.error||'';ptt.append(pttReason);
  const reason=document.createElement('p');reason.id='resident-shortcut-status';reason.className='small';reason.setAttribute('role','status');reason.textContent=status.shortcutError||'';conversation.append(reason);
  if(autosave){
    let saving=Promise.resolve();const enqueue=work=>{saving=saving.catch(()=>{}).then(work);return saving;};
    for(const [input,key,read]of [[always,'alwaysVisible',()=>always.checked],[size,'faceSize',()=>Number(size.value)],[sound,'chimeEnabled',()=>sound.checked],[volume,'chimeVolume',()=>Number(volume.value)],[motion,'idleMotion',()=>motion.checked],[enabled,'shortcut',()=>enabled.checked?keys.value.trim():null],[keys,'shortcut',()=>enabled.checked?keys.value.trim():null]])// Send every field, as the old save did: an exe built before partial updates rejects a partial object.
saveOnEdit(input,()=>enqueue(async()=>{const updated=await invoke('resident_preferences',{preferences:{alwaysVisible:always.checked,faceSize:Number(size.value),chimeEnabled:sound.checked,chimeVolume:Number(volume.value),idleMotion:motion.checked,shortcut:enabled.checked?keys.value.trim():null}});reason.textContent=updated.shortcutError||'';if(updated.shortcutError)throw new Error(updated.shortcutError);await syncResidentFace();}),{read});
    saveOnEdit(pttKey,async value=>{const result=await invoke('resident_ptt_preferences',{shortcut:value,save:true});pttReason.textContent=result.error||'';if(result.error)throw new Error(result.error);},{read:()=>pttKey.value.trim()||null});
  }
  return async()=>{
    try{
      const updated=await invoke('resident_preferences',{preferences:{alwaysVisible:always.checked,faceSize:Number(size.value),chimeEnabled:sound.checked,chimeVolume:Number(volume.value),idleMotion:motion.checked,shortcut:enabled.checked?keys.value.trim():null}});
      reason.textContent=updated.shortcutError||'';
      try{const result=await invoke('resident_ptt_preferences',{shortcut:pttKey.value.trim()||null,save:true});pttReason.textContent=result.error||'';}catch(error){pttReason.textContent=String(error.message||error);throw error;}
      await syncResidentFace();
    }catch(error){reason.textContent=String(error.message||error);throw new Error(reason.textContent);}
  };
}
export function idleNudgeControl(parent,cfg,draft){
  if(!isResident()||!Object.hasOwn(cfg.autonomy,'idle_nudge'))return;
  const label=document.createElement('label'),input=document.createElement('input'),text=document.createElement('span');
  label.className='switch';input.type='checkbox';input.id='resident-idle-nudge';input.checked=cfg.autonomy.idle_nudge;
  input.onchange=()=>draft.autonomy.idle_nudge=input.checked;
  text.textContent='しばらく話していない時に話しかける';label.append(input,text);parent.append(label);
}
export function residentSetting(draft){return isResident()&&typeof draft.autonomy.idle_nudge==='boolean'?{idle_nudge:draft.autonomy.idle_nudge}:{};}

export async function startResident({opening,settings,idle,habit,busy,onReply,onOpen,onFocus,onError}){
  if(!isResident()){
    await opening();let stopped=false,timer,interval=180000;
    async function tick(){
      try{const cfg=await settings();interval=(cfg.days?.poll_seconds||180)*1000;if(cfg.days?.has_habits&&!busy()&&document.visibilityState==='visible'&&(location.hash||'#talk')==='#talk'){const result=await habit();if(result.speak)await onReply(result);}}
      catch(error){onError(error);}finally{if(!stopped)timer=setTimeout(tick,interval);}
    }
    const cfg=await settings();
    if(cfg.days?.has_habits)timer=setTimeout(tick,(cfg.days.poll_seconds||180)*1000);
    // Start after a habit is added without requiring a reload; no background browser calls.
    const changed=()=>{if(!timer)timer=setTimeout(tick,1000);};window.addEventListener('habits-changed',changed);
    return ()=>{stopped=true;clearTimeout(timer);window.removeEventListener('habits-changed',changed);};
  }
  await ready;
  Object.assign(context,await invoke('resident_context'));
  let opened=!!context.openingClaimed,openingPending=false,openingDue=false,stopped=false,timer,faceTimer,openingTimer,interval=180000;
  warning=onError;openTalk=onOpen;
  await deliverTalk();
  const nativeStatus=await invoke('resident_preferences');
  if(nativeStatus.shortcutError)onError(new Error(nativeStatus.shortcutError));
  focusInput=onFocus;
  if(focusPending&&focusInput){focusPending=false;void focusInput();}
  async function faceTick(){
    try{await syncResidentFace();}catch{/* Native expiry hides the face until a fresh snapshot arrives. */}
    finally{if(!stopped)faceTimer=setTimeout(faceTick,10000);}
  }
  await faceTick();
  async function tryOpening(background){
    if(stopped||opened||openingPending)return;
    if(busy())return;
    openingPending=true;clearTimeout(openingTimer);
    try{
      // Claim before the request: reloads and page changes cannot repeat it.
      opened=true;
      if(!await invoke('resident_opening',{deferred:false}))return;
      const result=await opening(background);
      if(result?.deferred){await invoke('resident_opening',{deferred:true});opened=false;}
    }
    catch(error){onError(error);}
    finally{
      openingPending=false;
      // A show event during a quiet-hours response must not lose the explicit greeting.
      if(!opened&&context.everShown&&!stopped)void tryOpening(false);
    }
  }
  firstShow=()=>{void tryOpening(false);};
  if(context.everShown)firstShow();
  async function tick(){
    try{
      const cfg=await settings();interval=(cfg.autonomy.idle_poll_seconds||180)*1000;
      if(stopped)return;
      Object.assign(context,await invoke('resident_context'));
      if(stopped)return;
      // A quiet-hours response may release the claim after this page loaded.
      if(!openingPending)opened=!!context.openingClaimed;
      openingDue||=(context.elapsedMs||0)>=(cfg.autonomy.resident_opening_delay_seconds??90)*1000;
      if((openingDue||context.everShown)&&!opened&&!busy())await tryOpening(!context.everShown);
      if(((opened&&cfg.autonomy.idle_nudge)||cfg.days?.has_habits)&&!openingPending&&!busy()){
        const result=await idle();
        if(result.speak&&!stopped)await onReply(result);
      }
    }catch(error){onError(error);}
    finally{if(!stopped)timer=setTimeout(tick,interval);}
  }
  let delay=90000;
  try{const cfg=await settings();interval=(cfg.autonomy.idle_poll_seconds||180)*1000;delay=(cfg.autonomy.resident_opening_delay_seconds??90)*1000;}catch(error){onError(error);}
  if(!opened)openingTimer=setTimeout(async()=>{openingDue=true;if(!busy())await tryOpening(!context.everShown);},Math.max(0,delay-(context.elapsedMs||0)));
  timer=setTimeout(tick,interval);
  const stop=()=>{stopped=true;clearTimeout(timer);clearTimeout(faceTimer);clearTimeout(openingTimer);firstShow=null;openTalk=null;warning=null;focusInput=null;window.removeEventListener('pagehide',stop);};
  window.addEventListener('pagehide',stop,{once:true});return stop;
}
