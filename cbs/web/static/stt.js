import {accordion,choiceCards} from './onboarding.js';
import {api,node,field,select,icon} from './ui.js';

// Capture PCM in memory so all three transports and Haas-ja accept the same WAV.
export function wavPCM(chunks, rate, maximum){
  const length=chunks.reduce((n,c)=>n+c.length,0),source=new Float32Array(length);let at=0;
  for(const chunk of chunks){source.set(chunk,at);at+=chunk.length;}
  const count=Math.min(Math.floor(length*16000/rate),maximum*16000),buffer=new ArrayBuffer(44+count*2),v=new DataView(buffer);
  const str=(at,s)=>{for(let i=0;i<s.length;i++)v.setUint8(at+i,s.charCodeAt(i));};
  str(0,'RIFF');v.setUint32(4,36+count*2,true);str(8,'WAVE');str(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,16000,true);v.setUint32(28,32000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);str(36,'data');v.setUint32(40,count*2,true);
  for(let i=0;i<count;i++){const start=Math.floor(i*rate/16000),end=Math.max(start+1,Math.floor((i+1)*rate/16000));let sum=0;for(let j=start;j<end;j++)sum+=source[j]||0;v.setInt16(44+i*2,Math.round(Math.max(-1,Math.min(1,sum/(end-start)))*32767),true);}
  let binary='';for(const byte of new Uint8Array(buffer))binary+=String.fromCharCode(byte);return btoa(binary);
}

export function microphone({button,status,input,composer,stopVoice,onError,active,onLock=()=>{},openTalk=async()=>true,request=api}){
  if(!button||!status)return {configure(){},setBusy(){},cancel(){},get active(){return false;}};
  let cfg={},busy=false,phase='idle',generation=0,stream,context,source,processor,gain,timer,chunks=[],started,lastSound,samples=0,abort,held=false,ptt=false;
  function paint(){
    const supported=!!navigator.mediaDevices?.getUserMedia&&!!(window.AudioContext||window.webkitAudioContext);
    button.disabled=busy||!cfg.enabled||!supported||phase==='transcribing'||phase==='permission';
    button.replaceChildren(icon(phase==='recording'?'stop':'mic'));button.setAttribute('aria-label',phase==='recording'?'録音を止める':'マイクで話す');button.setAttribute('aria-pressed',String(phase==='recording'));
    if(phase==='idle')status.textContent=!supported?'この画面ではマイクを使えません。localhostのブラウザか常駐の窓を使ってください。':cfg.reason||'';
  }
  function release(){
    clearInterval(timer);timer=null;
    if(processor){processor.onaudioprocess=null;processor.disconnect();}source?.disconnect();gain?.disconnect();
    stream?.getTracks().forEach(track=>track.stop());void context?.close();stream=context=source=processor=gain=null;
  }
  function cancel(){generation++;abort?.abort();abort=null;release();chunks=[];phase='idle';ptt=false;onLock(false);paint();}
  async function finish(){
    if(phase!=='recording')return;
    const own=generation,rate=context.sampleRate,audio=wavPCM(chunks,rate,cfg.max_seconds);chunks=[];release();phase='transcribing';status.textContent='文字起こししています… · Escで取り消し';paint();
    abort=new AbortController();
    try{
      const result=await request('/api/stt/transcribe',{audio},'POST',abort.signal);
      if(own!==generation||!active())return;
      if(result.text){const start=input.selectionStart??input.value.length,end=input.selectionEnd??start;input.value=input.value.slice(0,start)+result.text+input.value.slice(end);input.selectionStart=input.selectionEnd=start+result.text.length;input.dispatchEvent(new window.Event('input',{bubbles:true}));input.focus();}
      phase='idle';onLock(false);paint();status.textContent=result.text?'文字起こしを入力しました。確認して送れます。':'声が聞き取れませんでした。もう一度話してください。';
      if(result.text&&result.auto_send)composer.requestSubmit();
    }catch(error){if(own===generation){phase='idle';onLock(false);paint();if(error.name!=='AbortError')onError(error);}}
    finally{if(own===generation){phase='idle';abort=null;onLock(false);paint();}}
  }
  async function start(push=false){
    if(phase!=='idle'||busy||!cfg.enabled||!active())return;
    const own=++generation;ptt=push;phase='permission';onLock(true);stopVoice();paint();
    try{
      if(!cfg.notice_seen){
        status.textContent='声は文字起こしのモデルの提供元に送られます。文字起こし後、音声はこの端末に保存しません。';
        const notice=document.createElement('p');notice.className='small muted';notice.textContent=status.textContent;status.after(notice);
        // Acknowledgement adds no foreground wait before capture.
        cfg.notice_seen=true;void request('/api/stt/notice',{}).catch(()=>{cfg.notice_seen=false;});
      }
      if(own!==generation||push&&!held)return;
      const acquired=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
      if(own!==generation||push&&!held||!active()){acquired.getTracks().forEach(t=>t.stop());return;}
      stream=acquired;context=new (window.AudioContext||window.webkitAudioContext)();await context.resume();
      if(own!==generation)return;
      source=context.createMediaStreamSource(stream);processor=context.createScriptProcessor(4096,1,1);gain=context.createGain();gain.gain.value=0;
      chunks=[];samples=0;started=lastSound=performance.now();phase='recording';
      processor.onaudioprocess=e=>{
        if(phase!=='recording')return;
        const data=e.inputBuffer.getChannelData(0),remaining=Math.max(0,Math.floor(cfg.max_seconds*context.sampleRate)-samples),copy=new Float32Array(data.subarray(0,remaining));
        chunks.push(copy);samples+=copy.length;let sum=0;for(const value of copy)sum+=value*value;
        if(Math.sqrt(sum/Math.max(1,copy.length))>=cfg.silence_rms)lastSound=performance.now();
        if(samples>=cfg.max_seconds*context.sampleRate)void finish();
      };
      source.connect(processor);processor.connect(gain);gain.connect(context.destination);paint();
      const tick=()=>{if(phase!=='recording')return;const now=performance.now();status.textContent=`録音中 ${Math.floor((now-started)/1000)}秒 / ${cfg.max_seconds}秒 · ${push?'キーを離して停止':'マイクをもう一度押すか無音で停止'} · Escで取り消し`;if(now-started>=cfg.max_seconds*1000||!ptt&&now-lastSound>=cfg.silence_seconds*1000)void finish();};
      timer=setInterval(tick,100);tick();
      for(const track of stream.getTracks())track.onended=()=>{if(phase==='recording')cancel();};
    }catch(error){
      if(own===generation){cancel();const denied=['NotAllowedError','SecurityError'].includes(error.name);status.textContent=denied?'マイクが許可されていません。ブラウザのサイト設定、Windowsの「プライバシーとセキュリティ → マイク」で許可して再試行してください。':'マイクを開始できません。接続と、ほかのアプリで使用中でないかを確認してください。';onError(new Error(status.textContent));}
    }finally{if(own===generation&&phase==='permission'){phase='idle';onLock(false);paint();}}
  }
  button.onclick=()=>phase==='recording'?finish():start();
  paint();
  window.addEventListener('keydown',e=>{if(e.key==='Escape'&&phase!=='idle'){e.preventDefault();cancel();status.textContent='録音を取り消しました。';}});
  window.addEventListener('pagehide',cancel);
  window.addEventListener('resident-visibility-change',()=>{if(document.hidden)cancel();});
  async function push(down){
    if(down===held)return;held=down;
    if(down){if(await openTalk()&&held)await start(true);}
    else if(ptt){if(phase==='recording')await finish();else if(phase==='permission')cancel();}
  }
  if(window.__TAURI__?.event){void (async()=>{await window.__TAURI__.event.listen('resident-ptt',e=>void push(e.payload));await push(await window.__TAURI__.core.invoke('resident_ptt_state'));})().catch(onError);}
  return {configure(value){cfg={max_seconds:60,silence_seconds:2,silence_rms:.015,...value};paint();},setBusy(value){busy=value;if(value&&phase!=='idle')cancel();paint();},cancel,get active(){return phase!=='idle';}};
}

