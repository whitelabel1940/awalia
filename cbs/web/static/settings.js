import {settingsShelves} from './settings-shelves.js';
import {entryNotice,embeddingIdentity,reindexAI} from './settings-entry.js';
import {openRouterLogin} from './openrouter-login.js';
import {sttSettingsFields} from './stt.js';
import {modelRoleFields} from './model-role.js';
import {daysSettings} from './days.js';
import {requestSettings} from './requests.js';
import {costSettings} from './costs.js';
import {api, failureText, node, field, select, action, card, shell, confirmDialog, partnerId, selectPartner, displayDefaults} from './ui.js';
import {applyTheme, applyAccent, accents, preference, remember} from './theme.js';
import {newLook, saveLook, lookSnapshot} from './look-editor.js';
import {onboardingFinishing} from './onboarding-finishing.js';
import {voiceSettings} from './voice.js';
import {embeddingDownload} from './embedding-download.js';
import {aiQuestionChoices} from './setting-choices.js';
import {importSettings} from './import.js';
import {backupSettings} from './backup.js';
import {residentControls} from './resident.js';
import {settingsNavigation,leaveSettings,clearSettingsNavigation} from './settings-navigation.js';
export async function settingsScreen(parent, name, {setup=false}={}) {
  if(!setup)return settingsShelves(parent,name);
  const cfg=await api('/api/settings');
  const form=node('form',undefined,'set');parent.append(form);const draft=structuredClone(cfg);
  const panels={},tabs=[],navigation=setup?null:settingsNavigation();let active='partner/requests';
  const tablist=node('div',undefined,'settings-tabs');tablist.setAttribute('role','tablist');tablist.setAttribute('aria-label','設定の種類');if(!setup)form.append(tablist);
  const tabDefinitions=[['partner','パートナー'],['screen','画面'],['ai','AI'],['other','その他']];
  function showTab(key){for(const [id,panel]of Object.entries(panels))panel.hidden=id!==key;for(const b of tabs){const selected=b.dataset.tab===key;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;}remember('settingsTab',key);}
  async function move(key){if(key===active)return;if(!await navigation.leave())return;history.pushState(null,'',`#settings/${key}`);parent.replaceChildren();await settingsScreen(parent,name,{setup});document.getElementById(key.startsWith('partner/')?`settings-partner-tab-${key.split('/')[1]}`:`settings-tab-${key}`)?.focus();}
  for(const [key,title]of tabDefinitions){
    const panel=node('div',undefined,'set settings-panel');panel.id=`settings-panel-${key}`;panels[key]=panel;form.append(panel);
    if(setup)continue;
    panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',`settings-tab-${key}`);
    const b=action(tablist,title,()=>move(key==='partner'?'partner/requests':key));b.id=`settings-tab-${key}`;b.dataset.tab=key;b.setAttribute('role','tab');b.setAttribute('aria-controls',panel.id);tabs.push(b);
    b.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const i=tabs.indexOf(b),next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[next].focus();void move(tabs[next].dataset.tab==='partner'?'partner/requests':tabs[next].dataset.tab);};
  }
  const subpanels={},subtabs=[];
  if(!setup){const sublist=node('div',undefined,'settings-tabs partner-tabs');sublist.setAttribute('role','tablist');sublist.setAttribute('aria-label','パートナーの設定');panels.partner.append(sublist);
    for(const [key,title]of [['requests','話し方とお願い'],['pace',`${name||'パートナー'}の過ごし方`],['days','大切な日と習慣'],['look','見た目と声'],['history','これまでの会話']]){
      const panel=node('div',undefined,'set');panel.id=`settings-partner-${key}`;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',`settings-partner-tab-${key}`);subpanels[key]=panel;panels.partner.append(panel);
      const button=action(sublist,title,()=>move(`partner/${key}`));button.id=`settings-partner-tab-${key}`;button.setAttribute('role','tab');button.setAttribute('aria-controls',panel.id);subtabs.push(button);
      button.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const i=subtabs.indexOf(button),next=e.key==='Home'?0:e.key==='End'?subtabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+subtabs.length)%subtabs.length;subtabs[next].focus();void move(`partner/${Object.keys(subpanels)[next]}`);};
    }
  }
  const page=key=>subpanels[key]||panels.partner;
  const screen=card(panels.screen,'画面の設定');screen.hidden=setup;
  const screenGroup=title=>{const group=node('section',undefined,'pace-group set');group.append(node('h4',title));screen.append(group);return group;};
  const appearance=screenGroup('見た目');
  let theme=preference('theme','system'),accent=document.documentElement.dataset.accent;
  select(appearance,'外観',{system:'端末に合わせる',light:'生成りの紙（ライト）',dark:'夜更けのインク（ダーク）'},theme,v=>{theme=v;});
  appearance.append(node('p','画面全体の明るさと紙・文字の色を変えます。端末の設定に合わせることもできます。','small muted'));
  draft.display={...displayDefaults,...cfg.display};
  const colours=node('fieldset',undefined,'accent-choices');colours.append(node('legend','差し色'));appearance.append(colours);
  for(const [key,[label,colour]] of Object.entries(accents)){
    const option=node('label',undefined,'choice'),radio=node('input'),swatch=node('span',undefined,'swatch');radio.type='radio';radio.name='accent';radio.value=key;radio.checked=document.documentElement.dataset.accent===key;
    swatch.style.setProperty('--swatch-light',colour);swatch.style.setProperty('--swatch-dark',accents[key][2]);swatch.setAttribute('aria-hidden','true');option.append(radio,swatch,node('span',label));colours.append(option);
    radio.onchange=()=>{accent=key;};
  }
  appearance.append(node('p','ボタンや選んだ項目に使う差し色を変えます。','small muted'));
  const conversation=screenGroup('会話の見せ方'),notifications=screenGroup('知らせ'),notebook=screenGroup('ノート');
  for(const [key,label,help]of [['split_replies','返答を分けて出す','長い返答を文の区切りでいくつかの吹き出しに分け、少しずつ表示します。'],['delay_user','自分の投稿を少し遅らせて出す','送った文の吹き出しを少し待ってから表示します。返答の処理はすぐに始まります。']]){
    const row=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=draft.display[key];input.onchange=()=>draft.display[key]=input.checked;row.append(input,node('span',label));conversation.append(row,node('p',help,'small muted'));
  }
  const partner=card(page('look'),'見た目と声');
  partner.hidden=setup;
  const imports=!setup&&partnerId?importSettings(page('history')):null;
  const edit=node('a','顔と表情を整える','btn');edit.href='/wizard?look=1';partner.append(edit);
  const cover=node('section',undefined,'set');cover.append(node('h4','扉絵'),node('p','ノートの上に飾る横長の絵です。顔と表情とは別に作って取り込めます。','small muted'));
  const editCover=node('a','扉絵を整える','btn');editCover.href='/wizard?look=1&use=cover';cover.append(editCover);partner.append(cover);
  const persona=!setup&&partnerId?await api('/api/persona'):null;
  const requests=!setup&&partnerId?await requestSettings(page('requests'),name,{extra:box=>{if(persona)select(box,'「AI なの？」への答え方',aiQuestionChoices,persona.ai_question,v=>persona.ai_question=v);},afterSave:async()=>{if(persona)await api('/api/persona',persona,'PUT');}}):null;
  if(!setup&&partnerId){const unanswered=await api(`/api/characters/${encodeURIComponent(partnerId)}/augment`);if(unanswered.questions.length){const questions=card(page('requests'),`${name}について、まだ答えていない質問`);questions.append(node('p',`迎えた時に答えなかった、${name}の性格や過去の場面についての質問です。答えると${name}の人となりに加わります。`,'small muted'));const augment=node('a','追加の質問に答える','btn');augment.href=`/wizard?augment=${encodeURIComponent(partnerId)}`;questions.append(augment);}}
  const days=!setup&&partnerId&&cfg.days?.has_habits!==undefined?await daysSettings(page('days'),name):null;
  const pace=card(page('pace'),`${name||'あなたのパートナー'}の過ごし方`);
  const saveVoice=!setup&&partnerId&&cfg.tts?await voiceSettings(page('look'),draft,{pageSave:true}):null;
  if(saveVoice)page('look').lastElementChild.querySelector('h3').textContent=`${name}の声`;
  pace.hidden=setup;
  const monoLabel=node('label',undefined,'switch'),mono=node('input');mono.type='checkbox';mono.checked=cfg.inner?.show_monologue??true;mono.onchange=()=>draft.inner={show_monologue:mono.checked};monoLabel.append(mono,node('span',`ノートに「今日の${name}」を表示する`));notebook.append(monoLabel,node('p',`${name}が自分の時間に考えた短い一言を、ノートに表示します。`,'small muted'));
  const noticeLabel=node('label',undefined,'switch'),notice=node('input');notice.type='checkbox';notice.checked=cfg.early?.notify??true;notice.onchange=()=>draft.early={notify:notice.checked};noticeLabel.append(notice,node('span',`${name}のことを新しく知った時に知らせる`));notifications.append(noticeLabel,node('p',`会話を通じて${name}の新しい一面がノートに加わった時、会話画面で知らせます。`,'small muted'));
  const autonomous=node('section',undefined,'pace-group set'),greeting=node('section',undefined,'pace-group set'),time=node('section',undefined,'pace-group set');
  autonomous.append(node('h4','自主活動'));greeting.append(node('h4','話しかけ'));time.append(node('h4','自分の時間'),node('p','自主活動の時に、どれをするかをその時に決めます。それぞれ1日の回数を上限にします。ここで調べたことや読んだもの・観たものの話を、会話や話しかけの中でしてくることがあります。','small muted'));pace.append(autonomous,greeting,time);
  const talkLabel=node('label',undefined,'switch'),talk=node('input');talk.type='checkbox';talk.checked=cfg.autonomy.opening!=='off';talkLabel.append(talk,node('span','自分から話しかける'));greeting.append(talkLabel,node('p','オフにすると、会いに来た時の挨拶も、常駐アプリで間が空いた時の話しかけもしません。','small muted'));
  let talkPace=talk.checked?cfg.autonomy.opening:'normal';
  const frequency=select(greeting,'話しかける頻度',{many:'多め',normal:'ふつう',reserved:'控えめ'},talkPace,v=>{talkPace=v;draft.autonomy.opening=v;});
  const updateTalk=()=>{frequency.disabled=!talk.checked;draft.autonomy.opening=talk.checked?talkPace:'off';draft.autonomy.idle_nudge=talk.checked;};talk.onchange=updateTalk;updateTalk();
  greeting.append(node('p','会話を開いた時の挨拶と、常駐アプリで間が空いた時の話しかけの多さです','small muted'));
  const saveResident=!setup?await residentControls(appearance,{notifications,conversation}):null;
  select(autonomous,'自主活動の頻度（しないでオフ）',[['.5','30分ごと'],['1','1時間ごと'],['2','2時間ごと'],['4','4時間ごと'],['8','8時間ごと'],['off','しない']],cfg.autonomy.inner_life?String(cfg.autonomy.inner_life_interval_hours).replace('0.5','.5'):'off',v=>{draft.autonomy.inner_life=v!=='off';if(v!=='off')draft.autonomy.inner_life_interval_hours=Number(v);});
  autonomous.append(node('p','アプリが動いている間、自分の時間に何をするかをこの間隔で考えます。「しない」で自主活動を止めます。','small muted'));
  const researchLabel=node('label',undefined,'switch'), research=node('input');research.type='checkbox';research.checked=cfg.autonomy.research&&(cfg.autonomy.tools?.research.enabled??true);researchLabel.append(research,node('span','調べ物をする'));time.append(researchLabel);
  draft.autonomy.tools=structuredClone(cfg.autonomy.tools||{research:{enabled:true,daily_max:3},read_source:{enabled:true,daily_max:3},read_book:{enabled:true,daily_max:2}});
  for(const [tool,daily_max] of [['watch_video',1],['world_activity',2],['read_wikipedia',3],['browse_shopping',1]])draft.autonomy.tools[tool]??={enabled:true,daily_max,...(tool==='watch_video'?{max_minutes:20,model:'openrouter/google/gemini-3.8-flash'}:{}),...(tool==='browse_shopping'?{domains:['amazon.co.jp','rakuten.co.jp','makuake.com','camp-fire.jp','readyfor.jp']}:{})};
  if(cfg.autonomy.research_daily_max!=null)draft.autonomy.tools.research.daily_max=cfg.autonomy.research_daily_max;
  for(const [tool,label,maxLabel] of [['research','調べ物','調べ物'],['read_source','登録したサイトの記事を読む','サイトの記事'],['read_book','本を読む','読書'],['watch_video','動画を観る','動画'],['world_activity','世界の中で活動する','世界の活動'],['read_wikipedia','調べ物の代わりにWikipediaを読む','Wikipedia'],['browse_shopping','買い物の検索結果を眺める','買い物の検索']]){
    const group=node('div',undefined,'pace-tool set');time.append(group);if(tool==='research')group.append(researchLabel);
    if(tool!=='research'){const l=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=draft.autonomy.tools[tool].enabled;input.onchange=()=>draft.autonomy.tools[tool].enabled=input.checked;l.append(input,node('span',label));group.append(l);}
    const descriptions={research:'気になったことをウェブで調べ、ノートの「最近のこと」に残します。',read_source:'「よく読むサイト」に登録したサイトから記事を読み、感想をノートの「最近のこと」に残します。',read_book:'読んでいる本を少しずつ読み進め、感想をノートに残します。',watch_video:'気になった動画を観て、内容と感想をノートの「最近のこと」に残します。',world_activity:`${name||'あなたのパートナー'}の暮らす世界の中で日常の用事や出来事を過ごし、ノートに残します。`,read_wikipedia:'ウェブで調べられない時などにWikipediaを読み、気になったことを残します。',browse_shopping:'検索結果を眺めて感想を残します。購入・ログイン・支援は行いません。'};
    group.append(node('p',descriptions[tool],'small muted'));
    const max=field(group,`${maxLabel}の1日の上限`,draft.autonomy.tools[tool].daily_max,v=>draft.autonomy.tools[tool].daily_max=Number(v));max.type='number';max.min=0;max.max=100;
    if(tool==='watch_video'){const length=field(group,'動画1本の長さの上限（分）',draft.autonomy.tools.watch_video.max_minutes??20,v=>draft.autonomy.tools.watch_video.max_minutes=Number(v));length.type='number';length.min=1;length.max=120;}
    if(tool==='browse_shopping')field(group,'買い物の検索対象サイト（ドメインをカンマで区切る）',draft.autonomy.tools.browse_shopping.domains.join(', '),v=>draft.autonomy.tools.browse_shopping.domains=v.split(',').map(s=>s.trim()).filter(Boolean));
  }
  const total=field(autonomous,'活動全体の1日の上限',cfg.autonomy.activities_daily_max??6,v=>draft.autonomy.activities_daily_max=Number(v));total.type='number';total.min=0;total.max=100;
  autonomous.append(node('p','調べ物・読書・動画などを合わせて、この回数までにします。','small muted'));
  const hours=Object.fromEntries(Array.from({length:24},(_,i)=>[i,`${String(i).padStart(2,'0')}:00`]));
  const quiet=node('div',undefined,'pair');greeting.append(quiet);
  select(quiet,'静かな時間の開始',hours,cfg.autonomy.quiet_start_hour,v=>draft.autonomy.quiet_start_hour=Number(v));
  select(quiet,'静かな時間の終了',hours,cfg.autonomy.quiet_end_hour,v=>draft.autonomy.quiet_end_hour=Number(v));
  greeting.append(node('p','静かな時間帯は、放置している間の話しかけを控えます。開始と終了が同じなら時間帯の制限はありません。','small muted'));
  const saveSources=!setup&&partnerId&&cfg.autonomy.tools?await sourceSettings(pace):null;
  const ai=card(panels.ai,'使う AI');
  if(!setup)await costSettings(panels.ai,cfg,draft,form);
  if(!setup){ai.append(node('h4','返答前の確認'),node('p','返事を出す前に別の AI が、設定にない過去を作っていないか、前に話したことと食い違っていないか、プライベートなことに踏み込んでいないかを確かめます。必要なら書き直してから出します。','small muted'));}
  draft.pipeline.post_check='sync';

  draft.llm.mode=cfg.llm.mode||'advanced';
  draft.llm.zdr_only=cfg.llm.zdr_only??false;
  select(ai,'設定方法',{simple:'かんたん',advanced:'詳細設定'},draft.llm.mode,v=>{draft.llm.mode=v;showMode();});
  const simple=node('div');ai.append(simple);simple.hidden=draft.llm.mode!=='simple';
  draft.simple={...(cfg.simple||{})};
  // Warnings and failure banners were computed before the connection; refresh them once it is made.
  const warningBox=node('div');
  const refreshWarnings=async()=>{try{const fresh=await api('/api/settings');cfg.warnings=fresh.warnings;warningBox.replaceChildren(...(setup?[]:fresh.warnings).map(w=>node('p',w,'warn')));}catch{/* keep the old list */}};
  const saveConnection=connectionPanel(simple,cfg,()=>draft.simple,{managed:!setup,onConnected:()=>void refreshWarnings()});
  // The judge has a single recommended model, so the simple mode keeps it fixed (choose it in advanced mode).
  draft.simple.judge=draft.simple.judge||cfg.models.judge;
  for(const [role,title] of [['actor','会話のモデル']]){
    const candidates=cfg.model_candidates?.[role]||[],current=draft.simple[role]||cfg.models[role];
    draft.simple[role]=current;
    const available=candidates.filter(c=>c.status==='passed');
    const options=available.map(c=>[c.model,`${c.label} — ${c.model.replace(/^openrouter\//,'')}`]);
    options.push(['custom','一覧にないモデル名を入力']);
    const chosen=select(simple,title,options,available.some(c=>c.model===current)?current:'custom',v=>{
      custom.parentElement.hidden=v!=='custom';
      draft.simple[role]=v==='custom'?`openrouter/${custom.value.trim().replace(/^openrouter\//,'')}`:v;
    });
    const custom=field(simple,`${title}の名前`,current.replace(/^openrouter\//,''),v=>{if(chosen.value==='custom')draft.simple[role]=`openrouter/${v.trim().replace(/^openrouter\//,'')}`;});
    custom.autocomplete='off';custom.maxLength=245;custom.parentElement.hidden=chosen.value!=='custom';
    if(candidates.length){
      const table=node('table'),head=node('tr');for(const text of ['候補','説明','料金の目安（入力 / 出力）','確認'])head.append(node('th',text));table.append(head);
      for(const c of candidates){const row=node('tr');for(const text of [c.model.replace(/^openrouter\//,''),c.description,`$${c.input_usd} / $${c.output_usd}`,c.status==='failed'?'検証で問題あり・選択対象外':'確認済み'])row.append(node('td',text));table.append(row);}const wrapper=node('div',undefined,'model-candidates');wrapper.append(table);simple.append(wrapper);
    }
  }
  simple.append(node('p','料金は100万トークンあたりの参考値です。実際の料金は OpenRouter で確認してください。一覧にないモデルは動作未確認です。','small muted'));
  simple.append(node('p','記憶の検索モデルは固定です。変更する場合は詳細設定を使います。','small muted'));
  const advanced=node('div');ai.append(advanced);advanced.hidden=draft.llm.mode!=='advanced';
  const presetBox=node('div');advanced.append(presetBox);
  const presetNote=node('p','','small muted');
  const presets=cfg.advanced_presets||[];
  const presetChoice=select(presetBox,'AI の組み合わせ',[...presets.map(p=>[p.id,p.label]),['custom','カスタム']], 'custom',id=>{
    const preset=presets.find(p=>p.id===id);if(!preset)return;
    for(const [role,value] of Object.entries(preset.models)){
      if(role==='research'){draft.models.research=value;researchModel.value=value;continue;}
      if(['vision','stt'].includes(role)&&!value){roles[role]={provider:'none',model:''};roleInputs[role].provider.value='none';roleInputs[role].model.value='';continue;}
      const [provider,...model]=value.split('/');roles[role]={provider,model:model.join('/')};
      roleInputs[role].provider.value=provider;roleInputs[role].model.value=model.join('/');
    }
    renderCredentials();syncPreset();
  });presetBox.append(presetNote);
  const zdrLabel=node('label',undefined,'switch'),zdr=node('input');zdr.type='checkbox';zdr.checked=draft.llm.zdr_only;zdr.onchange=()=>draft.llm.zdr_only=zdr.checked;
  zdrLabel.append(zdr,node('span','会話を保存しない提供元だけを使う（OpenRouter）'));ai.append(zdrLabel);
  ai.append(node('p','オンにすると、送った内容を保存しないと申告している提供元だけを使います。使える提供元が減るため、返事が遅くなったり、混み合って失敗したりすることがあります。動画を観る活動は使えなくなります。どの提供元も会話を学習には使わないと申告しています。OpenRouter 以外の接続（Gemini 直接・ローカルモデル）には影響しません。','small muted'));
  function showMode(){for(const role of ['embedding','stt'])if(localBoxes[role])localBoxes[role].hidden=roles[role].provider!=='local';simple.hidden=draft.llm.mode!=='simple';advanced.hidden=draft.llm.mode!=='advanced';for(const input of advanced.querySelectorAll('input,select'))input.disabled=draft.llm.mode!=='advanced';for(const role of ['vision','stt'])if(roleInputs[role]){roleInputs[role].model.disabled=draft.llm.mode!=='advanced'||roles[role].provider==='none';roleInputs[role].model.required=roles[role].provider!=='none';}void downloads.refresh();void sttDownloads.refresh();}
  const providerOptions={gemini:'Gemini',anthropic:'Claude（Anthropic）',openai:'OpenAI',openrouter:'OpenRouter',local:'ローカル（OpenAI 互換）'};
  for(const p of Object.keys(cfg.llm.providers))if(!providerOptions[p])providerOptions[p]=`ローカル（${p}）`;
  const roles={},roleInputs={},localBoxes={},lastProvider={};
  // The model a provider uses for this role: its transcription candidate, a preset's choice, or the first local model.
  const defaultModel=(role,provider)=>{
    if(role==='stt'){const local=Object.keys(cfg.stt?.local_models||{})[0];return provider==='local'?(local&&'local/'+local):Object.keys(cfg.stt?.candidates||{}).find(m=>m.startsWith(provider+'/'));}
    return provider==='local'?'local/ruri-v3-130m':presets.map(p=>p.models[role]).find(m=>m?.startsWith(provider+'/'));
  };
  function syncPreset(){
    const values={...Object.fromEntries(Object.entries(roles).map(([role,r])=>[role,r.provider==='none'?'':`${r.provider}/${r.model.trim()}`])),research:draft.models.research};
    const selected=presets.find(p=>Object.entries(p.models).every(([role,model])=>values[role]===model));
    presetChoice.value=selected?.id||'custom';presetNote.textContent=selected?.description||'';
  }
  for(const [role,title,desc] of [['actor','会話','話す言葉をつくります'],['judge','判断','場面や返答を確かめます'],['steward','記憶の整理','経験を手帖に残します'],['embedding','記憶の検索','話に合う記憶を探します'],['vision','写真の読み取り','写真を一度だけ説明文にします'],['stt','文字起こし','声を文字にして入力欄へ入れます']]){
    const [provider,...model]=((cfg.advanced_models||cfg.models)[role]||(['vision','stt'].includes(role)?'none/':'gemini/')).split('/');roles[role]={provider,model:model.join('/')};lastProvider[role]=provider;
    const row=node('div',undefined,'srow');row.append(node('p',desc,'small muted'));advanced.append(row);
    roleInputs[role]=modelRoleFields(row,title,role==='embedding'?{...Object.fromEntries(Object.entries(providerOptions).filter(([p])=>p!=='anthropic')),local:'ローカルモデル（ruri）'}:role==='stt'?{none:'使わない',gemini:'Gemini',openai:'OpenAI',openrouter:'OpenRouter',local:'ローカルモデル（Haas-ja）'}:role==='vision'?{none:'使わない',...providerOptions}:providerOptions,()=>roles[role],()=>{
      // A service's own transcription model replaces a local model name (and the reverse).
      if(['stt','embedding'].includes(role)&&roles[role].provider!==lastProvider[role]){
        lastProvider[role]=roles[role].provider;
        const provider=roles[role].provider,known=defaultModel(role,provider);
        roles[role].model=known?known.split('/').slice(1).join('/'):'';roleInputs[role].model.value=roles[role].model;
      }
      renderCredentials();syncPreset();if(role==='embedding')void downloads.refresh();void sttDownloads.refresh();});
    // Local model choices and their download sit in their own row, shown only for「ローカル」.
    if(role==='embedding'||role==='stt'){localBoxes[role]=node('div',undefined,'set');row.append(localBoxes[role]);}
  }
  const pickLocal=(role,model)=>{roles[role]={provider:'local',model};roleInputs[role].provider.value='local';roleInputs[role].model.value=model;renderCredentials();syncPreset();};
  localBoxes.embedding.append(node('p','この PC で記憶を探すモデルです。容量を確かめてから取得します。','small muted'));
  const embeddingPresets=node('div',undefined,'row');localBoxes.embedding.append(embeddingPresets);
  for(const [model,label] of [['ruri-v3-130m','ruri-v3-130m（初回 約140 MB）'],['ruri-v3-30m','ruri-v3-30m（初回 約44 MB）']])action(embeddingPresets,label,()=>pickLocal('embedding',model));
  const downloads=embeddingDownload(localBoxes.embedding,()=>draft.llm.mode==='advanced'?`${roles.embedding.provider}/${roles.embedding.model.trim()}`:'');
  draft.stt??={auto_send:false,max_seconds:60,silence_seconds:2,device:'auto'};
  localBoxes.stt.append(node('p','この PC で声を文字にするモデルです。Haas-ja は日本語向けの高速な文字起こしです。','small muted'));
  const sttChoices=node('div',undefined,'row');localBoxes.stt.append(sttChoices);
  for(const [id,spec] of Object.entries(cfg.stt?.local_models||{}))action(sttChoices,`${id}（約${(spec.size/1e6).toFixed(0)} MB）`,()=>pickLocal('stt',id));
  select(localBoxes.stt,'ローカルの文字起こしで使う装置',{auto:'自動（対応GPUがあれば使用）',gpu:'GPU（Vulkan）',cpu:'CPU'},draft.stt.device,v=>{draft.stt.device=v;void sttDownloads.refresh();});
  const sttDownloads=embeddingDownload(localBoxes.stt,()=>draft.llm.mode==='advanced'?`${roles.stt.provider}/${roles.stt.model.trim()}`:'',{endpoint:'/api/stt',title:'ローカルの文字起こし',label:'文字起こしモデル',parameters:()=>({device:draft.stt?.device||'auto'})});
  const speechInput=card(panels.ai,'声で入力する');speechInput.append(node('p','マイクの音声を文字にして、会話の入力欄へ入れます。文字にする方法は、詳細設定の「文字起こし」で選べます。あとから設定の「画面と入力」でも変えられます。','small muted'));
  sttSettingsFields(speechInput,draft);
  const researchModel=field(advanced,'調べ物のモデル名',(cfg.advanced_models||cfg.models).research||'gemini/gemini-3.8-flash',v=>{draft.models.research=v;renderCredentials();syncPreset();});
  draft.models.research=researchModel.value;
  syncPreset();
  const credentials=node('div');advanced.append(credentials);const keyFields={}, localFields={};
  const warnings=node('div');warnings.setAttribute('role','status');advanced.append(warnings);
  function renderCredentials() {
    for(const role of ['stt','embedding'])if(roles[role])lastProvider[role]=roles[role].provider;  // presets and local picks change it without the select
    const used=new Set(Object.values(roles).map(r=>r.provider));if(draft.autonomy.research)used.add((draft.models.research||'gemini/').split('/')[0]);
    credentials.replaceChildren();
    for(const p of [...used].filter(p=>p!=='none')){
      const row=node('div',undefined,'srow');credentials.append(row);
      if(p==='local'&&!Object.entries(roles).some(([role,r])=>!['embedding','stt'].includes(role)&&r.provider==='local'))continue;
      if(p==='gemini'||p==='openai'||p==='openrouter'||p==='anthropic'){
        if(!keyFields[p]){const label=node('label',`${providerOptions[p]} の API キー`,'f'),input=node('input');input.type='password';input.autocomplete='new-password';input.placeholder='変更するときだけ入力';input.maxLength=4096;label.append(input);keyFields[p]={label,input};}
        row.append(keyFields[p].label,node('p',cfg.keys[p]==='set'?'設定済み':'未設定','small muted'));
      }else{
        if(!localFields[p]){const label=node('label',`${providerOptions[p]}の接続先`,'f'),input=node('input');input.type='url';input.required=true;input.value=cfg.llm.providers[p]?.base_url || 'http://localhost:11434/v1';label.append(input);localFields[p]={label,input};}
        row.append(localFields[p].label,node('p','キーは不要です。','small muted'));
      }
    }
    // Only when research alone needs this key; a key the conversation needs anyway is asked for above.
    const rp=(draft.models.research||'gemini/').split('/')[0],alone=!Object.values(roles).some(r=>r.provider===rp);
    warnings.replaceChildren(...(draft.autonomy.research&&alone&&cfg.keys[rp]!=='set'?[node('p',`調べ物には ${providerOptions[rp]||rp} の API キーも使います。入れるまでは調べ物をしません。`,'small muted')]:[]));
    showMode();
  }
  research.onchange=()=>{draft.autonomy.research=research.checked;draft.autonomy.tools.research.enabled=research.checked;renderCredentials();};renderCredentials();
  ai.append(node('p','キーはこの PC に保存します。保存済みの値を画面に表示することはありません。','small muted'));
  const maintenance=node('div',undefined,'row');ai.append(maintenance);
  const progress=node('p','','status');progress.setAttribute('role','status');ai.append(progress);
  async function reindex() {
    try{await reindexAI(text=>progress.textContent=text);}catch(error){if(!maintenance.querySelector('button'))action(maintenance,'もう一度、索引を作り直す',reindex);throw error;}
  }
  if(cfg.reindex_required)action(maintenance,'記憶の索引を作り直す',reindex);
  // 「はじめに」 shows only what to do next; whole-app warnings (no key yet, unused features) wait for settings.
  if(!setup)warningBox.append(...cfg.warnings.map(warning=>node('p',warning,'warn')));ai.append(warningBox);
  const backup=!setup&&cfg.backup_status?backupSettings(panels.other,cfg,{managed:true}):null;
  const dev=card(panels.other,'開発者モード'), devLabel=node('label',undefined,'switch'),toggle=node('input');toggle.type='checkbox';toggle.checked=preference('dev')==='true';devLabel.append(toggle,node('span','開発者画面へのリンクを表示する'));dev.append(devLabel);
  dev.hidden=setup||!cfg.developer;  // release builds do not offer the developer screen
  const devLink=node('a','開発者画面を開く','btn');devLink.href=`/dev?character=${encodeURIComponent(partnerId)}`;devLink.hidden=!toggle.checked;dev.append(devLink);
  toggle.onchange=()=>{devLink.hidden=!toggle.checked;};
  const save=node('button',setup?'つながるか確かめて進む':'AI の設定を保存','btn primary');save.type=setup?'submit':'button';if(setup)form.append(save);else panels.ai.append(save);
  const saved=node('p','','status');saved.setAttribute('role','status');panels.ai.append(saved);
  if(!partnerId&&!setup&&cfg.connection_verified){const welcome=node('a','あなたのパートナーを迎える手順へ','btn');welcome.href='/wizard';form.append(welcome);}
  async function verify(){
    save.disabled=true;form.inert=true;saved.textContent='選んだ AI につながるか確かめています…';
    try{await api('/api/settings/connection',{});saved.textContent='つながりました。';location.reload();}
    catch(error){failureText(saved,error);}
    finally{save.disabled=false;form.inert=false;}
  }
  async function saveAI(){
    save.disabled=true;form.inert=true;saved.textContent='';
    try {
      const models=Object.fromEntries(Object.entries(roles).map(([role,r])=>[role,r.provider==='none'?'':`${r.provider}/${r.model.trim()}`]));
      const target=draft.llm.mode==='simple'?'openrouter/google/gemini-embedding-2':models.embedding;
      await downloads.requireReady();await sttDownloads.requireReady();
      const changed=embeddingIdentity(target)!==embeddingIdentity(cfg.models.embedding);
      if(changed&&!await confirmDialog('記憶の検索モデルを変更すると、保存済みの記憶の索引を作り直します。完了するまで記憶の検索を休みます。'))return false;
      if(draft.llm.mode==='simple')await saveConnection();
      const used=new Set(Object.values(roles).map(r=>r.provider));if(draft.autonomy.research)used.add((draft.models.research||'gemini/').split('/')[0]);
      for(const p of [...used].filter(p=>p!=='none'))if(draft.llm.mode==='advanced'&&keyFields[p]?.input.value){await api('/api/settings/keys',{provider:p,key:keyFields[p].input.value},'PUT');keyFields[p].input.value='';cfg.keys[p]='set';}
      const providers={};for(const p of [...used].filter(p=>p!=='none'))if(localFields[p])providers[p]={api:'chat',base_url:localFields[p].input.value};
      models.research=draft.models.research;
      const updated=await api('/api/settings',{...(draft.costs?{costs:draft.costs}:{}),stt:{auto_send:draft.stt.auto_send,max_seconds:draft.stt.max_seconds,silence_seconds:draft.stt.silence_seconds,device:draft.stt.device},pipeline:{post_check:draft.pipeline.post_check},...(draft.llm.mode==='advanced'?{models}:{simple:draft.simple}),llm:{mode:draft.llm.mode,zdr_only:draft.llm.zdr_only,...(draft.llm.mode==='advanced'&&Object.keys(providers).length?{providers}:{})}},'PUT');
      Object.assign(cfg,updated);
      if(changed&&updated.reindex_required!==false&&!setup)await reindex();renderCredentials();saved.textContent='設定を保存しました。';
      if(setup)await verify();return true;
    }catch(error){saved.textContent=error.message;throw error;}finally{save.disabled=false;form.inert=false;}
  }
  const saves={ai:saveAI,
    screen:async()=>{await api('/api/settings',{display:draft.display,early:{notify:draft.early?.notify??true},inner:{show_monologue:draft.inner?.show_monologue??true}},'PUT');if(saveResident)await saveResident();remember('theme',theme);applyTheme(theme);remember('accent',accent);applyAccent(accent);},
    'partner/pace':async()=>{await api('/api/settings',{autonomy:{idle_nudge:draft.autonomy.idle_nudge,inner_life:draft.autonomy.inner_life,inner_life_interval_hours:draft.autonomy.inner_life_interval_hours,research:draft.autonomy.research,research_daily_max:draft.autonomy.tools.research.daily_max,tools:draft.autonomy.tools,activities_daily_max:draft.autonomy.activities_daily_max??6,quiet_start_hour:draft.autonomy.quiet_start_hour,quiet_end_hour:draft.autonomy.quiet_end_hour,opening:draft.autonomy.opening}},'PUT');await saveSources?.();},
    'partner/look':async()=>{if(saveVoice)await api('/api/settings',{tts:await saveVoice()},'PUT');},
    other:async()=>{await backup?.save?.();remember('dev',String(toggle.checked));shell('settings',name);},
    'partner/history':async()=>{await imports?.save();}
  };
  if(requests)saves['partner/requests']=()=>requests.save();if(days)saves['partner/days']=()=>days.save();
  const cleaners={};
  async function savePage(key){
    const root=key.startsWith('partner/')?page(key.split('/')[1]):panels[key];
    for(const input of root.querySelectorAll('input,textarea,select'))if(!input.disabled&&!input.closest('[hidden]')&&!input.reportValidity())return false;
    if(await saves[key]?.()===false)return false;cleaners[key]?.();return true;
  }
  for(const [key,label]of [['screen','画面の設定を保存'],['partner/pace','過ごし方を保存'],['partner/look','見た目と声の設定を保存'],['other','その他の設定を保存']]){
    // これまでの会話 saves inside its own import flow; leaving the page still asks via navigation.
    if(setup)continue;const root=key.startsWith('partner/')?page(key.split('/')[1]):panels[key],status=node('p','','status');status.setAttribute('role','status');
    action(root,label,async()=>{if(await savePage(key)!==false)status.textContent='設定を保存しました。';},true);root.append(status);
  }
  if(!setup){
    for(const [key,root]of [...Object.entries(panels).filter(([k])=>k!=='partner'),...Object.entries(subpanels).map(([k,v])=>[`partner/${k}`,v])])cleaners[key]=navigation.register(key,root,()=>savePage(key));
    if(requests)requests.onSaved=cleaners['partner/requests'];if(days)days.onSaved=cleaners['partner/days'];
    const route=location.hash.replace(/^#settings\/?/,'').replace(/^partner-requests$/,'partner/requests').replace(/^partner-days$/,'partner/days').replace(/^look$/,'partner/look');
    active=route in saves||route==='partner/history'?route:route==='partner'?'partner/requests':tabDefinitions.some(([key])=>key===preference('settingsTab')&&key!=='partner')?preference('settingsTab'):'partner/requests';
    const [main,sub]=active.split('/');entryNotice(sub?subpanels[sub]:panels[main]);showTab(main);navigation.activate(active);
    for(const [key,panel]of Object.entries(subpanels))panel.hidden=key!==sub;
    for(const button of subtabs){const selected=button.id===`settings-partner-tab-${sub}`;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;}
  }
  save.onclick=async()=>{try{await savePage('ai');}catch(error){saved.textContent=error.message;}};
  form.noValidate=true;form.onsubmit=async e=>{e.preventDefault();try{return await savePage(setup?'ai':active);}catch(error){saved.textContent=error.message;return false;}};
  if(setup&&cfg.ai_ready)await verify();
}
export async function editLook(parent) {
  const state=newLook(await api('/api/look')),params=new URLSearchParams(location.search);
  const partner=(await api('/api/partner')).partner;
  const navigation=settingsNavigation();let saving=false,clean=()=>{};
  const save=async()=>{if(saving||state.pending)return false;saving=true;try{await saveLook(state);clean();return true;}finally{saving=false;}};
  const done=()=>{clean();location.href='/#settings/look';};
  const flow=await onboardingFinishing(parent,{name:partner?.name||'あなたのパートナー',lookOnly:true,lookState:state,describe:params.has('describe'),coverOnly:params.get('use')==='cover',onBack:()=>{location.href='/#settings/look';},onLookDone:done});
  clean=navigation.register('look',parent,save,()=>lookSnapshot(state));navigation.activate('look');
  return {...flow,busy:()=>saving||!!state.pending,save};
}

export {embeddingIdentity} from './settings-entry.js';
export function connectionPanel(parent,cfg,simpleChoices,{managed=false,onConnected=()=>{}}={}){
  const status=node('p',cfg.keys.openrouter==='set'?'OpenRouter につながっています。':'OpenRouter につなぐと、ひとつのキーで会話や記憶の整理、調べ物を使えます。');parent.append(status);
  const login=node('button','OpenRouter でログイン','btn primary');login.type='button';const connectionActions=node('div',undefined,'row');parent.append(connectionActions);connectionActions.append(login);let stopLogin;
  const modeSettings=()=>({llm:{mode:'simple'},...(simpleChoices?{simple:simpleChoices()}:{})});
  if(cfg.keys.openrouter==='set')login.textContent='OpenRouter でつなぎ直す';
  const idle=login.textContent;
  // Moving to OpenRouter takes a few seconds; show that the press was taken and ignore repeated clicks.
  login.onclick=async e=>{e.preventDefault();if(login.classList.contains('busy'))return;
    if(managed&&!await leaveSettings())return;
    login.classList.add('busy');login.setAttribute('aria-disabled','true');login.textContent='OpenRouter に移動しています…';
    try{await api('/api/settings',modeSettings(),'PUT');stopLogin?.();stopLogin=await openRouterLogin(parent,updated=>{cfg.keys=updated.keys;status.textContent='連携できました。もう一度押すと、つなぎ直せます。';login.classList.remove('busy');login.removeAttribute('aria-disabled');login.textContent='OpenRouter でつなぎ直す';onConnected();},()=>{status.textContent='ブラウザでの連携を待っています。キャンセルした場合はもう一度試せます。';login.classList.remove('busy');login.removeAttribute('aria-disabled');login.textContent=idle;});}
    catch(error){status.textContent=error.message;login.classList.remove('busy');login.removeAttribute('aria-disabled');login.textContent=idle;}};
  if(cfg.keys.openrouter==='set')action(connectionActions,'切る',async()=>{await api('/api/settings/keys',{provider:'openrouter',key:''},'PUT');cfg.keys.openrouter='unset';status.textContent='接続を切りました。';});
  parent.append(node('p','使いすぎを防ぐため、OpenRouter 側で利用上限を設定できます。','small muted'));
  const limits=node('a','OpenRouter のキーと利用上限を管理');limits.href='https://openrouter.ai/settings/keys';limits.target='_blank';limits.rel='noreferrer';parent.append(limits);
  const manual=node('details');manual.append(node('summary','キーを持っている場合'));parent.append(manual);
  const key=field(manual,'OpenRouter の API キー','',()=>{});key.type='password';key.autocomplete='new-password';key.maxLength=4096;
  const saveKey=async()=>{if(!key.value.trim())return;await api('/api/settings/keys',{provider:'openrouter',key:key.value.trim()},'PUT');cfg.keys.openrouter='set';key.value='';};
  if(managed)manual.append(node('p','AI の設定を保存すると、このキーも保存します。','small muted'));
  else action(manual,'キーを保存する',async()=>{if(!key.value.trim())throw new Error('キーを入力してください。');await saveKey();await api('/api/settings',modeSettings(),'PUT');key.value='';location.reload();});
  return saveKey;
}
export async function connectionGate(parent){
  const cfg=await api('/api/settings');
  if(cfg.ai_ready!==false){
    return false;
  }
  // After a restore without keys the partner already exists; select it so its face shows.
  const partner=(await api('/api/partner').catch(()=>({})))?.partner;if(partner)selectPartner(partner.id);
  parent.replaceChildren();shell('settings');
  if(document.getElementById('heading'))document.getElementById('heading').textContent='はじめに';
  if(document.getElementById('subtitle'))document.getElementById('subtitle').textContent='使う AI を選んでつなぎ、そのままあなたのパートナーを迎えます。';
  parent.append(node('p','まず、使う AI につなぎます。かんたん設定か詳細設定を選んでください。'));
  await settingsScreen(parent,'',{setup:true});
  return true;
}

async function sourceSettings(parent){
  const section=node('section',undefined,'pace-group set');section.append(node('h4','よく読むサイト'),node('p','記事を読みに行くサイト・ブログ・YouTube チャンネルの URL を登録します。「登録したサイトの記事を読む」が有効な時に記事を選び、新しい記事は1日1回まで確かめます。変更は「過ごし方を保存」で反映します。','small muted'));parent.append(section);
  const original=await api('/api/sources');let sources=structuredClone(original);
  const list=node('div');section.append(list);
  function refresh(){list.replaceChildren();for(const source of sources){
    const row=node('div',undefined,'srow');row.append(node('p',source.title),node('p',source.url,'small muted'));
    if(source.kind==='index')row.append(node('p','記事の一覧として、1日1回まで新着を確かめます。','small muted'));
    const label=node('label',undefined,'switch'),enabled=node('input');enabled.type='checkbox';enabled.checked=source.enabled;enabled.onchange=()=>source.enabled=enabled.checked;label.append(enabled,node('span','このサイトを読む'));row.append(label);
    if(source.error)row.append(node('p',source.error,'warn'));
    if(source.id)action(row,'取得を確かめる',async()=>{await api(`/api/sources/${source.id}/test`,{});});
    const identity=node('input');identity.type='hidden';identity.value=source.url;row.append(identity);
    action(row,'削除する',()=>{sources=sources.filter(s=>s!==source);refresh();});list.append(row);
  }}
  const url=field(section,'サイトの URL','',()=>{});url.type='url';url.maxLength=2048;
  action(section,'サイトを追加する',()=>{if(!url.value.trim()||!url.reportValidity())return;sources.push({id:null,title:'新しいサイト',url:url.value.trim(),enabled:true});url.value='';refresh();});
  refresh();
  return async()=>{
    if(url.value.trim()){if(!url.reportValidity())throw new Error('サイトの URL を確認してください。');sources.push({id:null,title:'新しいサイト',url:url.value.trim(),enabled:true});url.value='';}
    for(const old of [...original])if(!sources.some(s=>s.id===old.id)){await api(`/api/sources/${old.id}`,{},'DELETE');original.splice(original.indexOf(old),1);}
    for(const source of sources){
      if(!source.id){const enabled=source.enabled,added=await api('/api/sources',{url:source.url});Object.assign(source,added);original.push(structuredClone(source));source.enabled=enabled;}
      const old=original.find(s=>s.id===source.id);if(old&&old.enabled!==source.enabled){await api(`/api/sources/${source.id}`,{enabled:source.enabled},'PATCH');old.enabled=source.enabled;}
    }refresh();
  };
}

export {settingsRoute} from './settings-shelves.js';
