import {api,node,field,action,selectPartner} from './ui.js';
import {sequence,explanation,accordion,choiceCards,chapters,onboardingChrome,notice,partnerRoute} from './onboarding.js';
import {aiQuestionChoices,openStyleChoices} from './setting-choices.js';
import {onboardingFinishing} from './onboarding-finishing.js';
import {onboardingAI} from './onboarding-ai.js';
import {openImport} from './import.js';

export const worlds=['現代日本','現代の日本以外','和風ファンタジー','西洋ファンタジー','近未来','SF','その他'];
export function worldAxes(preset){return {region:preset!=='現代日本',era:!['現代日本','現代の日本以外'].includes(preset)};}

// The existing wizard owns generation, Reader state and question rendering.
// This module only arranges the new-partner screens and their draft input.
export async function onboardingPartner(parent,core,{secondPartner=false,resumeImport=false}={}){
  const {data}=core,extra={user_name:'',rules:'',about:'',partner_birthday:{},user_birthday:{}},indices={now:0,scenes:0,depth:0,life:0,more:0};
  const expanded={partner:0,user:0};
  let failures=0;
  let editingOpen=false;
  let flow,bringing='none',worldChoice=worlds.includes(data.world.preset)?data.world.preset:'その他',current='bringing',moreWork,createWork,aboutReview,aboutText,reviewChoices,confirmed=false,importReady=false;
  const goto=(id,options)=>flow.go(id,options),paint=()=>flow.start(current);
  core.setRender(paint);onboardingChrome();
  function text(p,label,obj,key,{required=false,multiline=false}={}){const e=field(p,label,obj[key],v=>{if(obj===data.world)core.editWorld(key,v||null);else obj[key]=v||null;if(obj===data&&key==='text')core.textChanged();},multiline?'textarea':'input');e.required=required;return e;}
  function birthday(p,key){for(const [k,label,min,max]of [['month','月',1,12],['day','日',1,31],['year','年（任意）',1,9999]]){const e=field(p,label,extra[key][k],v=>extra[key][k]=v?Number(v):undefined);e.type='number';e.min=min;e.max=max;}p.append(node('p','月日を入れると、大切な日として覚えます。','muted'));}
  function item(title,obj,key,options={}){return {title,summary:()=>obj[key]||'',render:p=>text(p,title,obj,key,options)};}
  function partnerItems(){const axes=worldAxes(worldChoice);return [
    item('名前',data.basics,'name',{required:true}),item('一人称',data.basics,'first_person'),item('口調',data.basics,'register'),
    {title:'世界観',summary:()=>data.world.preset,render:p=>{choiceCards(p,'世界観',worlds.map(v=>[v,v,({現代日本:'今の日本で暮らしています。',その他:'自由に世界を決められます。'})[v]||'']),worldChoice,v=>{worldChoice=v;core.editWorld('preset',v==='その他'?'':v);data.world.reality=null;if(!worldAxes(v).region)data.world.region=null;if(!worldAxes(v).era)data.world.era=null;paint();});if(worldChoice==='その他')text(p,'どんな世界ですか',data.world,'preset',{required:true});}},
    {...item('地域',data.world,'region'),disabled:!axes.region,reason:'現代日本では聞きません。'},
    {...item('時代',data.world,'era'),disabled:!axes.era,reason:'現代の世界では聞きません。'},
    item('世界観の補足',data.world,'description',{multiline:true}),item('種族・存在',data.world,'being'),
    {title:'年齢・性別・誕生日',summary:()=>[data.basics.age,data.basics.gender,extra.partner_birthday.month&&`${extra.partner_birthday.month}月${extra.partner_birthday.day||''}日`].filter(Boolean).join(' / '),render:p=>{text(p,'年齢（任意）',data.basics,'age');text(p,'性別（任意）',data.basics,'gender');birthday(p,'partner_birthday');}},
    {title:'「AI なの？」と聞かれたら',summary:()=>aiQuestionChoices[data.persona.ai_question],render:p=>choiceCards(p,'答え方',Object.entries(aiQuestionChoices).map(([v,l])=>[v,l,({immersive:'人として答えます。',playful:'肯定も否定もせず、はぐらかします。',honest:'真剣に聞かれたら AI であることを認め、そのうえで話し続けます。'})[v]]),data.persona.ai_question,v=>{data.persona.ai_question=v;core.forgetSettingChecks();})},
    {title:'返答の長さ・会話のルール',summary:()=>[data.basics.length,extra.rules].filter(Boolean).join(' / '),render:p=>{text(p,'返答の長さ',data.basics,'length');text(p,'会話のルール（1行にひとつ）',extra,'rules',{multiline:true});}},
    {title:'あなたとの接点',summary:()=>({A:'同じ世界で、離れて暮らす',B1:'近くに住んでいる',B2:'一緒に暮らす',C:'別の世界から画面越しに'})[data.world.contact],render:p=>choiceCards(p,'あなたとの接点',[['A','同じ世界で、離れて暮らす','メッセージや通話で話せます。'],['B1','近くに住んでいる','別々の家で暮らします。'],['B2','一緒に暮らす','同じ家で暮らします。'],['C','別の世界から画面越しに','違う世界からつながります。']],data.world.contact,v=>core.editWorld('contact',v),{recommended:['現代日本','現代の日本以外'].includes(worldChoice)?'A':'C'})}
  ];}
  function userItems(){return [item('あなたの名前',extra,'user_name'),item('呼ばれ方',data.basics,'second_person'),{title:'あなたの誕生日',summary:()=>extra.user_birthday.month?`${extra.user_birthday.month}月${extra.user_birthday.day||''}日`:'',render:p=>birthday(p,'user_birthday')},item('知っておいてほしいこと',extra,'about',{multiline:true})];}
  const groups=['now','scenes','depth','life','more'],nextGroup=id=>groups[groups.indexOf(id)+1]||'checks';
  function qs(id){return id==='more'?data.adaptive_questions.map(q=>({...q,group:'adaptive_answers',choices:{...q.choices,...core.commonChoices}})):core.questions(id);}
  function leave(id){for(const q of qs(id)){const values=q.group==='now'?data.now.life:data[q.group];if(!values[q.id])values[q.id]={answer:['leave_open'],text:null};}return nextGroup(id);}
  async function planNext(){await core.readPlan();return core.hasChecks()?'checks':'review';}
  // A stopped job offers a retry in place; after three in a row, also a way back to the AI settings.
  function stopped(body,error,retry,{label='もう一度試す',repair=true}={}){
    body.querySelector(':scope>.job-stopped')?.remove();failures++;const box=node('div',undefined,'job-stopped');body.append(box);
    // Only a stopped job can be the AI's doing; a refused input is not.
    repair=repair&&!!error.stage;
    const what=error.stage?`${error.stage}で${repair&&failures>=3?'何度か止まっています':'止まりました'}。`:`${error.message}${/[。.]$/.test(error.message)?'':'。'}`;
    notice(box,repair&&failures>=3?`${what}${label}か、下の「AI の設定を見直す」から使う AI を確かめてください。`:`${what}下の「${label}」から、もう一度試してください。`,'error');
    action(box,label,async()=>{box.remove();await retry();});
    if(repair&&failures>=3)action(box,'AI の設定を見直す',async()=>{const returnTo=current;await onboardingAI(parent,{repair:true,first:!secondPartner});await goto(returnTo,{remember:false});});
  }
  async function run(body,work,{proceed=goto,repair=true}={}){try{const target=await work();failures=0;return target;}catch(error){stopped(body,error,async()=>{const target=await run(body,work,{proceed,repair});if(target!==false)await proceed(target);},{repair});return false;}}
  const checks=body=>run(body,planNext);
  let moreVersion=0,moreSignature;
  let moreAwaited=false,checksStopped=false;
  async function moreNext(){startMore();moreAwaited=true;try{await wait(moreWork);}catch(e){moreWork=null;throw e;}finally{moreAwaited=false;}indices.more=0;return qs('more').length?'more-question':planNext();}
  // While nobody waits on 次へ, the explanation itself says when the questions are ready or stopped.
  function watchMore(body,active){startMore();const work=moreWork;work.then(value=>{if(!active()||work!==moreWork||moreAwaited)return;if(value===true){failures=0;notice(body,data.adaptive_questions.length?'追加の質問ができました。次へ進めます。':'追加で聞くことはありませんでした。次へ進めます。');return;}moreWork=null;stopped(body,value.error,()=>watchMore(body,active));});}
  function startMore(){const signature=JSON.stringify([data.basics,data.world,data.text,data.now,data.answers,data.life]);if(moreSignature!==signature){moreSignature=signature;moreWork=null;core.clearAdaptive();}if(!moreWork){const token=++moreVersion;moreWork=core.adaptive().then(result=>{if(token===moreVersion)data.adaptive_questions=result.questions;return true;},e=>({error:e}));}}
  let createSignature;
  function startCreate(revise=false){const signature=JSON.stringify(data);if(createWork&&createSignature===signature&&!revise)return;createSignature=signature;createWork=core.create(revise).then(()=>true,e=>({error:e}));}
  async function wait(work){const value=await work;if(value?.error)throw value.error;}
  async function finishCreate(revise=false){if(!createWork)startCreate(revise);try{await wait(createWork);}catch(error){createWork=null;throw error;}}
  function explanationScreen(id,next,options={}){return {id,chapter:['world'].includes(id)?'パートナー':'くわしく',explanation:true,render:p=>{current=id;return explanation(p,id,data.basics.name);},next:()=>next,...options};}
  async function reviewEnter({active,body,setReady}){const busy=node('p','まとめています…','status');body.append(busy);try{await finishCreate(current==='review-wait');if(extra.about&&(!aboutReview||aboutText!==extra.about)){aboutReview=await api(`/api/bootstrap/import/${data.id}/about/review`,{text:extra.about});aboutText=extra.about;}else if(!extra.about)aboutReview=null;if(active()){failures=0;busy.remove();notice(body,'まとめができました。');setReady(true);}}catch(error){if(active()){busy.remove();stopped(body,error,()=>paint(),{label:'もう一度まとめる'});}}}
  const screens=[
    explanationScreen('partner','bringing',{chapter:'パートナー'}),
    {id:'bringing',chapter:'パートナー',back:'partner',render:p=>{current='bringing';choiceCards(p,'持っているものはありますか？',[['none','そのまま作る','名前と世界から、一緒に決めます。'],['text','設定文がある','書いてある人物設定を読み取ります。'],['log','過去のチャットログがある','ほかの場所で話した会話を取り込みます。'],['both','両方ある','設定文と会話を引き継ぎます。']],bringing,v=>bringing=v);},next:()=>bringing==='none'?'world':bringing==='log'?'import':'profile'},
    {id:'profile',chapter:'パートナー',render:p=>{current='profile';p.append(node('h2','設定文'));text(p,'設定文（任意）',data,'text',{multiline:true});const d=node('details');d.open=true;d.append(node('summary','設定の具体化'));p.append(d);core.policy(d,'default','設定の具体化');},next:(_,{body})=>run(body,async()=>{await core.readProfile();worldChoice=worlds.includes(data.world.preset)?data.world.preset:'その他';return bringing==='both'?'import':'world';})},
    {id:'import',chapter:'パートナー',pending:true,next:()=>importReady?'world':false,enter:({setReady})=>setReady(importReady),render:async p=>{current='import';importReady=false;p.append(node('h2','過去のチャットログ'));await openImport(p,{onboarding:true,onPending:()=>{importReady=false;const next=p.nextElementSibling?.querySelector('.primary');if(next)next.disabled=true;},path:`/api/bootstrap/import/${data.id}`,text:data.text,onContinue:async function proceed(token,text){data.import_batch_ids=[...new Set([...(data.import_batch_ids||[]),token].filter(Boolean))];data.text=text||null;try{await core.readProfile(true);failures=0;}catch(error){stopped(p,error,()=>proceed(token,text));return;}worldChoice=worlds.includes(data.world.preset)?data.world.preset:'その他';importReady=true;notice(p,'取り込みが完了しました。');const next=p.nextElementSibling?.querySelector('.primary');if(next)next.disabled=false;}});}},
    explanationScreen('world','partner-fields'),
    {id:'partner-fields',chapter:'パートナー',render:p=>{current='partner-fields';p.append(node('h2','パートナーについて'));accordion(p,partnerItems(),{activeIndex:expanded.partner,onActive:i=>expanded.partner=i});},next:()=>{if(!data.basics.name?.trim())throw new Error('名前を教えてください。');if(worldChoice==='その他'&&!data.world.preset?.trim())throw new Error('どんな世界かを教えてください。');data.world.reality=null;return 'user-fields';}},
    {id:'user-fields',chapter:'パートナー',render:p=>{current='user-fields';p.append(node('h2','あなたについて'));accordion(p,userItems(),{activeIndex:expanded.user,onActive:i=>expanded.user=i});},next:()=> 'summary'},
    {id:'summary',chapter:'パートナー',render:p=>{current='summary';p.append(node('h2','パートナーとあなたの確認'));let empty=0;const list=node('div',undefined,'onboarding-summary');p.append(list);for(const [items,target,key]of [[partnerItems(),'partner-fields','partner'],[userItems(),'user-fields','user']])items.forEach((item,index)=>{if(item.disabled)return;const value=item.summary?.();if(!value){empty++;return;}const row=node('div',undefined,'summary-row'),content=node('div');row.append(node('span',item.title));content.append(node('span',value));action(content,'直す',()=>{expanded[key]=index;return goto(target);}).classList.add('quiet');row.append(content);list.append(row);});p.append(node('p',`決めていない項目は ${empty} 件。話していくうちに決まっていきます。`,'small muted'));},next:(_,{body})=>run(body,async()=>{await core.readPlan();return 'now';})},
    ...groups.flatMap(id=>[
      explanationScreen(id,id==='now'?'likes':`${id}-question`,{
        skip:({body})=>{leave(id);if(id==='more'){moreVersion++;return checks(body);}return nextGroup(id);},
        ...(id==='more'?{enter:({active,body})=>watchMore(body,active),next:(_,{body})=>run(body,moreNext)}:{next:()=>{indices[id]=0;return id==='scenes'?'open-style':id==='now'?'likes':qs(id).length?`${id}-question`:nextGroup(id);}})
      }),
      ...(id==='scenes'?[{id:'open-style',chapter:'くわしく',render:p=>{current='open-style';p.append(node('h2','決めなかった場面'),node('p',`このあとの質問で「決めないでおく」にしたところで、${data.basics.name}がどう振る舞うかを選びます。あとから設定で変えられます。`));choiceCards(p,null,openStyleChoices(data.basics.name),data.persona.open_style||'hinted',v=>data.persona.open_style=v,{recommended:null,label:'決めなかった場面'});},next:()=>{if(editingOpen){editingOpen=false;return 'confirmation';}return qs('scenes').length?'scenes-question':nextGroup('scenes');}}]:[]),
      ...(id==='now'?[{id:'likes',chapter:'くわしく',render:p=>{current='likes';core.likes(p);},next:()=>{if(data.now.likes.some(l=>!l.topic?.trim()))throw new Error('好きなことを入力するか、空の項目を外してください。');return qs('now').length?'now-question':'scenes';},leave:()=>leave('now')}]:[]),
      {id:`${id}-question`,chapter:'くわしく',render:p=>{current=`${id}-question`;const questions=qs(id),q=questions[indices[id]];if(!q)return;return core.question(p,q,q.group);},next:async(commit,{body})=>{commit?.();if(indices[id]+1<qs(id).length){indices[id]++;await flow.start(current,{top:true});return false;}return id==='more'?checks(body):nextGroup(id);},back:()=>{if(indices[id]>0){indices[id]--;return `${id}-question`;}return id==='now'?'likes':id==='scenes'?'open-style':id;},leave:({body})=>id==='more'?(leave(id),checks(body)):leave(id)}
    ]),
    {id:'checks',chapter:'くわしく',enter:({setReady})=>setReady(!checksStopped),render:async p=>{current='checks';checksStopped=false;try{await core.readPlan();failures=0;}catch(error){checksStopped=true;stopped(p,error,()=>paint());return;}if(!core.hasChecks()){await goto('review',{remember:false});return;}p.append(node('h2','確かめてほしいこと'));core.settingChecks(p,{inline:true});},next:()=>{if(core.unresolvedChecks())throw new Error('項目ごとに内容を選んでください。');return 'review';}},
    {id:'review',chapter:'くわしく',pending:true,next:()=> 'confirmation',render:p=>{current='review';return explanation(p,'review',data.basics.name);},enter:reviewEnter},
    {id:'confirmation',chapter:'くわしく',label:'この内容で迎える',render:p=>{current='confirmation';const row=node('div',undefined,'summary-row');row.append(node('span','決めなかった場面'),node('span',openStyleChoices(data.basics.name).find(([v])=>v===(data.persona.open_style||'hinted'))[1]));action(row,'直す',()=>{editingOpen=true;return goto('open-style');});reviewChoices=core.renderConfirmation(p,has=>{const b=p.nextElementSibling?.querySelector('.primary');if(b)b.textContent=has?'補足を反映して確認する':'この内容で迎える';});p.append(row);if(aboutReview){p.append(node('h2','知っておいてほしいことの保存先'));for(const c of aboutReview.candidates){const row=node('label',undefined,'choice'),input=node('input');input.type='checkbox';input.checked=c.selected!==false;input.onchange=()=>c.selected=input.checked;row.append(input,node('span',`${c.destination_label}: ${c.content}${c.private?'（'+c.category_label+'・個人的なこと）':''}`),node('blockquote',c.quote));p.append(row);if(c.existing_facts?.length){choiceCards(p,'重なる事実',[['both','両方残す','今ある事実と分けて保存します。'],...c.existing_facts.map(f=>[String(f.id),'この事実とまとめる',f.content])],c.merge||'both',v=>c.merge=v);}}if(!aboutReview.candidates.length)p.append(node('p','保存できる事実・お願いの候補はありません。文を直す場合は戻って編集できます。'));}},next:async(_,{body})=>{
      if(!confirmed&&reviewChoices?.size){if([...reviewChoices.values()].some(v=>!v.trim()))throw new Error('補足を入力してください。');Object.assign(data.review_notes,Object.fromEntries(reviewChoices));startCreate(true);return 'review-wait';}
      const days=[];for(const kind of ['partner_birthday','user_birthday']){const d=extra[kind];if(d.month||d.day||d.year){if(!d.month||!d.day)throw new Error('誕生日の月と日を入力してください。');days.push({kind,name:kind==='partner_birthday'?`${data.basics.name}の誕生日`:'あなたの誕生日',...d});}}
      const selected=aboutReview?.candidates.filter(c=>c.selected!==false)||[];
      const save=async()=>{await core.confirm({open_style:data.persona.open_style,days,user_name:extra.user_name||'',rules:(extra.rules||'').split(/\r?\n/).map(s=>s.trim()).filter(Boolean),about_token:aboutReview?.token||null,about_keys:selected.map(c=>c.id),about_decisions:Object.fromEntries(selected.filter(c=>c.existing_facts?.length).map(c=>[c.id,c.merge&&c.merge!=='both'?Number(c.merge):null]))});return true;};
      const finish=async()=>{confirmed=true;selectPartner(data.id);await onboardingFinishing(parent,{name:data.basics.name,secondPartner,onBack:()=>goto('confirmation')});};
      if(await run(body,save,{proceed:finish,repair:false}))await finish();return false;
    }},
    {id:'review-wait',chapter:'くわしく',pending:true,next:()=> 'confirmation',render:p=>{current='review-wait';return explanation(p,'review',data.basics.name);},enter:reviewEnter}

  ];
  function progressFor(screen){
    let route;
    if(screen.chapter==='パートナー')route=partnerRoute(bringing);
    else if(screen.chapter==='くわしく'){const part=groups.find(id=>screen.id===id||screen.id===`${id}-question`||(id==='now'&&screen.id==='likes'));if(part){route=[part,...(part==='now'?['likes']:[]),...qs(part).map((q,i)=>`${part}-question:${i}`)];const key=screen.id.endsWith('-question')?`${screen.id}:${indices[part]}`:screen.id;return {label:({now:'今のこと',scenes:'場面の質問',depth:'深いところ',life:'これまでの人生',more:'もう少しだけ'})[part],current:route.indexOf(key)+1,total:route.length};}route=[screen.id];}
    else route=[screen.id];
    const group=groups.find(id=>screen.id===`${id}-question`),id=group?`${screen.id}:${indices[group]}`:screen.id==='review-wait'?'review':screen.id;
    return {current:route.indexOf(id)+1,total:route.length};
  }
  flow=sequence(parent,screens,{telemetryFirst:!secondPartner,visibleChapters:secondPartner?chapters.filter(c=>!['AI','声で入力','バックアップ'].includes(c)):chapters,progressFor,onDone:()=>{location.href='/#talk';}});
  await flow.start(resumeImport?'import':'bringing');return {go:goto,data,extra};
}
