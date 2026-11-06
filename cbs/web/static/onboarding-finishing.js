import {modelLabel} from './onboarding-ai.js';
import {summaryRow,returnToSettings} from './settings-entry.js';
import {api,node,field,select,action,face} from './ui.js';
import {sequence,explanation,chapters,choiceCards,notice,onboardingChrome} from './onboarding.js';
import {newLook,mountLook,saveLook,applyLookDraft,lookPayload,lookSnapshot,lookFieldsEmpty,applyLookDescription} from './look-editor.js';
import {voiceSettings,stopVoice} from './voice.js';
import {embeddingDownload} from './embedding-download.js';
import {defaultSTTModel,sttSettingsFields} from './stt.js';
import {backupSettings} from './backup.js';

// Every write uses saved-partner settings; the partner is already usable.
export async function onboardingFinishing(parent,{name,secondPartner=false,onBack,settingsEntry,lookOnly=false,lookState,describe=false,coverOnly=false,onLookDone}={}){
  let flow,cfg,look=lookState,voice,voiceHost,backup,backupHost,sttModel,serviceModel,initialVoice;
  const settings=async()=>cfg??=await api('/api/settings');
  const saveInput=()=>api('/api/settings',{stt:{auto_send:cfg.stt.auto_send,max_seconds:cfg.stt.max_seconds,silence_seconds:cfg.stt.silence_seconds,device:cfg.stt.device},...(cfg.llm.mode==='simple'?{simple:{stt:sttModel}}:{models:{stt:sttModel}})},'PUT');
  const nextAfterVoice=secondPartner?'done':'voice_input';
  async function voiceStep(skipped){stopVoice();await api('/api/setup/voice-step',{skipped});return nextAfterVoice;}
  async function ensureLook(){if(!look)look=newLook(await api('/api/look'));return look;}
  async function ensureVoice(){
    await settings();if(!settingsEntry)cfg.tts.autoplay_proactive=false;
    if(!voice){voiceHost=node('div');parent.append(voiceHost);voice=await voiceSettings(voiceHost,cfg,{onboarding:true,allowDisabled:!!settingsEntry});initialVoice=voice.snapshot();}
    return voice;
  }
  const copy=(id,chapter,next,label,skip)=>({id,chapter,explanation:true,label,render:p=>explanation(p,id,name),next,skip});
  const afterFaces=()=>look.ownedImages?'look-cover-import':'look-cover';
  async function persistLook(){look.pending=(look.pending||0)+1;try{await saveLook(look);}finally{look.pending--;}}
  async function describeLook(){
    look.pending=(look.pending||0)+1;
    try{const result=await api('/api/look/describe',lookPayload(look));
      if(!result.filled)throw new Error(result.message);
      applyLookDescription(look,result);return result;
    }finally{look.pending--;}
  }
  const lookPage=(id,title,view,next)=>({id,chapter:'見た目',render:async p=>{p.append(node('h2',title));await mountLook(p,await ensureLook(),{view,onboarding:true,onSingle:()=>{look.singlePath=true;return flow.go('look-single');},onSheet:()=>flow.go('look-sheet')});},next:async()=>{
    await look.previewReady;if(look.pending)throw new Error('画像の処理が終わるまでお待ちください。');
    if(look.reviewReturn)return 'look-confirm';
    await persistLook();
    if(view==='faces'){
      const hasFaces=look.slots.some(Boolean)||look.existing.some(e=>!look.removed.has(e));
      if(hasFaces&&lookFieldsEmpty(look)&&!look.offeredDescription)return 'look-describe';
      return afterFaces();
    }
    return next;
  }});
  const voicePage=(step,title,next)=>({id:'voice-'+step,chapter:'声',render:async p=>{p.append(node('h2',title));const editor=await ensureVoice();p.append(voiceHost);editor.renderStep(voiceHost,step);},next});
  const screens=[
    {backAction:()=>onBack?.(),...copy('look','見た目',async()=>{await ensureLook();if(look.ownedImages)look.image.use='sheet';return look.ownedImages?'look-faces':'look-style';},'次へ',()=> 'voice'),render:async p=>{
      await explanation(p,'look',name);await ensureLook();
      choiceCards(p,'見た目の整え方',[['create','これから見た目を決めて、画像を作る'],['import','もう画像がある']],look.ownedImages?'import':'create',v=>{look.ownedImages=v==='import';draft.hidden=look.ownedImages;},{recommended:null});
      const note=notice(p,''),draft=action(p,'設定文と性格から下書きする',async()=>{
        const next=p.nextElementSibling?.querySelector('.primary');if(next)next.disabled=true;
        look.pending=(look.pending||0)+1;
        try{const result=await api('/api/look/draft',{});
          if(!result.assignments.length){note.update('設定文や性格から読み取れる見た目がありませんでした。「次へ」から入力できます。');return;}
          applyLookDraft(look,result);await persistLook();note.update('下書きが完了しました。次へ進めます。');
        }catch(error){note.update(error.message,'error');}finally{look.pending--;if(next)next.disabled=false;}
      });draft.hidden=!!look.ownedImages;
    }},
    ...[['style','絵柄'],['profile','人物像'],['face','顔のパーツ'],['hair','髪型'],['outfit','服装']].map(([id,title],i,all)=>lookPage('look-'+id,title,id,i===all.length-1?'look-confirm':'look-'+all[i+1][0])),
    {id:'look-confirm',chapter:'見た目',render:async p=>{
      p.append(node('h2','見た目の確認'));const payload=lookPayload(await ensureLook()),catalog=await api('/api/look/fields');
      const list=node('div',undefined,'onboarding-summary');p.append(list);
      for(const spec of catalog.fields.filter(s=>s.tab!=='image'||(look.reviewReturn&&s.path.startsWith('person.')&&!s.path.startsWith('person.expressions.')))){
        const value=spec.path.split('.').reduce((o,k)=>o?.[k],payload);if(!value)continue;
        const row=node('div',undefined,'summary-row'),content=node('div');row.append(node('span',spec.title));content.append(node('span',value));
        action(content,'直す',()=>{if(look.reviewReturn&&spec.tab==='image'){field(content,spec.title,value,v=>look.person[spec.path.split('.').at(-1)]=v);}else return flow.go('look-'+spec.tab);}).classList.add('quiet');row.append(content);list.append(row);
      }
      if(!list.children.length)p.append(node('p','まだ決めていません'));
    },next:async()=>{await persistLook();const target=look.reviewReturn||'look-sheet';delete look.reviewReturn;return target;}},
    lookPage('look-sheet','表情シートを作る','sheet','look-faces'),
    lookPage('look-single','顔の画像1枚を作る','single-prompt','look-faces'),
    lookPage('look-faces','画像の取り込み・確認','faces','look-cover'),
    {id:'look-describe',chapter:'見た目',label:'今はしない',render:p=>{
      look.offeredDescription=true;
      p.append(node('h2','画像から見た目を書き起こす'),node('p',`見た目の欄がまだ空です。取り込んだ画像から書き起こすと、会話でも${name}がこの見た目に沿って話せます。`));
      const note=notice(p,'');action(p,'画像から書き起こす',async()=>{
        const next=p.nextElementSibling.querySelector('.primary');next.disabled=true;
        note.update('画像を読んでいます。','waiting');
        try{await describeLook();look.reviewReturn=afterFaces();await flow.go('look-confirm');}catch(e){note.update(e.message,'error');}finally{next.disabled=false;}
      });
    },next:()=>afterFaces()},
    {...lookPage('look-cover','扉絵を作る','cover-prompt','look-cover-import'),explanation:true,skip:()=> 'voice',render:async p=>{p.append(node('h2','扉絵を作る'),node('p','扉絵はなくても使えます。「飛ばす」で声の設定へ進めます。'));await mountLook(p,await ensureLook(),{view:'cover-prompt',onboarding:true});}},
    lookPage('look-cover-import','画像の取り込み','cover-import','voice'),
    {...copy('voice','声',async()=>{await settings();cfg.tts.enabled=true;cfg.tts.autoplay_proactive=false;return 'voice-autoplay';},'声の設定をする',()=>voiceStep(true)),skipLabel:'あとで'},
    voicePage('autoplay','返事を自動で読み上げる？',()=> 'voice-engine'),
    voicePage('engine','声のエンジン',async()=>cfg.tts.engine==='external'?'voice-url':voice.ready()?'voice-method':'voice-download'),
    {...voicePage('download','声に必要なものを取得する',()=> 'voice-method'),pending:true,enter:async({active,body,setReady})=>{
      const update=()=>{if(!active()||!body.isConnected)return;setReady(voice.ready());if(!voice.ready())setTimeout(update,750);};update();
    }},
    voicePage('method','声の作り方',()=>voice.method()==='clone'?'voice-reference':'voice-description'),
    voicePage('description','声の説明',()=> 'voice-confirm'),
    voicePage('reference','参照音声',()=> 'voice-confirm'),
    voicePage('url','外部の音声サーバー URL',()=> 'voice-model'),
    voicePage('model','モデル名',()=> 'voice-name'),
    voicePage('name','声の名前',()=> 'voice-style'),
    voicePage('style','抑揚',()=> 'voice-confirm'),
    {...voicePage('confirm','声を確かめる',async()=>{await voice.decide();return voiceStep(false);}),label:'この声で決める'},
    {...copy('voice_input','声で入力',async()=>{await settings();sttModel??=defaultSTTModel(cfg);return 'input-model';},'設定する',()=> 'export'),skipLabel:'あとで'},
    {id:'input-model',chapter:'声で入力',render:async p=>{
      await settings();if(settingsEntry&&!sttModel)sttModel=defaultSTTModel(cfg);sttModel??=defaultSTTModel(cfg);serviceModel=defaultSTTModel(cfg);
      const options=[];if(!serviceModel.startsWith('local/'))options.push(['service','AI のサービスを使う','今つながっている AI で声を文字にします。']);options.push(['local','ローカルモデル（Haas-ja）','このパソコンで声を文字にします。']);
      choiceCards(p,'使うモデル',options,sttModel.startsWith('local/')?'local':'service',v=>sttModel=v==='local'?'local/haas-ja':serviceModel);
    },next:()=>sttModel.startsWith('local/')?'input-download':'input-options'},
    {id:'input-download',chapter:'声で入力',pending:true,next:()=> 'input-device',render:p=>p.append(node('h2','声で入力に必要なものを取得する')),enter:async({body,go,active,setReady})=>{
      let initial=true;const download=embeddingDownload(body,()=>sttModel,{endpoint:'/api/stt',title:'声で入力の準備',label:'声で入力のモデル',parameters:()=>({device:cfg.stt.device||'auto'}),onboarding:true,onReady:async()=>{setReady(true);if(initial&&active())await go('input-device',{remember:false});}});await download.refresh();initial=false;
    }},
    {id:'input-device',chapter:'声で入力',render:p=>select(p,'装置',{auto:'自動（おすすめ）',gpu:'GPU',cpu:'CPU'},cfg.stt.device||'auto',v=>cfg.stt.device=v),next:()=> 'input-options'},
    {id:'input-options',chapter:'声で入力',render:p=>{p.append(node('h2','声で入力の設定'));sttSettingsFields(p,cfg,{onboarding:true});},next:()=> 'input-confirm'},
    {id:'input-confirm',chapter:'声で入力',render:p=>{p.append(node('h2','声で入力の確認'),node('p',`${sttModel.startsWith('local/')?'ローカルモデル（Haas-ja）':'AI のサービス'} / ${cfg.stt.auto_send?'自動で送る':'確かめてから送る'} / 上限 ${cfg.stt.max_seconds} 秒 / 無音 ${cfg.stt.silence_seconds} 秒`));},next:async()=>{
      await saveInput();return 'export';
    }},
    {...copy('export','バックアップ',async()=>{await settings();return cfg.backup_status.trial?'export-trial':'export-options';},'自動バックアップを設定する',()=> 'done'),skipLabel:'あとで'},
    {id:'export-trial',chapter:'バックアップ',render:p=>p.append(node('h2','バックアップ'),node('p','お試しではバックアップを行いません。設定は通常の起動後に行えます。','muted')),next:()=> 'done'},
    {id:'export-options',chapter:'バックアップ',render:p=>{p.append(node('h2','自動バックアップ'));if(!backup){backupHost=node('div');p.append(backupHost);backup=backupSettings(backupHost,cfg,{managed:true,onboarding:true});}else p.append(backupHost);},next:async()=>{await backup.save();return 'export-confirm';}},
    {id:'export-confirm',chapter:'バックアップ',render:p=>p.append(node('h2','バックアップの確認'),node('p',`${cfg.backup.folder} / ${{off:'しない',daily:'毎日',weekly:'毎週'}[cfg.backup.frequency]}`)),next:()=> 'done'},
    {id:'done',chapter:secondPartner?'声':'バックアップ',explanation:true,label:`${name}と話しはじめる`,render:async p=>{const saved=await api('/api/look');const portrait=face(name,saved.expressions?.includes('smile')?'smile':'neutral',saved.expressions||[]);if(!saved.expressions?.length){const symbol=node('img');symbol.src='/static/brand/symbol.svg';symbol.alt='';portrait.replaceChildren(symbol);}await explanation(p,'done',name);p.prepend(portrait);},next:()=> 'done'}
  ];
  if(settingsEntry){
    onboardingChrome();await settings();const initial=JSON.stringify({model:cfg.models.stt||'',device:cfg.stt.device});sttModel=cfg.models.stt||'';
    if(settingsEntry==='voice')await ensureVoice();
    const summary={id:'entry-summary',chapter:settingsEntry==='voice'?'声':'声で入力',pending:true,backAction:()=>{stopVoice();returnToSettings(settingsEntry);},label:settingsEntry==='voice'?'この声で決める':'この方法で決める',render:p=>{
      p.append(node('h2',settingsEntry==='voice'?'今の声':'今の文字起こしの方法'));const list=node('div',undefined,'onboarding-summary');p.append(list);
      const row=(title,value,id)=>summaryRow(list,title,value,()=>flow.go(id));
      if(settingsEntry==='voice'){const {voice:v,references,ready}=voice.summary();row('音声のエンジン',cfg.tts.engine==='external'?'外部の音声サーバー':'ローカルモデル（speech.cpp）','voice-engine');row('使うモデル（取得状態）',cfg.tts.engine==='external'?(cfg.tts.model||'外部のサービス'):`${({auto:'自動（GPUならq8、CPUならq4）',q8_0:'q8',q4_k:'q4'})[cfg.tts.local_model||'auto']||cfg.tts.local_model} · ${ready?'取得済み':'未取得'}`,cfg.tts.engine==='external'?'voice-model':'voice-download');row('声の作り方',v.mode==='clone'?'参照音声':'説明','voice-method');row(v.mode==='clone'?'参照音声':'声の説明',v.mode==='clone'?`${references.length}本・合計 ${references.reduce((n,c)=>n+c.seconds,0).toFixed(1)}秒`:v.description,v.mode==='clone'?'voice-reference':'voice-description');summaryRow(list,'試し聞き',voice.previewed()?'試聴済み':'まだ聞いていません',()=>flow.go('voice-confirm'),'声を確かめる');}
      else {row('文字起こしの方法（モデル）',!sttModel?'未設定':sttModel==='local/haas-ja'?'ローカルモデル（Haas-ja）':modelLabel(sttModel,cfg,'stt'),'input-model');summaryRow(list,'取得状態',sttModel.startsWith('local/')?'未取得':'サービスを使います',()=>flow.go('input-download'));row('使う装置',({auto:'自動（おすすめ）',gpu:'GPU',cpu:'CPU'})[cfg.stt.device||'auto']||cfg.stt.device,'input-device');}
    },enter:async({body,setReady})=>{body.nextElementSibling.querySelector('button').textContent='設定に戻る';if(settingsEntry==='voice')setReady(voice.snapshot()!==initialVoice&&voice.previewed()&&voice.ready());else {const changed=JSON.stringify({model:sttModel,device:cfg.stt.device})!==initial;if(sttModel.startsWith('local/')){const host=node('div');host.hidden=true;body.append(host);const download=embeddingDownload(host,()=>sttModel,{endpoint:'/api/stt',parameters:()=>({device:cfg.stt.device||'auto'}),onReady:()=>{const row=[...body.querySelectorAll('.summary-row')].find(r=>r.firstChild.textContent==='取得状態');if(row)row.children[1].firstChild.textContent='取得済み';setReady(changed);}});await download.refresh();}else setReady(changed&&!!cfg.stt.candidates?.[sttModel]&&cfg.keys[sttModel.split('/')[0]]==='set');}},next:async()=>{if(settingsEntry==='voice'){await voice.decide();stopVoice();}else await saveInput();returnToSettings(settingsEntry,true);return false;}};
    const selected=screens.filter(s=>s.id.startsWith(settingsEntry==='voice'?'voice-':'input-'));
    for(const screen of selected){screen.back='entry-summary';if(['voice-description','voice-reference','voice-style','voice-confirm','input-device','input-options','input-confirm'].includes(screen.id))screen.next=()=> 'entry-summary';}
    if(settingsEntry==='input')selected.find(s=>s.id==='input-model').next=()=>sttModel.startsWith('local/')?'input-download':'entry-summary';
    flow=sequence(parent,[summary,...selected],{setupHelp:false,showCount:false,visibleChapters:[summary.chapter],onDone:()=>{}});await flow.start();return flow;
  }
  if(lookOnly){
    await ensureLook();
    const selected=screens.filter(s=>s.chapter==='見た目');
    for(const screen of selected){if(screen.skip)screen.skip=()=> 'done';if(screen.id==='look-cover-import'){const next=screen.next;screen.next=async()=>{await next();return 'done';};}}
    let start=coverOnly?'look-cover':'look',describeError;
    if(describe){
      selected.find(s=>s.id==='look-confirm').backAction=()=>onBack?.();
      notice(parent,'画像を読んでいます。','waiting');try{await describeLook();look.reviewReturn='done';start='look-confirm';}catch(e){describeError=e.message;}
    }
    // Count only the pages of the chosen route, as in onboarding (own images skip the field pages).
    const lookProgress=screen=>{
      if(screen.explanation)return {current:1,total:1};
      let route=selected.filter(s=>!s.explanation&&s.id!=='look-describe').map(s=>s.id);
      if(look?.ownedImages)route=['look-faces','look-cover-import'];else if(!look?.singlePath)route=route.filter(id=>id!=='look-single');
      return {current:Math.max(1,route.indexOf(screen.id)+1),total:route.length};
    };
    flow=sequence(parent,selected,{telemetryFirst:null,visibleChapters:['見た目'],progressFor:lookProgress,onDone:onLookDone});
    await flow.start(start);if(describeError)notice(parent.querySelector('section'),describeError,'error');
    return Object.assign(flow,{busy:()=>!!look.pending,save:()=>saveLook(look),snapshot:()=>lookSnapshot(look)});
  }
  const filtered=secondPartner?screens.filter(s=>!['声で入力','バックアップ'].includes(s.chapter)||s.id==='done'):screens;
  flow=sequence(parent,filtered,{telemetryFirst:!secondPartner,visibleChapters:secondPartner?chapters.filter(c=>!['AI','声で入力','バックアップ'].includes(c)):chapters,progressFor:screen=>{
    if(screen.explanation)return {current:1,total:1};
    let route=filtered.filter(s=>s.chapter===screen.chapter&&!s.explanation).map(s=>s.id);
    if(screen.chapter==='見た目'){route=route.filter(id=>id!=='look-describe');if(look?.ownedImages)route=['look-faces','look-cover-import'];else if(!look?.singlePath)route=route.filter(id=>id!=='look-single');}
    if(screen.chapter==='声')route=['voice-autoplay','voice-engine',...(cfg?.tts.engine==='external'?['voice-url','voice-model','voice-name','voice-style']:[...(!voice?.ready()?['voice-download']:[]),'voice-method',voice?.method()==='clone'?'voice-reference':'voice-description']),'voice-confirm'];
    if(screen.chapter==='声で入力')route=['input-model',...(sttModel?.startsWith('local/')?['input-device']:[]),'input-options','input-confirm'];
    if(screen.chapter==='バックアップ')route=cfg?.backup_status.trial?['export-trial']:['export-options','export-confirm'];
    return {current:Math.max(1,route.indexOf(screen.id)+1),total:route.length};
  },onDone:()=>{stopVoice();location.href='/#talk';}});
  await flow.start('look');return flow;
}
