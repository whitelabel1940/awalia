import {questionnaireNamed} from './questionnaire.js';
import {api, $, node, action, field, select as nativeSelect, card, shell, report, selectPartner} from './ui.js';
import {choiceCards,policyDescriptions} from './onboarding.js';
import {editLook, connectionGate} from './settings.js';
import {onboardingAI} from './onboarding-ai.js';
import {onboardingFinishing} from './onboarding-finishing.js';
import {onboardingPartner} from './onboarding-partner.js';
import {stopVoice, configureVoice, autoplayResidentTurn} from './voice.js';
import {isResident,startResident,notifyResident,markResidentReady} from './resident.js';
import {settingsNavigation,leaveSettings,clearSettingsNavigation} from './settings-navigation.js';
import {settingTemplates,aiQuestionChoices,aiQuestionConflict} from './setting-choices.js';
import {welcomeRestore} from './restore.js';
let restoreWelcome=false;
const params=new URLSearchParams(location.search), augmentId=params.get('augment'), lookOnly=params.has('look');
const data={id:`partner_${crypto.randomUUID().replaceAll('-','').slice(0,12)}`,basics:{name:''},world:{preset:'現代日本',contact:'A'},persona:{ai_question:'immersive',open_style:'hinted'},text:null,now:{likes:[],life:{}},answers:{},life:{},elaborate:{default:'may_elaborate'},adaptive_questions:[],adaptive_answers:{},conflict_resolutions:[],review_notes:{}};
const resumeImport=params.get('import');if(resumeImport&&/^[a-z][a-z0-9_\-]{0,63}$/.test(resumeImport))data.id=resumeImport;
let catalog, plan, step=4, pending=false, draftJob, questionIndex=0, adaptiveIndex=0, adaptiveGenerated=false, adaptiveParents={}, augmentQuestions=[], name='';
// Preserve question IDs across repeated planning and back navigation.
const asked=new Map();
let profileText, createdResult;
let onboardingRender,creationVersion=0;
function select(parent,title,options,value,change){if(!onboardingRender)return nativeSelect(parent,title,options,value,change);const state={value};choiceCards(parent,title,Object.entries(options).map(([v,l])=>[v,l,policyDescriptions[v]||'']),value,v=>{state.value=v;change(v);});return state;}
let cleanAugment=()=>{};
async function residentPage(partner,pageBusy){
  if(!isResident()||!partner)return;
  const sessions=await api('/api/sessions'),session=sessions[0]||await api('/api/sessions',{});
  const histories=await Promise.all(sessions.map(s=>api(`/api/sessions/${s.id}/turns`)));
  let after=Math.max(0,...histories.flat().map(t=>t.id));
  const settings=async()=>{const cfg=await api('/api/settings');configureVoice(cfg.tts);return cfg;};
  const reply=async result=>{after=Math.max(after,result.turn_id);autoplayResidentTurn(result.turn_id);await notifyResident(partner,result,session.id,{outsideTalk:true});};
  const request=path=>api(path,{session_id:session.id,after_turn_id:after});
  async function open(target){
    if(target&&target.characterId!==partner.id)return;
    if(!await leaveSettings())return;
    clearSettingsNavigation();stopVoice();
    const query=new URLSearchParams({character:partner.id});if(target)query.set('resident_turn',target.turnId);
    location.assign(`/?${query}#talk`);
  }
  await markResidentReady();
  await startResident({settings,busy:pageBusy,
    opening:async background=>{const result=await api('/api/proactive/opening',{session_id:session.id,after_turn_id:after,...(background?{background:true}:{})});if(result.speak)await reply(result);return result;},
    idle:()=>request('/api/proactive/idle'),habit:()=>request('/api/proactive/habit'),onReply:reply,onOpen:open,onFocus:()=>open(),onError:report});
}
const prefilled={}, checkRecords=new Map();
function checkKey(c){return c.question_ref?JSON.stringify([c.question_ref,c.quote,c.answer_keys]):JSON.stringify([c.setting,c.quote,c.choice_template||'world']);}
function clearAdaptive(){const refs=new Set(data.adaptive_questions.map(q=>`author:${q.id}`));data.conflict_resolutions=data.conflict_resolutions.filter(r=>!refs.has(r.conflict.question_ref));for(const [key,c]of checkRecords)if(refs.has(c.question_ref))checkRecords.delete(key);data.adaptive_questions=[];data.adaptive_answers={};adaptiveGenerated=false;adaptiveIndex=0;}
function invalidateChoice(group,id){const ref=`${group==='now'||group==='life'?group:'author'}:${id}`;data.conflict_resolutions=data.conflict_resolutions.filter(r=>r.conflict.question_ref!==ref);for(const [key,c]of checkRecords)if(c.question_ref===ref)checkRecords.delete(key);}
function button(text,fn,primary=false,parent=$('navigation'),back=false){
  if(parent===$('navigation')){
    const group=back?$('navigation').querySelector('.wizard-back'):$('navigation').querySelector('.wizard-forward');
    const b=action(group,text,async()=>{if(!pending)await fn();},primary);
    if(!primary&&!back){const main=group.querySelector('.primary');if(main)group.insertBefore(b,main);}return b;
  }
  return action(parent,text,async()=>{if(!pending)await fn();},primary);
}
function backButton(text,fn){return button(text,fn,false,$('navigation'),true);}
function navigation(){const back=node('div',undefined,'wizard-back'),forward=node('div',undefined,'wizard-forward');$('navigation').replaceChildren(back,forward);}

