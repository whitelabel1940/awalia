import {telemetryConsent} from './telemetry.js';
import {summaryRow,returnToSettings,embeddingIdentity,reindexAI} from './settings-entry.js';
import {openRouterLogin} from './openrouter-login.js';
import {api,node,field,shell,action,confirmDialog} from './ui.js';
import {sequence,explanation,chapters,choiceCards,onboardingChrome,notice,partnerRoute,setupHelper} from './onboarding.js';
import {modelRoleFields} from './model-role.js';
import {embeddingDownload} from './embedding-download.js';

export const roleNames={actor:'会話',judge:'判断',steward:'記憶の整理',embedding:'記憶の検索',research:'調べもの',vision:'画像の読み取り'};
export function modelLabel(model,cfg,role='actor'){if(!model)return '使わない';const choices=[...Object.values(cfg.onboarding.actor_choices).flat(),...(cfg.model_candidates?.actor||cfg.simple.actor_candidates||[])];const match=choices.find(c=>c.model===model);if(role==='actor'&&match)return match.label;const [service,...parts]=model.split('/');return `${({openrouter:'OpenRouter',openai:'OpenAI',gemini:'Gemini',anthropic:'Claude',local:'ローカル'})[service]||service} の ${parts.at(-1)}`;}
export function requiredProviders(models,includeSTT=false){return [...new Set(Object.entries(models).filter(([r,m])=>(includeSTT||r!=='stt')&&m).map(([,m])=>m.split('/')[0]))].filter(p=>p!=='local');}
export async function onboardingAI(parent,{partnerOnly=false,first=!partnerOnly,onWelcome,repair=false,settingsEntry=false}={}){
  let cfg=await api('/api/settings'),state=await api('/api/onboarding/state');
  const draft={method:'oauth',combination:'mixed',kind:'router',zdr:cfg.llm.zdr_only,models:{...cfg.models},simple:{...cfg.simple},keys:{},urls:{}};
  if(settingsEntry){draft.method=cfg.llm.mode==='simple'?'oauth':'key';draft.combination=cfg.llm.mode==='simple'?'mixed':cfg.advanced_presets.find(p=>Object.entries(p.models).every(([r,m])=>cfg.models[r]===m))?.id||'custom';}
  const initial=JSON.stringify(draft);
  const showConsent=!state.consent;
  let consent={agreed:()=>true,save:async()=>{}},consentReady=()=>{};
  let stopLogin,oauthReady=cfg.keys.openrouter==='set',downloadReady=false;
  let simple=cfg.llm.mode==='simple',downloads,flow,needsAI=repair||!cfg.ai_ready||!cfg.connection_verified;
  const preset=id=>cfg.advanced_presets.find(p=>p.id===id)?.models;
  const choosePreset=id=>{draft.models={...(preset(id)||cfg.models)};if(id==='mixed')draft.simple={actor:draft.models.actor,judge:draft.models.judge};if(id==='gemini')draft.models.steward=draft.models.judge;};
  const descriptions={oauth:'OpenRouter の画面で許可するだけ。キーのコピーは要りません。',key:'各社のサイトで発行したキーを貼り付けます。',mixed:'会話と判断に、それぞれ向いた AI を使います。',openai:'OpenAI のキーでまとめて使います。',gemini:'Gemini のキーでまとめて使います。',claude:'Claude とローカルの記憶検索を使います。',custom:'役割ごとに使うサービスとモデルを選びます。',router:'ひとつのキーで複数の AI を使います。',direct:'それぞれのサービスに直接つなぎます。'};
  function choice(body,title,options,key){choiceCards(body,title,options.map(([v,l])=>[v,l,descriptions[v]]),draft[key],v=>draft[key]=v);}
  function modelRole(body,role){
    const value=draft.models[role]||'',parts=value.split('/');let provider=parts.shift()||'gemini',model=parts.join('/');
    const providers={gemini:'Gemini',openai:'OpenAI',anthropic:'Claude',openrouter:'OpenRouter',local:'ローカルモデル'};
    for(const p of Object.keys(cfg.llm.providers||{}))if(!providers[p])providers[p]=p;
    if(role==='embedding')delete providers.anthropic;
    if(role==='vision')providers.none='使わない';
    if(settingsEntry&&role==='stt'){for(const p of Object.keys(providers))if(!['gemini','openai','openrouter','local'].includes(p))delete providers[p];providers.none='使わない';}
    const valueState={provider:value?provider:((role==='vision'||settingsEntry&&role==='stt')?'none':'gemini'),model};
    modelRoleFields(body,(settingsEntry&&role==='vision'?'写真の読み取り':roleNames[role])||({steward:'記憶の整理',stt:'文字起こし'})[role],providers,valueState,({provider,model})=>{
      draft.models[role]=provider==='none'?'':`${provider}/${model.trim()}`;
      if(role==='judge'&&!settingsEntry)draft.models.steward=draft.models.judge;
    },(p,title,options,value,change)=>choiceCards(p,title,Object.entries(options).map(([v,l])=>[v,l,`${l} のモデルを使います。`]),value,change),{onboarding:true,examples:p=>{const candidate=cfg.advanced_presets.map(x=>x.models[role]).find(m=>m?.startsWith(p+'/'));return candidate?.slice(p.length+1)||'';}});
  }
  const settingsPayload=(providers={})=>({llm:{mode:simple?'simple':'advanced',zdr_only:draft.zdr,...(Object.keys(providers).length?{providers}:{})},...(simple?{simple:draft.simple}:{models:draft.models})});
  async function save(){
    const providers={};
    for(const provider of requiredProviders(draft.models,settingsEntry)){
      if(draft.keys[provider]?.trim()){await api('/api/settings/keys',{provider,key:draft.keys[provider].trim()},'PUT');draft.keys[provider]='';cfg.keys[provider]='set';}
      if(draft.urls[provider])providers[provider]={api:'chat',base_url:draft.urls[provider]};
    }
    if(draft.urls.local)providers.local={api:'chat',base_url:draft.urls.local};
    cfg=await api('/api/settings',settingsPayload(providers),'PUT');
  }
  const copyScreen=(id,chapter='AI')=>({id,chapter,explanation:true,render:body=>explanation(body,id)});
  const screens=[
    // The first screen carries the consent too, so starting is one press.
    {id:'welcome',label:showConsent?'同意してはじめる':'はじめる',pending:showConsent,enter:({setReady})=>{consentReady=setReady;setReady(!showConsent||consent.agreed());},render:async body=>{body.classList.add('welcome');const logo=node('div',undefined,'welcome-logo');for(const theme of ['light','dark']){const image=node('img',undefined,`brand-${theme}`);image.src=`/static/brand/vertical-${theme}.svg`;image.alt=theme==='light'?'Awalia ふたりのあわいに、絆を育む。':'';if(theme==='dark')image.setAttribute('aria-hidden','true');logo.append(image);}body.append(logo);if(showConsent){const box=node('div',undefined,'welcome-consent');body.append(box);const copy=await explanation(box,'consent');consent=await telemetryConsent(box,{onChange:ready=>consentReady(ready)});return copy;}},next:async copy=>{if(showConsent&&!state.consent){if(!consent.agreed())return false;await consent.save();state=await api('/api/onboarding/consent',{version:copy.version});}return 'ai';}},
    {...copyScreen('ai'),render:async body=>{const copy=await explanation(body,'ai');if(!settingsEntry)setupHelper(body,{describe:false});return copy;},next:()=>cfg.ai_ready&&cfg.connection_verified?'summary':'method'},
    {id:'method',render:body=>choice(body,'設定のしかた',[['oauth','かんたん（OpenRouter と連携）'],['key','API キーで設定']],'method'),next:()=>draft.method==='oauth'?'oauth':'combination'},
    {id:'oauth',back:'method',pending:true,render:body=>{
      body.append(node('h2','OpenRouter と連携する'),node('p','OpenRouter のアカウントと、購入済みのクレジットが必要です。まだの時は、先に用意してから押してください。'),node('p','ブラウザで連携したら、この画面で次へ進めます。'));
      const credits=node('p',undefined,'small muted'),link=node('a','アカウント作成・クレジットの購入（OpenRouter）');link.href='https://openrouter.ai/settings/credits';link.target='_blank';link.rel='noopener noreferrer';credits.append(link);body.append(credits);
    },enter:({body,setReady})=>{stopLogin?.();setReady(oauthReady);let status;
      const login=action(body,'OpenRouter と連携する',async()=>{try{simple=true;choosePreset('mixed');if(!settingsEntry)await api('/api/settings',{llm:{mode:'simple'},simple:{actor:preset('mixed').actor,judge:preset('mixed').judge}},'PUT');stopLogin?.();stopLogin=await openRouterLogin(body,updated=>{cfg=updated;oauthReady=true;login.disabled=true;status.update('連携できました。');setReady(true);},()=>{status.update('ブラウザでの連携を待っています… うまくいかない時や、ブラウザの画面を閉じた時は、もう一度押せます。','waiting');},error=>status.update(error.message,'error'));}catch(error){status.update(error.message,'error');}});const connect=login.onclick;login.onclick=async()=>{await connect();login.disabled=oauthReady;};login.disabled=oauthReady;status=notice(body,oauthReady?'連携できました。':'');
    },next:async()=>{stopLogin?.();if(!simple){simple=true;choosePreset('mixed');if(!settingsEntry)await save();}return settingsEntry?'entry-summary':'zdr';}},
    {id:'combination',render:body=>choice(body,'AI の組み合わせ',[['mixed','おすすめの組み合わせ'],['openai','OpenAI だけ'],['gemini','Gemini だけ'],['claude','Claude だけ'],['custom','全部自分で決める']],'combination'),next:()=>{simple=draft.combination==='mixed';if(draft.combination==='custom'){draft.models=Object.fromEntries(Object.entries(cfg.advanced_models).map(([r,m])=>[r,['actor','judge','embedding','research','vision'].includes(r)&&!(r==='embedding'&&m?.startsWith('local/'))?(m?m.split('/')[0]+'/':''):m]));draft.models.steward=draft.models.judge;return 'role-actor';}choosePreset(draft.combination);if(simple)return 'kind';const choices=cfg.onboarding.actor_choices[draft.combination]||[];if(choices.length)draft.models.actor=choices[0].model;return choices.length>1?'actor':draft.models.embedding.startsWith('local/')?'download':'keys';}},
    {id:'kind',render:body=>choice(body,'使うキー',[['router','OpenRouter のキー1つ'],['direct','OpenAI と Gemini のキー2つ']],'kind'),next:()=>{simple=draft.kind==='router';choosePreset(simple?'mixed':'recommended_direct');return 'keys';}},
    {id:'actor',render:body=>{const choices=cfg.onboarding.actor_choices[draft.combination]||[];choiceCards(body,'会話のモデル',choices.map(c=>[c.model,c.label,`${c.description}\n${c.model.split('/').slice(1).join('/')}`]),draft.models.actor,v=>draft.models.actor=v);},next:()=>draft.models.embedding.startsWith('local/')?'download':'keys'},
    ...['actor','judge','embedding','research','vision'].map((role,index,roles)=>({id:`role-${role}`,render:body=>{
      if(role!=='embedding'){modelRole(body,role);return;}
      choiceCards(body,'記憶の検索',[['service','AI のサービスを使う','使うサービスのモデルを選びます。'],['local','ローカルモデルを使う（約140MB の取得）','キーを使わず、このパソコンで検索します。']],draft.models.embedding.startsWith('local/')?'local':'service',v=>{draft.models.embedding=v==='local'?'local/ruri-v3-130m':'gemini/';void flow.start('role-embedding');});
      if(!draft.models.embedding.startsWith('local/'))modelRole(body,role);
    },next:()=>index<roles.length-1?`role-${roles[index+1]}`:draft.models.embedding.startsWith('local/')?'download':'keys'})),
    {id:'download',pending:true,render:()=>{},next:()=> 'keys',enter:async({go,active,body,setReady})=>{
      let initial=true;downloadReady=false;
      downloads=embeddingDownload(body,()=>draft.models.embedding,{onboarding:true,onReady:async()=>{downloadReady=true;setReady(true);if(initial&&active())await go('keys',{remember:false});}});
      await downloads.refresh();initial=false;setReady(downloadReady);
    }},
    {id:'keys',render:body=>{body.append(node('h2','使うサービスのキー'));
      for(const provider of requiredProviders(draft.models,settingsEntry)){
        const input=field(body,`${({gemini:'Gemini',openai:'OpenAI',anthropic:'Claude',openrouter:'OpenRouter'})[provider]||provider} の API キー${cfg.keys[provider]==='set'?'（保存済み・変更する時だけ入力）':''}`,draft.keys[provider]||'',v=>draft.keys[provider]=v);input.type='password';input.autocomplete='off';input.maxLength=4096;
        const url={openrouter:'https://openrouter.ai/keys',openai:'https://platform.openai.com/api-keys',gemini:'https://aistudio.google.com/apikey',anthropic:'https://console.anthropic.com/settings/keys'}[provider];if(url){const note=node('p',undefined,'small muted'),link=node('a','キーを発行する');link.href=url;link.target='_blank';link.rel='noopener noreferrer';note.append(link);body.append(note);}
        if(!['gemini','openai','anthropic','openrouter'].includes(provider))field(body,'接続先の URL',draft.urls[provider]||cfg.llm.providers[provider]?.base_url||'',v=>draft.urls[provider]=v);
      }
      if(Object.entries(draft.models).some(([role,m])=>!['embedding','stt'].includes(role)&&m?.startsWith('local/')))field(body,'ローカルモデルの URL',draft.urls.local||cfg.llm.providers.local?.base_url||'',v=>draft.urls.local=v);
    },next:async()=>{if(settingsEntry)return 'entry-summary';await save();return Object.values(draft.models).some(m=>m?.startsWith('openrouter/'))?'zdr':'check';}},
    {id:'zdr',render:body=>{const options=[[false,'限定しない','つながりやすさを優先して使います。'],[true,'保存しない提供元だけを使う','内容を保存しないと申告している提供元に限定します。']];if(draft.zdr)options.reverse();choiceCards(body,'会話を保存しない提供元だけを使う？',options,draft.zdr,v=>draft.zdr=v);},next:async()=>{if(settingsEntry)return 'entry-summary';await api('/api/settings',{llm:{zdr_only:draft.zdr}},'PUT');return 'check';}},
    {id:'check',pending:true,back:'keys',next:()=> 'summary',render:body=>body.append(node('h2','接続の確認')),enter:async({active,body,setReady})=>{const waiting=node('p','選んだ AI に、会話・判断・記憶の整理・記憶の検索の4つの役で短い確認を送っています。ふつうは数秒〜30秒ほどです。','progress working');waiting.setAttribute('role','status');body.append(waiting);try{await api('/api/settings/connection',{});cfg=await api('/api/settings');if(active()){waiting.remove();notice(body,'接続の確認が完了しました。');setReady(true);}}catch(error){if(active()){waiting.remove();body.querySelector('h2').textContent='つながりませんでした';notice(body,error.message,'error');if(!settingsEntry)setupHelper(body);}}}},
    {id:'summary',back:()=>{needsAI=true;draft.method='key';return 'keys';},label:'次へ',render:body=>{body.append(node('h2','使うモデルの確認'));for(const [role,title]of Object.entries(roleNames))body.append(node('p',`${title}: ${modelLabel(cfg.models[role],cfg,role)}${role==='actor'?'（'+cfg.models[role].split('/').at(-1)+'）':''}`));},next:async()=>{if(repair)return 'done';await api('/api/setup/voice-step',{skipped:true});return 'partner';}},
    {...copyScreen('partner','パートナー'),next:()=> 'done'}
  ];
  if(settingsEntry){
    const summary={id:'entry-summary',pending:true,label:'つながるか確かめて保存',backAction:()=>returnToSettings('ai'),render:body=>{
      body.append(node('h2','今の AI のつなぎ方'));const list=node('div',undefined,'onboarding-summary');body.append(list);
      const row=(title,value,id)=>summaryRow(list,title,value,()=>flow.go(id));
      row('設定のしかた',draft.method==='oauth'?'かんたん':'キー','method');row('AI の組み合わせ',draft.combination==='custom'?'カスタム（役ごとに指定）':({mixed:'おすすめの組み合わせ',openai:'OpenAI だけ',gemini:'Gemini だけ',claude:'Claude だけ',recommended_direct:'おすすめの組み合わせ'})[draft.combination]||draft.combination,'combination');
      row('会話のモデル',modelLabel(draft.models.actor,cfg),'entry-actor');row('会話を保存しない提供元',draft.zdr?'保存しない提供元だけ':'限定しない','zdr');
      if(draft.method==='key')row('使うサービスのキー',requiredProviders(draft.models,settingsEntry).map(p=>`${({openrouter:'OpenRouter',openai:'OpenAI',gemini:'Gemini',anthropic:'Claude'})[p]||p}: ${draft.keys[p]||cfg.keys[p]==='set'?'設定済み':'未設定'}`).join(' / '),'keys');
      else summaryRow(list,'OpenRouter',oauthReady?'つながっています':'未設定',()=>{oauthReady=false;return flow.go('oauth');},'つなぎ直す');
    },enter:({body,setReady})=>{body.nextElementSibling.querySelector('button').textContent='設定に戻る';setReady(JSON.stringify(draft)!==initial);},next:async()=>{
      const body=parent.querySelector('section'),status=notice(body,'つながるか確かめています…','waiting');
      try{const target=simple?'openrouter/google/gemini-embedding-2':draft.models.embedding,changed=embeddingIdentity(target)!==embeddingIdentity(cfg.models.embedding);if(changed&&!await confirmDialog('記憶の検索モデルを変更すると、保存済みの記憶の索引を作り直します。完了するまで記憶の検索を休みます。'))return false;if(draft.models.embedding?.startsWith('local/'))await embeddingDownload(body,()=>draft.models.embedding).requireReady();if(draft.models.stt?.startsWith('local/'))await embeddingDownload(body,()=>draft.models.stt,{endpoint:'/api/stt'}).requireReady();const providers={};for(const [p,url]of Object.entries(draft.urls))if(url)providers[p]={api:'chat',base_url:url};await api('/api/settings/connection',{settings:settingsPayload(providers),keys:Object.fromEntries(Object.entries(draft.keys).filter(([,v])=>v.trim()).map(([p,v])=>[p,v.trim()]))});await save();if(changed&&cfg.reindex_required!==false)await reindexAI(text=>status.update(text,'waiting'));returnToSettings('ai',true);}catch(error){status.update(error.message,'error');}return false;
    }};
    screens.unshift(summary);
    screens.push({id:'entry-actor',back:'entry-summary',render:body=>{if(simple){field(body,'会話のモデル',draft.simple.actor,v=>{draft.simple.actor=v;draft.models.actor=v;});}else modelRole(body,'actor');},next:()=>requiredProviders(draft.models,settingsEntry).some(p=>cfg.keys[p]!=='set'&&!draft.keys[p])?'keys':'entry-summary'});
    screens.push({id:'entry-custom',back:'entry-summary',render:body=>{for(const role of ['actor','judge','steward','embedding','vision','stt','research'])modelRole(body,role);downloads=embeddingDownload(body,()=>draft.models.embedding);void downloads.refresh();const sttDownload=embeddingDownload(body,()=>draft.models.stt||'',{endpoint:'/api/stt',title:'文字起こしのモデル',label:'文字起こしのモデル'});void sttDownload.refresh();},next:()=> 'keys'});
    for(const screen of screens){if(['welcome','ai','partner','summary','check','entry-summary'].includes(screen.id))continue;screen.back='entry-summary';if(screen.id==='combination'){const next=screen.next;screen.next=()=>draft.combination==='custom'?(simple=false,'entry-custom'):next();}}
  }
  function progressFor(screen){
    if(settingsEntry)return {current:1,total:1};
    if(screen.id==='partner')return {current:1,total:partnerRoute().length};
    // The cover shows no count, so the chapter starts at its explanation.
    const route=['ai'];
    if(needsAI){
      route.push('method');
      if(draft.method==='oauth')route.push('oauth','zdr');
      else {
        route.push('combination');
        if(draft.combination==='mixed')route.push('kind');
        else if(draft.combination==='custom')route.push(...['actor','judge','embedding','research','vision'].map(r=>`role-${r}`));
        else if((cfg.onboarding.actor_choices[draft.combination]||[]).length>1)route.push('actor');
        if(draft.models.embedding?.startsWith('local/'))route.push('download');
        route.push('keys');if(requiredProviders(draft.models,settingsEntry).includes('openrouter'))route.push('zdr');
      }
      route.push('check');
    }
    route.push('summary');return {current:route.indexOf(screen.id)+1,total:route.length};
  }
  shell('settings');
  onboardingChrome();
  if(document.getElementById('heading'))document.getElementById('heading').textContent='はじめに';
  if(document.getElementById('subtitle'))document.getElementById('subtitle').textContent='ひとつずつ、準備していきましょう。';
  return new Promise((resolve,reject)=>{
    flow=sequence(parent,partnerOnly?[screens.at(-1)]:screens,{setupHelp:!settingsEntry,telemetryFirst:settingsEntry?null:first,showCount:!settingsEntry,progressFor,visibleChapters:settingsEntry?['AI']:partnerOnly?chapters.filter(c=>!['AI','声で入力','バックアップ'].includes(c)):chapters,onDone:resolve,onWelcome});
    flow.start(settingsEntry?'entry-summary':repair?'method':partnerOnly?'partner':undefined).catch(reject);
  });
}