// The onboarding chapter and settings share validation and configuration fields.
export function sttSettingsFields(parent,draft,{onboarding=false}={}){
  const cfg=draft.stt??={auto_send:false,max_seconds:60,silence_seconds:2,device:'auto'};
  const specs=[
    {title:'話し終えた後',summary:()=>cfg.auto_send?'自動で送る':'確かめてから送る',render:p=>onboarding?choiceCards(p,'話し終えた後',[['review','確かめてから送る','文字になった内容を確かめてから送ります。'],['send','自動で送る','話し終えたら、そのまま送ります。']],cfg.auto_send?'send':'review',v=>cfg.auto_send=v==='send'):select(p,'話し終えた後',{review:'確かめてから送る',send:'自動で送る'},cfg.auto_send?'send':'review',v=>cfg.auto_send=v==='send')},
    {title:'上限',summary:()=>`${cfg.max_seconds} 秒`,render:p=>{const e=field(p,'1回の録音の上限（秒）',cfg.max_seconds,v=>cfg.max_seconds=Number(v));e.type='number';e.min=1;e.max=120;}},
    {title:'止めるまでの無音時間',summary:()=>`${cfg.silence_seconds} 秒`,render:p=>{const e=field(p,'止めるまでの無音（秒）',cfg.silence_seconds,v=>cfg.silence_seconds=Number(v));e.type='number';e.min=.5;e.max=10;e.step=.5;}}
  ];
  if(onboarding)accordion(parent,specs,{activeIndex:0});
  else for(const spec of specs)spec.render(parent);
}
export function defaultSTTModel(cfg){
  const candidates=cfg.stt?.candidates||{};
  const providers=new Set(Object.entries(cfg.models||{}).filter(([role,m])=>role!=='stt'&&m).map(([,m])=>m).map(m=>m.split('/')[0]));
  const current=cfg.models?.stt;
  if(current&&!current.startsWith('local/')&&candidates[current]&&providers.has(current.split('/')[0]))return current;
  return Object.keys(candidates).find(m=>candidates[m]&&providers.has(m.split('/')[0]))||'local/haas-ja';
}