function forgetSettingChecks(){for(const [id,c]of checkRecords)if(!c.question_ref)checkRecords.delete(id);for(const key of Object.keys(data.review_notes))if(key.startsWith('setting:'))delete data.review_notes[key];}
function editWorld(key,value){data.world[key]=value;forgetSettingChecks();if(key==='contact')dropStaleContactAnswers();}
// Some questions offer different choices per contact (living together or not).
function contactChoices(q){const by=q.contact_choices;return by?(by[data.world.contact]||by.default):null;}
// An answer picked under another contact no longer fits; drop it so the question is asked again.
function dropStaleContactAnswers(){if(!catalog)return;for(const q of catalog.questionnaire.sections.flatMap(s=>s.questions)){const value=data.now.life[q.id],offered=contactChoices(q);if(!value||!offered)continue;const keys=value.answer||[].concat(value),fits=new Set([...offered,'leave_open','unknown']);if(keys.some(k=>!fits.has(k))){delete data.now.life[q.id];invalidateChoice('now',q.id);clearAdaptive();}}}
function policy(parent,ref,label){select(parent,label,{may_elaborate:'膨らませてよい',keep:'このまま'},data.elaborate[ref]||data.elaborate.default,v=>data.elaborate[ref]=v);}
let jobSerial=0;const runningJobs=new Map();
function jobProgress(){const active=[...runningJobs.values()],blocking=active.some(j=>!j.background);pending=active.length>0;$('screen').inert=blocking;$('navigation').inert=blocking;$('screen').setAttribute('aria-busy',String(pending));const progress=$('progress');if(document.body.classList.contains('onboarding'))$('screen').querySelector(':scope>section')?.append(progress);progress.classList.toggle('working',pending);progress.textContent=active.at(-1)?.progress||'';}
async function job(path,body,{background=false}={}){
  // A like row added and then left empty (by going back) is not an answer; sent as is, the server refuses the whole request.
  if(body===data)data.now.likes=data.now.likes.filter(l=>l.topic?.trim());
  const id=++jobSerial,state={background,progress:`${data.basics.name||name||'あなたのパートナー'}のことを考えています。少しお待ちください。`};runningJobs.set(id,state);jobProgress();
  try{let j=await api(path,body);while(j.status==='running'){state.progress=j.progress;jobProgress();await new Promise(r=>setTimeout(r,800));j=await api(`/api/bootstrap/jobs/${j.id}`);}if(j.status==='failed'){const stage={reading:'読み取り',questions:'質問の生成',building:'人物像の生成',validation:'検証',review:'確認項目の整理',saving:'保存の準備'}[j.stage]||'処理';const error=new Error(`${stage}で止まりました。もう一度試してください。`);error.stage=stage;throw error;}return j;}
  finally{runningJobs.delete(id);jobProgress();}
}
async function readPlan(){
  const j=await job('/api/bootstrap/plan',data);plan=j.result;for(const q of plan.questions)asked.set(`${q.group}:${q.id}`,q);
  prefillVoice(plan.reader);
  const checks=[...(plan.reader.setting_conflicts||[]),...(plan.confirmation_required||[])],currentKeys=new Set(checks.map(checkKey));
  for(const [key,c]of checkRecords)if(c.setting==='persona.ai_question'&&!c.choice&&!currentKeys.has(key))checkRecords.delete(key);
  for(const c of checks){
    const key=checkKey(c),previous=checkRecords.get(key);checkRecords.set(key,{selectedAnswer:data.persona.ai_question,...previous,...c});
  }
  return true;
}
async function readProfile(){
  if((!data.text&&!data.import_batch_ids?.length)||data.text===profileText)return;
  const j=await job('/api/bootstrap/read',{id:data.id,text:data.text,persona:data.persona,import_batch_ids:data.import_batch_ids||[]});plan=j.result;profileText=data.text;
  const profile=plan.reader.profile;
  if(profile?.name)data.basics.name=profile.name.value;
  if(profile)for(const [key,item]of Object.entries(profile.world))if(item)data.world[key]=item.value;
  dropStaleContactAnswers();
  if(profile&&Object.values(profile.world).some(Boolean))data.world.preset='自由記述';
  // Draft the speech fields from the text; keep anything the user typed themselves.
  prefillVoice(plan.reader);
}
function prefillVoice(reader){
  const voice=reader.voice||{};
  for(const [key,item]of Object.entries({first_person:voice.first_person,second_person:voice.second_person_default,register:voice.speech_register,length:voice.length_default})){
    if(!item?.value||(data.basics[key]&&data.basics[key]!==prefilled[key]))continue;
    data.basics[key]=prefilled[key]=item.value;
  }
}
function sceneQuestions(){
  if(augmentId)return augmentQuestions.map(q=>({...q,group:'answers'}));
  const order=['relationship','depth','life'];
  return [...asked.values()].filter(q=>q.group!=='now'&&(q.group!=='answers'||plan.coverage[q.domain]!=='text')&&!plan.reader.items.some(i=>i.covers.includes(`${q.group}:${q.id}`)))
    .sort((a,b)=>order.indexOf(a.section)-order.indexOf(b.section));
}
function valuesFor(group){return group==='now'?data.now.life:data[group];}
function question(parent,q,group){
  q=questionnaireNamed(q,data.basics.name||name);
  const values=group==='now'?data.now.life:data[group];let current=values[q.id]||{answer:['leave_open'],text:null};
  if(typeof current==='string')current={answer:[current]};if(Array.isArray(current))current={answer:current};
  const box=node('fieldset',undefined,'qcard');box.append(node('legend',q.scene));if(q.multi)box.append(node('p','2つまで選べます。','small muted'));
  const checks=[],open=new Set(['leave_open','unknown',...(q.open_choices||[])]);
  const offered=contactChoices(q);
  const choices=Object.entries({...(offered?Object.fromEntries(offered.map(k=>[k,q.choices[k]])):q.choices),leave_open:'決めないでおく（話していくうちに分かる）'}).filter(([key])=>key!=='unknown').sort(([a],[b])=>a==='leave_open'?-1:b==='leave_open'?1:0);
  for(const [key,text] of choices){const label=node('label',undefined,'choice'),e=node('input');e.type=q.multi?'checkbox':'radio';e.name=`${group}_${q.id}`;e.value=key;e.checked=current.answer.includes(key)||(key==='leave_open'&&current.answer.includes('unknown'));label.append(e,node('span',text));box.append(label);checks.push(e);
    e.onchange=()=>{let chosen;if(q.multi){if(open.has(key)){for(const c of checks)c.checked=c===e;e.checked=true;}else for(const c of checks)if(open.has(c.value))c.checked=false;chosen=checks.filter(c=>c.checked).map(c=>c.value);if(chosen.length>2){e.checked=false;report(new Error('選べるのは2つまでです。'));return;}if(!chosen.length)chosen=['leave_open'];}else chosen=[key];current={...current,answer:chosen};if(group==='life'&&q.id==='Q4'&&chosen.length===2)current.relationship_grouping=current.relationship_grouping||'different';else delete current.relationship_grouping;for(const c of checks)c.checked=chosen.includes(c.value);values[q.id]=current;invalidateChoice(group,q.id);clearAdaptiveUnless(group);};
  }
  field(box,'補足（任意）',current.text,v=>{current={...current,text:v||null};values[q.id]=current;invalidateChoice(group,q.id);clearAdaptiveUnless(group);},'textarea');
  if(group==='life'&&q.id==='Q4')select(box,'2つ選んだ場合の人物',{same:'同じ人物',different:'別の人物'},current.relationship_grouping||'different',v=>{if(current.answer.length===2){current.relationship_grouping=v;values[q.id]=current;invalidateChoice(group,q.id);}});
  if(group==='now')policy(box,`now:${q.id}`,'この暮らしの設定');parent.append(box);
  return ()=>{values[q.id]=current;};
}
function clearAdaptiveUnless(group){if(group!=='adaptive_answers')clearAdaptive();}
function likes(parent){
  const c=card(parent,'好きなこと、気になること');
  for(const [i,like] of data.now.likes.entries()){
    const row=node('div',undefined,'srow');field(row,'好きなこと',like.topic,v=>like.topic=v);field(row,'補足（任意）',like.detail,v=>like.detail=v||null);
    select(row,'この好みの設定',{may_elaborate:'膨らませてよい',keep:'このまま'},like.elaborate||'may_elaborate',v=>like.elaborate=v);button('外す',()=>{data.now.likes.splice(i,1);render();},false,row);c.append(row);
  }
  button('好きなことを追加',()=>{data.now.likes.push({topic:'',elaborate:'may_elaborate'});render();},false,c);
}
function settingChecks(parent){
  const c=card(parent);c.append(node('p','項目ごとに、どの内容で迎えるかを選んでください。'));
  for(const [key,check]of checkRecords){
    const type=check.question_ref?'answer':check.setting==='persona.ai_question'?'ai':check.choice_template||'world',template=settingTemplates[type];
    const box=node('fieldset',undefined,'qcard');box.append(node('legend',template.question));
    if(type==='ai')box.append(node('p',aiQuestionConflict(check.selectedAnswer,check.quote,!!data.import_batch_ids?.length)));
    else box.append(node('blockquote',check.quote));
    // Only the explanation comes from the Reader; questions and choices are fixed.
    if(check.detail)box.append(node('p',check.detail,'warn'));
    if(check.question_ref)box.append(node('p',`質問: ${check.question}`),node('p',`回答: ${check.choices.join(' / ')}${check.answer_text?`（${check.answer_text}）`:''}`));
    for(const [value,label]of Object.entries(template.choices)){
      const row=node('label',undefined,'choice'),input=node('input');input.type='radio';input.name=`check_${[...checkRecords.keys()].indexOf(key)}`;input.value=value;input.checked=check.choice===value;
      input.onchange=()=>{
        check.choice=value;check.label=label;
        if(check.question_ref){
          const conflict={topic:check.topic,quote:check.quote,question_ref:check.question_ref,answer_keys:check.answer_keys};
          data.conflict_resolutions=data.conflict_resolutions.filter(r=>checkKey(r.conflict)!==key);data.conflict_resolutions.push({conflict,choice:value});
        }else{
          const note=`「${check.quote}」について: ${label}。`;
          if(type==='ai')data.persona={...data.persona,ai_question:value};
          else if(type!=='source'){
            const old=check.note;let description=data.world.description||'';
            if(old)description=description.replace(old,'').trim();
            data.world.description=[description,note].filter(Boolean).join('\n');check.note=note;
          }
          data.review_notes[`setting:${key}`]=note;
        }
      };
      row.append(input,node('span',label));box.append(row);
    }
    c.append(box);
  }
  if(!checkRecords.size)c.append(node('p','今、確かめてほしい食い違いはありません。','muted'));
}
function augmentBody(){return {answers:data.answers,adaptive_questions:data.adaptive_questions,adaptive_answers:data.adaptive_answers};}
async function enterAdaptive(){clearAdaptive();step=5;render();}
async function finishAugment(){
  const j=await job(`/api/characters/${encodeURIComponent(augmentId)}/augment`,augmentBody());
  cleanAugment();
  $('screen').replaceChildren(node('div',`${name}の人物像に、答えた場面を足しました。`,'card'));navigation();$('heading').textContent='追加しました';button('会話に戻る',()=>location.href='/#talk',true);
}
function adaptiveScreen(parent){
  $('heading').textContent='もう少しだけ（任意）';parent.append(node('p',`選んだ反応が、どんな時に変わるのか。${data.basics.name||name||'あなたのパートナー'}のための場面を、最大4問考えます。答えずに進むこともできます。`,'muted'));
  backButton('戻る',()=>{if(adaptiveGenerated&&adaptiveIndex>0)adaptiveIndex--;else{step=4;questionIndex=Math.max(0,sceneQuestions().length-1);}render();});
  backButton('場面の質問に戻って直す',()=>{step=4;questionIndex=0;clearAdaptive();render();});
  button('深掘りを飛ばす',async()=>{clearAdaptive();await finishAugment();});
  if(!adaptiveGenerated){
    button(`${data.basics.name||name||'あなたのパートナー'}の場面を考える`,async()=>{const j=await job(`/api/characters/${encodeURIComponent(augmentId)}/adaptive`,augmentBody());data.adaptive_questions=j.result.questions;adaptiveParents=j.result.parents;adaptiveGenerated=true;adaptiveIndex=0;render();},true);
  }else if(data.adaptive_questions.length){
    const q=data.adaptive_questions[adaptiveIndex], source=adaptiveParents[q.parent_question];
    $('counter').textContent=`${adaptiveIndex+1} / ${data.adaptive_questions.length}`;
    if(source)parent.append(node('p',`さっきの「${source.title}」で「${source.choices.join(' / ')}」を選んだので、条件を少し変えて聞きます（${q.added_condition}）`,'lead-in'));
    const commit=question(card(parent),{...q,choices:{...q.choices,...catalog.questionnaire.common_choices}},'adaptive_answers');
    button(adaptiveIndex<data.adaptive_questions.length-1?'次の質問へ':'この内容を追加する',async()=>{commit();if(adaptiveIndex<data.adaptive_questions.length-1){adaptiveIndex++;render();}else await finishAugment();},true);
  }else {parent.append(node('p','今回は追加で確かめる場面はありません。','card'));button('この内容を追加する',finishAugment,true);}
}
function renderConfirmation(result,onboardingParent,onReviewChange=()=>{}){
  const parent=onboardingParent;
  parent.append(node('h2',result.name,'partner-name'),node('p',result.introduction,'intro'));
  const speaking=card(parent,'話し方');
  if(result.speaking_style){
    const v=result.speaking_style,c=speaking;
    for(const [title,value]of [['一人称',v.first_person],['普段の呼び方',v.second_person.default],['必要な時の呼び方',v.second_person.when_needed],['口調',v.register],['普段の返答',v.length.default],['長く話す時',v.length.expand_when],['語尾',(v.sentence_endings||[]).join(' / ')]])if(value){const row=node('div',undefined,'summary-row');row.append(node('span',title),node('div',value));c.append(row);}
  }
  const aiRow=node('div',undefined,'summary-row');aiRow.append(node('span','「AI なの？」への答え方'),node('div',aiQuestionChoices[data.persona.ai_question]));speaking.append(aiRow);
  const decisions=[...checkRecords.values()].filter(c=>c.choice);
  if(decisions.length){const c=card(parent,'決めたこと');for(const decision of decisions)c.append(node('p',`「${decision.quote}」: ${decision.label}`));}
  const open=card(parent,'まだ決まっていない余白');
  function group(title,intent,texts){const c=card(open,title);c.append(node('p',intent,'small muted'));if(!texts.length){c.append(node('p','今、ここに残してあることはありません。','small muted'));return;}const list=node('ul');for(const text of texts.slice(0,3))list.append(node('li',text));c.append(list);if(texts.length>3){const more=node('details');more.append(node('summary',`ほか ${texts.length-3} 件`));const rest=node('ul');for(const text of texts.slice(3))rest.append(node('li',text));more.append(rest);c.append(more);}}
  group('場面での反応',`ここは決めずに残してあり、話していくうちに${data.basics.name||name||'あなたのパートナー'}が自分で決めていきます。`,result.open_domains.map(d=>d.description));
  group('昔のこと','まだ語られていない昔のことは、関係が深まるにつれて少しずつ分かっていきます。',result.open_past.map(p=>p.about));
  group('今の暮らし','決めていない暮らしや好みは、日々の会話で少しずつ形になっていきます。',result.open_now);
  const memories=card(parent,`${data.basics.name||name||'あなたのパートナー'}の最近の日常`),list=node('ul');for(const m of result.seed_memories)list.append(node('li',m.content));memories.append(list);
  const review=card(parent,'補った内容の確認');review.append(node('p','補った内容を確かめてください。補足した項目は、もう一度人物像を作って確認できます。','check-note'));
  const supplements=new Map();
  for(const item of result.author_review){
    const c=card(review);c.append(node('p',`何を補ったか: ${item.supplemented}`),node('p',`何をもとにしたか: ${item.based_on}`));
    const choice=select(c,'この項目',{keep:'このままでよい',supplement:'補足する'},'keep',v=>{text.hidden=text.parentElement.hidden=v!=='supplement';if(v==='supplement')supplements.set(item.ref,text.value);else supplements.delete(item.ref);onReviewChange(supplements.size>0);});
    const text=field(c,'足したいこと','',v=>{if(choice.value==='supplement')supplements.set(item.ref,v);},'textarea');text.hidden=text.parentElement.hidden=true;
  }
  if(!result.author_review.length)review.append(node('p','確かめてほしい補完はありません。','muted'));
  return supplements;
}
function render(){
  if(onboardingRender){void onboardingRender();return;}
  stopVoice();$('screen').replaceChildren();navigation();$('navigation').classList.add('question-actions');$('counter').textContent='';$('error').hidden=true;
  $('heading').tabIndex=-1;$('heading').focus({preventScroll:true});window.scrollTo(0,0);
  $('steps').replaceChildren(...[[4,'追加の質問'],[5,'もう少しだけ']].map(([id,label])=>{const span=node('span',label,id===step?'on':'');if(id===step)span.setAttribute('aria-current','step');return span;}));
  const p=$('screen');shell('settings',name);
  if(step===5){adaptiveScreen(p);return;}
  if(step===4){
    const qs=sceneQuestions();$('heading').textContent='追加の質問';
    if(!qs.length){backButton('戻る',()=>location.href='/#settings');p.append(node('p','今、追加で答えられる場面はありません。','card'));button('会話に戻る',()=>location.href='/#talk');return;}
    questionIndex=Math.min(questionIndex,qs.length-1);const q=qs[questionIndex];$('counter').textContent=`${questionIndex+1} / ${qs.length}`;
    const section=catalog.questionnaire.sections.find(s=>s.id===q.section);
    const sectionTitle={relationship:'関係（あなたとの間で）',depth:'深いところ',now_life:'今の暮らし',life:'これまでの人生'}[q.section];
    if(sectionTitle)p.append(node('h2',sectionTitle));
    if(section&&(questionIndex===0||qs[questionIndex-1].section!==q.section))p.append(node('p',questionnaireNamed(section.description,data.basics.name||name),'lead-in'));
    const commit=question(card(p),q,q.group);
    backButton('戻る',()=>{if(questionIndex>0)questionIndex--;else{location.href='/#settings';return;}render();});
    backButton('場面の質問に戻って直す',()=>{questionIndex=0;clearAdaptive();render();});
    button('深掘りを飛ばす',async()=>{commit();clearAdaptive();await finishAugment();});
    function leaveRemaining(remaining){for(const item of remaining){const values=valuesFor(item.group);if(!values[item.id])values[item.id]={answer:['leave_open'],text:null};}}
    const shortcuts=node('details');shortcuts.append(node('summary','まとめて決めないでおく'));const shortcutButtons=node('div',undefined,'row');shortcuts.append(shortcutButtons);p.append(shortcuts);
    if(q.section)button('このセクションの残りは決めないでおく',async()=>{commit();let next=questionIndex+1;while(next<qs.length&&qs[next].section===q.section)next++;leaveRemaining(qs.slice(questionIndex+1,next));if(next<qs.length){questionIndex=next;render();}else await enterAdaptive();},false,shortcutButtons);
    button('残りは全部決めないでおく',async()=>{commit();leaveRemaining(qs.slice(questionIndex+1));await enterAdaptive();},false,shortcutButtons);
    button('ここまでで保存',async()=>{commit();await enterAdaptive();},false,shortcutButtons);
    button('次の質問へ',async()=>{commit();if(questionIndex<qs.length-1){questionIndex++;render();}else await enterAdaptive();},true);return;
  }
}
(async()=>{try{
  const settingsEntry=['ai','voice','input'].find(key=>params.has(key));
  if(settingsEntry){const partner=(await api('/api/partner')).partner;if(!partner){location.replace('/wizard');return;}selectPartner(partner.id);$('navigation').replaceChildren();if(settingsEntry==='ai'){const flow=onboardingAI($('screen'),{settingsEntry:true});await residentPage(partner,()=>$('screen').inert);await flow;}else {await onboardingFinishing($('screen'),{name:partner.name,settingsEntry});await residentPage(partner,()=>$('screen').inert);}return;}
  if(lookOnly){const partner=(await api('/api/partner')).partner;if(!partner){location.replace('/wizard');return;}selectPartner(partner.id);shell('settings',partner.name);$('heading').textContent='顔と表情を整える';$('subtitle').textContent='見た目は、あとから何度でも整えられます。';const editor=await editLook($('screen'));await residentPage(partner,editor.busy);return;}
  if(!augmentId)restoreWelcome=!(await api('/api/partner')).partner;
  if(!augmentId){
    if(restoreWelcome){$('navigation').replaceChildren();await onboardingAI($('screen'),{onWelcome:welcomeRestore});}
    else {if(await connectionGate($('screen')))return;await onboardingAI($('screen'),{partnerOnly:true});}
  }
  catalog=await api('/api/bootstrap/questionnaire');
  if(!augmentId){
    $('navigation').replaceChildren();
    await onboardingPartner($('screen'),{data,commonChoices:catalog.questionnaire.common_choices,
      setRender:fn=>onboardingRender=fn,readProfile:async force=>{if(force)profileText=undefined;await readProfile();},readPlan:()=>readPlan(),editWorld,forgetSettingChecks,policy,likes,question,settingChecks,
      textChanged:()=>{data.conflict_resolutions=[];forgetSettingChecks();checkRecords.clear();clearAdaptive();},
      clearAdaptive,hasChecks:()=>checkRecords.size>0,unresolvedChecks:()=>[...checkRecords.values()].some(c=>!c.choice),
      questions:id=>id==='now'?[...asked.values()].filter(q=>q.group==='now'&&!plan.reader.items.some(i=>i.covers.includes(`now:${q.id}`))):sceneQuestions().filter(q=>q.section===({scenes:'relationship',depth:'depth',life:'life'})[id]),
      adaptive:async()=>{const j=await job('/api/bootstrap/adaptive',data,{background:true});return j.result;},
      create:async(revise=false)=>{const version=++creationVersion,j=await job(revise?`/api/bootstrap/jobs/${draftJob}/revise`:'/api/bootstrap/create',data,{background:true});if(version===creationVersion){draftJob=j.id;createdResult=j.result;}},
      renderConfirmation:(p,onChange)=>renderConfirmation(createdResult,p,onChange),confirm:body=>api(`/api/bootstrap/jobs/${draftJob}/confirm`,body)
    },{secondPartner:!restoreWelcome,resumeImport:!!resumeImport});return;
  }
  if(augmentId){selectPartner(augmentId);name=(await api('/api/partner')).partner?.name||'';augmentQuestions=(await api(`/api/characters/${encodeURIComponent(augmentId)}/augment`)).questions;step=4;$('subtitle').textContent=`答えた分だけ、${name}の人物像に足されます。どの場面も任意です。`;}
  render();
  if(augmentId){
    const navigation=settingsNavigation();
    cleanAugment=navigation.register('augment',$('screen'),async()=>{if(pending)return false;await finishAugment();return true;},()=>JSON.stringify(augmentBody()));navigation.activate('augment');
    await residentPage({id:augmentId,name},()=>pending);
  }
}catch(e){report(e);}})();
