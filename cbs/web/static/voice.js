import {saveOnEdit} from './settings-save.js';
import {notice,choiceCards} from './onboarding.js';
import {api, scoped, node, field, select, action, card, icon} from './ui.js';
import {isResident, residentVisible, refreshResidentVisible} from './resident.js';
let config={enabled:false,autoplay:true}, current, request, objectUrl, generation=0, finishPlayback, automaticPlayback=false, resetControl;
function playbackVolume(){try{const value=localStorage.getItem('cbs.voiceVolume');const number=value===null?1:Number(value);return Number.isFinite(number)?Math.min(1,Math.max(0,number)):1;}catch{return 1;}}
let volume=playbackVolume();
function setVolume(value){volume=value;try{localStorage.setItem('cbs.voiceVolume',String(volume));}catch{}if(current)current.volume=volume;for(const audio of document.querySelectorAll('.voice-references audio'))audio.volume=volume;}
export function configureVoice(value){config=value||{enabled:false,autoplay:true};if(!config.enabled)stopVoice();}
export async function voiceQuickSettings(parent,cfg,name){
  const box=card(parent,`${name}の声`),result=await api('/api/voice'),status=node('p','','status');status.setAttribute('role','status');box.append(status);
  if(result.warning){const warning=node('p',result.warning,'warn'),next=node('a','声を変える ›');next.href='/wizard?voice=1';warning.append(next);box.querySelector('h3').after(warning);}
  const currentVoice=node('div',undefined,'row voice-current');box.append(currentVoice);currentVoice.append(node('p',`${result.voice.mode==='clone'?'参照音声から作った声':'説明から作った声'} · ${cfg.tts.engine==='external'?'外部の音声サーバー':'この PC で合成'}`));
  action(currentVoice,'声を聞く',()=>play('/api/voice/preview',{text:result.preview_text||'こんにちは。',expression:'neutral'},text=>status.textContent=text));
  const controls=[];let enabled;
  for(const [key,title]of [['enabled','音声を使う'],['autoplay','新しい返答を自動で読み上げる'],['autoplay_proactive','自分から話しかけてきた時も読み上げる']]){
    const label=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=!!cfg.tts[key];label.append(input,node('span',title));box.append(label);
    if(key==='enabled')enabled=input;else controls.push(input);saveOnEdit(input,v=>api('/api/settings',{tts:{[key]:v}},'PUT'));
  }
  const volumeInput=field(box,'再生の音量',Math.round(volume*100),v=>{setVolume(Number(v)/100);value.textContent=v+'%';}),value=node('output',`${Math.round(volume*100)}%`);volumeInput.type='range';volumeInput.min=0;volumeInput.max=100;const volumeRow=node('div',undefined,'row volume-row');volumeInput.parentElement.append(volumeRow);volumeRow.append(volumeInput,value);controls.push(volumeInput);saveOnEdit(volumeInput,v=>setVolume(Number(v)/100));
  const paint=()=>{for(const e of controls)e.disabled=!enabled.checked;};enabled.addEventListener('change',paint);paint();box.append(node('p','音量はこの端末にだけ、すぐ保存します。','small muted'));
  const change=node('a','声を変える','btn settings-flow');change.append(icon('right'));change.href='/wizard?voice=1';box.append(change,node('p','声の作り方・参照音声・音声のエンジンは、この手順の中で選び直せます。','small muted'));
}
export function stopVoice(){resetControl?.();resetControl=null;generation++;automaticPlayback=false;request?.abort();request=null;current?.pause();current=null;finishPlayback?.();finishPlayback=null;if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=null;}
async function canPlay(version){
  if(version!==generation)return false;
  if(!automaticPlayback)return true;
  const visible=await refreshResidentVisible();
  if(version!==generation)return false;
  if(!visible){stopVoice();return false;}
  return true;
}
async function streamedAudio(response,version,onStatus){
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',playback=Promise.resolve(),failure,completed=false;
  function event(frame){
    const kind=frame.split('\n').find(line=>line.startsWith('event:'))?.slice(6).trim();
    const data=JSON.parse(frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5)).join('\n'));
    if(kind==='error')throw new Error(data.message);
    if(kind==='done'){completed=true;return;}
    if(kind!=='audio')return;
    playback=playback.then(async()=>{
      if(!await canPlay(version))return;
      if(objectUrl)URL.revokeObjectURL(objectUrl);
      const bytes=Uint8Array.from(atob(data.wav),c=>c.charCodeAt(0));
      objectUrl=URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}));current=new Audio(objectUrl);
      current.volume=volume;
      await new Promise((resolve,reject)=>{
        finishPlayback=resolve;current.onended=resolve;current.onerror=()=>reject(new Error('音声を再生できませんでした。'));
        current.play().then(()=>{if(version===generation)onStatus('再生中');},reject);
      });
    });
    void playback.catch(error=>{failure=error;request?.abort();});
  }
  try{
    while(true){
      const {value,done}=await reader.read();if(version!==generation)return;
      buffer+=decoder.decode(value||new Uint8Array(),{stream:!done}).replaceAll('\r\n','\n');
      let split;while((split=buffer.indexOf('\n\n'))!==-1){event(buffer.slice(0,split));buffer=buffer.slice(split+2);}
      if(done)break;
    }
    if(!completed)throw new Error('音声の接続が途切れました。もう一度再生してください。');
    await playback;
  }catch(error){throw failure||error;}
  if(version===generation){onStatus('');stopVoice();}
}
async function play(path,body,onStatus=()=>{},onReady=()=>{},automatic=false){
  const startingGeneration=generation;
  try{if(automatic&&(!await refreshResidentVisible()||startingGeneration!==generation))return;}catch{return;}
  stopVoice();automaticPlayback=automatic;const version=generation, controller=new AbortController();request=controller;
  try{
    onStatus('音声を準備しています…');
    let response;
    for(let attempts=0;attempts<180;attempts++){
      response=await fetch(scoped(path),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});
      if(response.status!==409||!path.startsWith('/api/turns/'))break;
      await new Promise(resolve=>setTimeout(resolve,500));
      if(!await canPlay(version))return;
    }
    if(!response.ok){const error=await response.json();throw new Error(error.detail||'音声を作れませんでした。');}
    if(!await canPlay(version))return;
    if(response.headers.get('Content-Type')?.includes('text/event-stream')){await streamedAudio(response,version,onStatus);return;}
    const blob=await response.blob();if(version!==generation)return;
    if(!await canPlay(version))return;
    onReady(response);
    objectUrl=URL.createObjectURL(blob);current=new Audio(objectUrl);
    current.volume=volume;
    current.onended=()=>{if(version===generation){onStatus('');stopVoice();}};
    current.onerror=()=>{if(version===generation){onStatus('音声を再生できませんでした。もう一度お試しください。');stopVoice();}};
    await current.play();if(version===generation)onStatus('再生中');
  }catch(error){if(error.name!=='AbortError'&&version===generation){onStatus(error.name==='NotAllowedError'?'再生ボタンを押して聞いてください。':error.message);stopVoice();}}
}
export function voiceButton(parent,turnId,{autoplay=false,pending=false,proactive=false}={}){
  if(!config.enabled||!turnId)return;
  const row=node('div',undefined,'voice-playback'),status=node('span','','voice-status'),button=node('button',undefined,'btn quiet icon-button');status.setAttribute('role','status');button.type='button';
  let active=false;
  function paint(text=''){active=text==='再生中'||text==='音声を準備しています…';button.setAttribute('aria-label',active?'止める':'声で聞く');button.setAttribute('aria-pressed',String(active));button.replaceChildren(icon(active?'stop':'speaker'));status.textContent=text;}
  const start=(automatic=false)=>play(`/api/turns/${turnId}/audio/stream`,{},text=>{paint(text);if(active)resetControl=()=>{if(active)paint();};},()=>{},automatic);
  button.onclick=()=>{if(active)stopVoice();else return start();};row.start=start;paint();row.append(button,status);parent.append(row);
  if(autoplay&&config.autoplay&&(!proactive||config.autoplay_proactive)&&!pending)void start(true);
}
export function autoplayTurn(turnId){
  if(!config.enabled||!config.autoplay)return;
  const article=[...document.querySelectorAll('[data-turn-id]')].find(e=>e.dataset.turnId===String(turnId));
  const row=article?.querySelector('.voice-playback');
  if(row)void row.start(true);
}
// A resident page has no chat article, but uses the same automatic playback gates. Its turns are always speak-ups.
export function autoplayResidentTurn(turnId){
  if(config.enabled&&config.autoplay&&config.autoplay_proactive)void play(`/api/turns/${turnId}/audio/stream`,{},()=>{},()=>{},true);
}
window.addEventListener('resident-visibility-change',()=>{if(automaticPlayback&&isResident()&&!residentVisible())stopVoice();});
const bytes=value=>`${(value/1e6).toFixed(1)} MB`;
const roughSize=value=>value>=1e9?`${(value/1e9).toFixed(1)} GB`:bytes(value);
export async function downloadControls(parent,draft,onState=()=>{},{onboarding=false,chooseModel=false}={}){
  const section=card(parent,'音声モデルの取得'),status=node('p','','status'),progress=node('progress'),confirmation=node('div');
  status.setAttribute('role','status');progress.max=100;section.append(status,progress,confirmation);const result=onboarding?notice(section,''):null;
  let timer,latest;
  const model=select(section,'使うモデル',{auto:'自動（GPUならq8、CPUならq4）',q8_0:'q8',q4_k:'q4'},draft.tts.local_model||'auto',v=>{draft.tts.local_model=v;confirmation.replaceChildren();void refresh();});
  model.parentElement.hidden=onboarding&&!chooseModel;
  function paint(state){
    latest=state;onState(state);const active=state.status==='downloading';model.disabled=active;
    const labels={missing:'未取得',downloading:'取得中',ready:'準備完了',cancelled:'取り消しました。途中から再開できます。',error:'エラー'};
    const stage=state.stage==='engine'?'配布物':state.stage==='cancelling'?'取り消し中':'モデル';
    const amount=state.total?` ${bytes(state.completed||0)} / ${bytes(state.total)}（${state.percent??0}%）`:'';
    const eta=state.eta_seconds==null?' 残り時間を計算中':` 残り約${Math.ceil(state.eta_seconds/60)}分`;
    status.textContent=`${labels[state.status]||state.status}${active?'・'+stage+amount+eta:''} ${state.error||''} ${onboarding?'声に必要なエンジンとモデルを取得します（約 '+roughSize(state.download_bytes??state.total??0)+'）。取得済みのものは使い回します。':state.reason||''}`;
    if(onboarding){status.hidden=['ready','error'].includes(state.status);result.update(state.status==='ready'?(state.model||draft.tts.model||'音声モデル')+'の取得が完了しました。次へ進めます。':state.status==='error'?(state.error||'取得に失敗しました。'):'',state.status==='error'?'error':'success');}
    progress.hidden=!active;progress.value=state.percent||0;progress.setAttribute('aria-label','音声モデルの取得率');
    cancel.hidden=!active;acquire.hidden=active||(onboarding&&state.status==='ready');acquire.textContent=onboarding?'取得する':state.status==='ready'?'再取得・整合性を確認する':state.status==='cancelled'||state.status==='error'?'途中から再開する':'音声モデルを取得する';
    if(active){clearTimeout(timer);timer=setTimeout(()=>{if(section.isConnected)void refresh();},750);}
  }
  async function refresh(){try{paint(await api('/api/voice/download?choice='+encodeURIComponent(draft.tts.local_model||'auto')));}catch(error){if(onboarding)result.update(error.message,'error');else status.textContent=error.message;}}
  const acquire=action(section,'音声モデルを取得する',async()=>{
    try{const quote=await api('/api/voice/download/plan',{choice:draft.tts.local_model||'auto'});
    if(onboarding){paint(await api('/api/voice/download',{token:quote.token,confirmed:true}));return;}
    confirmation.replaceChildren(node('p',`${quote.model}：全体 ${bytes(quote.total)}、取得済み ${bytes(quote.completed)}、これから最大 ${bytes(quote.download_bytes)} を取得します。${quote.reason}`));
    action(confirmation,'この容量で取得する',async()=>{paint(await api('/api/voice/download',{token:quote.token,confirmed:true}));confirmation.replaceChildren();},true);
    action(confirmation,'やめる',()=>confirmation.replaceChildren());}catch(error){if(onboarding)result.update(error.message,'error');else throw error;}
  });

  const cancel=action(section,'取得を取り消す',async()=>{await api('/api/voice/download/cancel',{});await refresh();});cancel.hidden=true;if(onboarding)cancel.classList.add('quiet');
  await refresh();
  return ()=>latest?.status==='ready';
}

export async function voiceSettings(form,draft,{setup=false,pageSave=false,onboarding=false,allowDisabled=false}={}){
  const prefix=setup?'/api/setup/voice':'/api/voice';
  const root=card(form,'声'),result=await api(prefix),editor=node('div',undefined,'voice-editor');
  let voice={...result.voice},references=result.references,ready=false,candidateId='',appliedTts=structuredClone(draft.tts),decided='',previewed='',mounted=false;
  const status=node('p','','status');status.setAttribute('role','status');
  const toggle=(title,value,change)=>{const label=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=value;input.onchange=()=>change(input.checked);label.append(input,node('span',title));root.append(label);return label;};
  toggle('音声を使う',draft.tts.enabled,v=>{draft.tts.enabled=v;showEditor();});
  const autoplayToggle=onboarding?choiceCards(root,'自動で読み上げる',[['yes','読み上げる','返事を声でも聞けます。'],['no','読み上げない','文字で返事を読みます。']],draft.tts.autoplay?'yes':'no',v=>draft.tts.autoplay=v==='yes'):toggle('新しい返答を自動で読み上げる',draft.tts.autoplay,v=>draft.tts.autoplay=v);
  if(!onboarding)toggle('自分から話しかけてきた時も読み上げる',!!draft.tts.autoplay_proactive,v=>draft.tts.autoplay_proactive=v);
  let playbackVolume=volume;
  const volumeInput=field(root,'再生の音量',Math.round(volume*100),v=>{playbackVolume=Number(v)/100;if(!pageSave)setVolume(playbackVolume);volumeValue.textContent=`${v}%`;});
  volumeInput.type='range';volumeInput.min=0;volumeInput.max=100;volumeInput.step=1;
  const volumeValue=node('output',`${Math.round(volume*100)}%`);volumeInput.parentElement.classList.add('volume-field');const volumeRow=node('div',undefined,'row volume-row');volumeInput.parentElement.append(volumeRow);volumeRow.append(volumeInput,volumeValue);
  root.append(node('p','この端末に保存します。保存した音声は変わりません。','small muted'));
  const engineInput=select(root,'音声のエンジン',{'speech.cpp':onboarding?'ローカルモデル（おすすめ）':'このPC（speech.cpp / Irodori）',external:'外部の音声サーバー'},draft.tts.engine,v=>{draft.tts.engine=v;showEditor();});
  const engineChoice=onboarding?choiceCards(root,'声のエンジン',[['speech.cpp','ローカルモデル','このパソコンで声を作ります。'],['external','外部の音声サーバー','用意した音声サーバーを使います。']],draft.tts.engine,v=>{draft.tts.engine=v;engineInput.value=v;}):engineInput.parentElement;
  if(onboarding)engineInput.parentElement.hidden=true;
  const downloads=node('div');root.append(downloads);
  await downloadControls(downloads,draft,state=>{ready=state.status==='ready';showEditor();},{onboarding,chooseModel:allowDisabled});
  const external=node('div',undefined,'voice-external');root.append(external);
  const urlInput=field(external,'外部の音声サーバーURL（/v1 まで）',draft.tts.base_url,v=>draft.tts.base_url=v);
  const modelInput=field(external,'外部のモデル名（任意）',draft.tts.model,v=>draft.tts.model=v);
  const nameInput=field(external,'外部の声の名前',voice.external_voice,v=>voice.external_voice=v);
  const styleInput=select(external,'外部エンジンの声の調子',{none:'付けない',instructions:'指示文で渡す'},draft.tts.styles.external?.mode||'none',v=>{draft.tts.styles.external??={values:{}};draft.tts.styles.external.mode=v;});
  root.append(editor);
  const methodInput=select(editor,'声の作り方',onboarding?{design:'説明文で',clone:'参照音声で'}:{design:'説明から作る',clone:'参照音声から作る（クローン）'},voice.mode,v=>{voice.mode=v;showMethod();});
  const methodChoice=onboarding?choiceCards(editor,'声の作り方',[['design','説明文で','声の特徴を言葉で伝えて作ります。'],['clone','参照音声で','見本の音声を使って作ります。']],voice.mode,v=>{voice.mode=v;methodInput.value=v;}):methodInput.parentElement;
  if(onboarding)methodInput.parentElement.hidden=true;
  const design=node('div',undefined,'voice-design'),clone=node('div',undefined,'voice-clone');editor.append(design,clone);
  function showMethod(){if(onboarding&&mounted)return;design.hidden=voice.mode!=='design';clone.hidden=voice.mode!=='clone';sample.hidden=draft.tts.engine!=='speech.cpp';}
  function showEditor(){
    if(!mounted||onboarding)return;const local=draft.tts.engine==='speech.cpp';
    downloads.hidden=!local;external.hidden=local;
    editor.hidden=setup&&(!draft.tts.enabled||(local&&!ready));
    sample.hidden=!local;
  }
  const description=field(design,'声の説明',voice.description,v=>voice.description=v,'textarea');description.maxLength=2000;
  design.append(node('p','どんな声で、どんな様子で話すかを書いてください。','small muted'));
  if(!setup)action(design,onboarding?'設定と性格から下書き':'人物の設定から声の説明を作り直す',async()=>{const r=await api(prefix+'/design',{});voice.description=r.voice.description;description.value=voice.description;status.textContent='説明を下書きしました。声を試せます。';});
  const sample=action(design,voice.sample_reference?'作り直す':'この説明で声を作る',async()=>{
    await prepare();const r=await api(prefix+'/sample',{candidate_id:candidateId});
    voice={...r.voice};sample.textContent='作り直す';status.textContent='候補の声を作りました。試し読みで聞けます。';
  });
  clone.append(node('p','権利を持っている声だけを使ってください。','small muted'),node('h4','参照音声の一覧'),node('p','同じ人の短い音声を追加してください。決めるまでは現在の声を変えません。','small muted'));
  const list=node('ul',undefined,'voice-references'),total=node('p','','small');clone.append(list,total);
  function renderReferences(data){
    references=data.references;list.replaceChildren();
    for(const clip of references){
      const row=node('li'),audio=node('audio');audio.controls=true;audio.preload='none';audio.volume=volume;audio.src=scoped(prefix+'/references/'+encodeURIComponent(clip.id));audio.setAttribute('aria-label',clip.name+'の試聴');
      row.append(node('span',`${clip.name}（${clip.seconds.toFixed(1)}秒）`),audio);
      action(row,'外す',async()=>{renderReferences(await api(prefix+'/references/'+encodeURIComponent(clip.id),{},'DELETE'));status.textContent='一覧を変更しました。候補の声を試せます。';});list.append(row);
    }
    if(!references.length)list.append(node('li','参照音声はまだありません。'));
    total.textContent=`合計 ${(data.total_seconds??references.reduce((n,c)=>n+c.seconds,0)).toFixed(1)}秒 / 目安30秒前後・上限120秒。${data.reference_warning||''}`;
  }
  renderReferences(result);
  action(clone,'すべて外す',async()=>{renderReferences(await api(prefix+'/references',{},'DELETE'));status.textContent='一覧を空にしました。現在の声は変わりません。';});
  const drop=node('label','WAVを追加する、またはここへドロップ','voice-drop'),file=node('input');file.type='file';file.accept='.wav,audio/wav';file.multiple=true;drop.append(file);clone.append(drop);
  async function upload(files){
    for(const value of files){
      if(value.size>20*1024*1024)throw new Error('WAVは20MB以下にしてください。');
      const r=await fetch(scoped(prefix+'/reference'),{method:'PUT',headers:{'Content-Type':'audio/wav','X-Filename':encodeURIComponent(value.name||'reference.wav')},body:value});const data=await r.json();if(!r.ok)throw new Error(data.detail);
      renderReferences(data);
    }
    file.value='';status.textContent='一覧に追加しました。候補の声を試せます。';
  }
  file.onchange=()=>upload(file.files).catch(e=>status.textContent=e.message);
  drop.ondragover=e=>e.preventDefault();drop.ondrop=e=>{e.preventDefault();void upload(e.dataTransfer.files).catch(error=>status.textContent=error.message);};
  async function prepare(){
    if(!draft.tts.enabled&&!allowDisabled)throw new Error('音声をオンにしてください。');
    const r=await api(prefix+'/candidate',{voice,tts:draft.tts,candidate_id:candidateId});candidateId=r.candidate_id;voice={...r.voice};description.value=voice.description;
  }
  const signature=()=>JSON.stringify({voice,tts:draft.tts,references:references.map(c=>c.id)});
  if(pageSave)root.settingsValue=signature;
  const preview={text:result.preview_text??'',expression:'neutral'};
  field(editor,'試しに読む文',preview.text,v=>preview.text=v).maxLength=300;
  const expressionSwitch=node('div',undefined,'voice-expressions');expressionSwitch.setAttribute('role','group');expressionSwitch.setAttribute('aria-label','試し読みの表情');editor.append(node('p','試し読みの表情','small muted'),expressionSwitch);
  for(const [value,title]of Object.entries({neutral:'ふつう',smile:'笑顔',shy:'照れ',surprised:'驚き',sad:'悲しい',troubled:'困った',thinking:'考え中',pout:'すねる'})){const button=action(expressionSwitch,title,()=>{preview.expression=value;for(const b of expressionSwitch.children)b.setAttribute('aria-pressed',String(b===button));});button.setAttribute('aria-pressed',String(value==='neutral'));}
  const previewActions=node('div',undefined,'row voice-preview-actions');editor.append(previewActions);
  action(previewActions,'声を試す',async()=>{
    await prepare();await play(prefix+'/preview',{...preview,candidate_id:candidateId},text=>{status.textContent=text;if(text==='再生中')previewed=signature();},response=>{if(response.headers.get('X-Voice-Sample')==='ready')sample.textContent='作り直す';});
  });
  action(previewActions,'止める',()=>{stopVoice();status.textContent='';});
  editor.append(node('p','気に入るまで試してから決めてください。','small muted'));
  const decideVoice=async()=>{
    await prepare();
    await api('/api/settings',{tts:draft.tts},'PUT');
    voice=(await api(prefix,{...voice,candidate_id:candidateId,apply_references:true},'PUT')).voice;
    appliedTts=structuredClone(draft.tts);decided=signature();configureVoice(appliedTts);status.textContent='この声に決めました。';
  };
  const decide=action(editor,'この声で決める',decideVoice,true);
  decide.hidden=pageSave;
  root.append(status);mounted=true;showMethod();showEditor();
  if(result.warning)root.append(node('p',result.warning,'warn'));
  // General settings saves only playback switches, never the pending voice or engine fields.
  let initialVoice=JSON.stringify({voice,references:references.map(c=>c.id)});
  const saveForSettings=async()=>{
    if(!pageSave)return {...appliedTts,enabled:draft.tts.enabled,autoplay:draft.tts.autoplay,autoplay_proactive:!!draft.tts.autoplay_proactive};
    if(JSON.stringify({voice,references:references.map(c=>c.id)})!==initialVoice){
      await prepare();
      await api('/api/settings',{tts:draft.tts},'PUT');
      voice=(await api(prefix,{...voice,candidate_id:candidateId,apply_references:true},'PUT')).voice;
      appliedTts=structuredClone(draft.tts);decided=signature();configureVoice(appliedTts);
      initialVoice=JSON.stringify({voice,references:references.map(c=>c.id)});
    }
    setVolume(playbackVolume);return draft.tts;
  };
  saveForSettings.method=()=>voice.mode;
  saveForSettings.decide=decideVoice;
  saveForSettings.renderStep=(parent,step)=>{
    parent.append(root);for(const child of root.children)child.hidden=true;for(const child of editor.children)child.hidden=true;for(const child of external.children)child.hidden=true;
    const reveal=e=>{for(let at=e;at&&at!==root;at=at.parentElement)at.hidden=false;};
    if(step==='autoplay')reveal(autoplayToggle);
    else if(step==='engine')reveal(engineChoice);
    else if(step==='download')reveal(downloads);
    else if(step==='method')reveal(methodChoice);
    else if(step==='description'||step==='reference'){
      for(const child of editor.children)child.hidden=true;
      reveal(step==='description'?design:clone);sample.hidden=true;
    }else if(['url','model','name','style'].includes(step)){
      for(const child of external.children)child.hidden=true;
      reveal(({url:urlInput,model:modelInput,name:nameInput,style:styleInput})[step].parentElement);
    }else if(step==='confirm'){
      for(const child of editor.children)child.hidden=[methodInput.parentElement,methodChoice,design,clone,decide].includes(child);
      reveal(editor);reveal(volumeInput.parentElement);
    }
    reveal(status);
  };
  saveForSettings.snapshot=signature;
  saveForSettings.summary=()=>({voice,references,ready});
  saveForSettings.previewed=()=>previewed===signature();
  saveForSettings.ready=()=>draft.tts.engine==='external'||ready;
  saveForSettings.decided=()=>decided===signature();return saveForSettings;
}
