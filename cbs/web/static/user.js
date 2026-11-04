import {newsEntries,refreshNews,newsScreen} from './news.js';
import {recentActivities} from './note-activities.js';
import {partnerNote} from './partner-note.js';
import {dayNotice} from './days.js';
import {startShareNotice} from './share.js';
import {microphone} from './stt.js';
import {showCostNotice, checkCostNotice} from './costs.js';
import {showFailures, refreshFailures} from './failures.js';
import {photoComposer, photoHistory, sendingPhotos} from './photos.js';
import {requestNotice} from './requests.js';
import {api, apiError, scoped, $, node, face, icon, chatBand, shell, report, partnerId, selectPartner, plainMarkdown, displayDefaults, replyBubbles, typingIndicator, waitingPresentation} from './ui.js';
import {settingsScreen, connectionGate, settingsRoute} from './settings.js';
import {leaveSettings,clearSettingsNavigation,settingsHash} from './settings-navigation.js';
import {configureVoice, stopVoice, voiceButton, autoplayTurn} from './voice.js';
import {knowingScreen} from './knowing.js';
import {diaryScreen} from './diary.js';
import {partnerHeader} from './note-header.js';
import {startResident, notifyResident, markResidentReady} from './resident.js';
let band;
let display={...displayDefaults}, bubbleCancels=[];
let partner, look, session, history=[], busy=false, ready=false, viewVersion=0, lastDay, waiting, openingWait, sendSequence=0, notifyForming=true, pendingResidentTurn=new URLSearchParams(location.search).get('resident_turn'),pendingResidentFocus=false,navigating=false;
const attachments=photoComposer({composer:$('composer'),input:$('message'),button:$('photo-add'),picker:$('photo-files'),previews:$('photo-previews'),status:$('photo-status'),onError:report,active:()=>ready&&(location.hash||'#talk')==='#talk'});
const mic=microphone({button:$('mic'),status:$('mic-status'),input:$('message'),composer:$('composer'),stopVoice,onError:report,active:()=>ready&&!busy&&(location.hash||'#talk')==='#talk',onLock:value=>{$('send').disabled=value||busy;},openTalk:async()=>{if(!ready)return false;if(location.hash!=='#talk'){window.history.replaceState(null,'','#talk');await view();}return true;}});
function clock(time){return new Date(time).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'});}
function dateKey(time){const d=new Date(time);return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;}
function separator(time) {
  if(lastDay&&dateKey(lastDay)===dateKey(time))return;
  const text=new Date(time).toLocaleDateString('ja-JP',{month:'long',day:'numeric',weekday:'long'});
  const previous=lastDay?new Date(lastDay):null,current=new Date(time);
  const gap=previous?Math.round((Date.UTC(current.getFullYear(),current.getMonth(),current.getDate())-Date.UTC(previous.getFullYear(),previous.getMonth(),previous.getDate()))/86400000):0;
  $('messages').append(node('div',text+(gap>1?` · ${gap}日ぶりの会話`:''),'sep'));lastDay=time;
}
function formingNotice(article) {
  if(!notifyForming||!article||article.querySelector('.forming-notice'))return;
  const line=node('p',`${partner.name}のことを、またひとつ知りました。 `,'small muted forming-notice'),link=node('a','ノートで見る');link.href='#note';line.append(link);article.lastElementChild.append(line);
}
function message(text,time,{user=false,expression='neutral',proactive=false,thinking=false,animate=false,turnId,autoplay=false,pending=false,selfForming=false,requestNotices=[],dayNotices=[],live=false}={}) {
  const article=node('article',undefined,`${user?'mine':'msg'}${thinking?' thinking':''}${animate?' arrive':''}`), body=node('div'), who=node('div',undefined,'who');
  who.append(node('b',user?'あなた':partner.name,'sr-only'));
  const meta=node('div',undefined,'message-meta');
  if(proactive)who.append(node('span',`${partner.name}から`,'badge'));
  if(!thinking){const t=node('time',clock(time));t.dateTime=time;meta.append(t);}
  const content=node('div',text,'text');body.append(who,content,meta);article.append(...(user?[]:[face(partner.name,expression,look.expressions)]),body);
  if(thinking){article.setAttribute('aria-live','off');typingIndicator(content,partner.name);}
  else if(!user){const cancel=replyBubbles(content,text,display,{animate:live,onAppend:previousHeight=>{if(previousHeight-window.innerHeight-window.scrollY<180)scrollEnd();}});if(live)bubbleCancels.push(cancel);}
  if(turnId)article.dataset.turnId=turnId;
  if(!user&&!thinking)band?.expression(expression);
  $('messages').append(article);if(!user&&!thinking)voiceButton(meta,turnId,{autoplay:autoplay&&!mic.active,pending,proactive});if(selfForming)formingNotice(article);requestNotice(article,requestNotices);dayNotice(article,dayNotices);return {article,content};
}
function renderHistory() {
  refreshShareInvite();
  for(const cancel of bubbleCancels)cancel();bubbleCancels=[];
  $('screen').replaceChildren(node('div',undefined,'messages'));$('screen').firstChild.id='messages';lastDay=null;
  $('content').querySelector('.chat-band')?.remove();band=null;
  $('messages').setAttribute('role','log');$('messages').setAttribute('aria-live','polite');$('messages').setAttribute('aria-relevant','additions');
  for(const turn of history){
    if(turn.user_text||turn.photos?.length){separator(turn.created_at);const shown=message(turn.user_text,turn.created_at,{user:true});if(turn.status==='completed')readReceipt(shown);photoHistory(shown.content.parentNode,turn.photos,async id=>{turn.photos=turn.photos.filter(p=>p.id!==id);});}
    if(turn.assistant_text&&turn.status==='completed'){separator(turn.assistant_at||turn.created_at);message(turn.assistant_text,turn.assistant_at||turn.created_at,{expression:turn.expression||'neutral',proactive:turn.kind==='proactive',turnId:turn.id,selfForming:turn.self_forming,requestNotices:turn.request_notices,dayNotices:turn.day_notices});}
  }
  band=chatBand($('content'),partner.name,look.expressions,history.filter(t=>t.assistant_text&&t.status==='completed').at(-1)?.expression||'neutral');newsEntries('talk');
  if(!history.length){separator(new Date().toISOString());}
  if(!history.some(t=>t.user_text))$('screen').append(node('p','今日のことでも、ふと思ったことでも。ひとことから話してみてください。','hint'));
}
function scrollEnd(){
  if((location.hash||'#talk')!=='#talk')return;
  const target=pendingResidentTurn==null?null:[...document.querySelectorAll('[data-turn-id]')].find(e=>e.dataset.turnId===String(pendingResidentTurn));
  if(target){pendingResidentTurn=null;target.scrollIntoView?.({block:'center'});return;}
  window.scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'});
}
function refreshShareInvite(){
  const version=viewVersion,cid=partner?.id;
  void startShareNotice($('composer'),{active:()=>version===viewVersion&&cid===partner?.id&&(location.hash||'#talk')==='#talk'});
}
function setBusy(value){busy=value;attachments.setBusy(value);mic.setBusy(value);$('send').disabled=value;$('message').disabled=value;$('send-status').textContent=value?'':'Enter で送る · Shift + Enter で改行';}
async function loadConversation() {
  const sessions=await api('/api/sessions');
  const histories=await Promise.all(sessions.map(s=>api(`/api/sessions/${s.id}/turns`)));
  history=histories.flat().sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id-b.id);
  session=sessions[0]||await api('/api/sessions',{});
}
function readReceipt(shown){
  if(!shown||shown.article.querySelector('.read-receipt'))return;
  shown.article.querySelector('.message-meta').append(node('small','既読','read-receipt'));
}
function paintWaiting(own,state){
  const follow=document.documentElement.scrollHeight-window.innerHeight-window.scrollY<180;
  if(own.shownUser){own.shownUser.article.hidden=!state.user;if(state.read)readReceipt(own.shownUser);}
  if(state.typing&&!state.done&&$('messages')){
    if(!own.pending?.article.isConnected)own.pending=message('',new Date().toISOString(),{thinking:true,expression:'thinking'});
  }else own.pending?.article.remove();
  if(follow)scrollEnd();
}
function beginWaiting(own,cfg=display){own.presentation=waitingPresentation(cfg,{paint:state=>paintWaiting(own,state)});}
async function opening(background=false) {
  if(busy||sendSequence>0)return;
  const own={text:`${partner.name}が考えています…`,sequence:sendSequence};openingWait=own;
  beginWaiting(own,{...display,delay_user:false});
  try {
    const result=await api('/api/proactive/opening',{session_id:session.id,after_turn_id:Math.max(0,...history.map(t=>t.id)),...(background?{background:true}:{})});
    own.presentation.finish();
    if(openingWait!==own||own.sequence!==sendSequence)return;
    if(result.speak)await proactiveReply(result,{notify:background});
    return result;
  }catch(e){own.presentation.finish();if(openingWait===own)report(e);}finally{void refreshFailures();if(openingWait===own)openingWait=null;scrollEnd();}
}
async function proactiveReply(result,{notify=true}={}){
  const turn={...result,id:result.turn_id,created_at:result.assistant_at,kind:'proactive',status:'completed',user_text:''};
  if(history.some(t=>t.id===turn.id))return;
  history.push(turn);
  if($('messages')){separator(result.assistant_at);message(result.assistant_text,result.assistant_at,{expression:result.expression,proactive:true,animate:true,turnId:result.turn_id,autoplay:true,live:true});scrollEnd();}
  if(notify)await notifyResident(partner,result,session.id);
}
async function send(e) {
  e.preventDefault();if(busy||mic.active||!session)return;const text=$('message').value.trim();if(!text&&!attachments.count)return;
  openingWait?.presentation.finish();openingWait=null;
  stopVoice();$('error').hidden=true;const at=new Date().toISOString();separator(at);const shownUser=message(text,at,{user:true});$('screen').querySelector('.hint')?.remove();$('message').value='';resizeMessage();
  // Text and photos leave the input together and appear in your bubble together; a failure puts both back.
  const taken=attachments.take(),sending=sendingPhotos(shownUser.content.parentNode,taken);setBusy(true);
  const requestStarted=performance.now(), requestSession=session.id, requestPartner=partner.id, requestSequence=++sendSequence;
  const ownWaiting={userText:text,userAt:at,shownUser};waiting=ownWaiting;beginWaiting(ownWaiting);scrollEnd();let reply=false;
  try{
    const photos=await attachments.payload(taken);
    const response=await fetch(scoped(`/api/sessions/${session.id}/messages/stream`),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:text,...(photos.length?{photos}:{})})});
    if(!response.ok)throw apiError(await response.json());
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
    function event(frame){
      if(session?.id!==requestSession||partner.id!==requestPartner)return;
      const follow=document.documentElement.scrollHeight-window.innerHeight-window.scrollY<180;
      const lines=frame.split('\n'),type=lines.find(line=>line.startsWith('event:'))?.slice(6).trim();const data=JSON.parse(lines.filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n'));
      // Only a checked reply event may supply visible assistant text.
      if(type==='error')throw apiError(data);
      if(type==='llm_failures')showFailures(data.notices);
      if(type==='reply'){reply=true;ownWaiting.replied=true;ownWaiting.presentation.finish(true);const turn={...data,id:data.turn_id,user_text:text,created_at:at,status:'completed',kind:'conversation'};history.push(turn);sending?.remove();attachments.release(taken);photoHistory(ownWaiting.shownUser.content.parentNode,data.photos,async id=>{turn.photos=turn.photos.filter(p=>p.id!==id);});if($('messages')){separator(data.assistant_at);message(data.assistant_text,data.assistant_at,{expression:data.expression,turnId:data.turn_id,autoplay:true,live:true,pending:data.expression_pending});}}
      if(type==='reply'){const shown=performance.now()-requestStarted;setBusy(false);if(waiting===ownWaiting)waiting=null;window.cbsLatency??=new Map();window.cbsLatency.set(data.turn_id,{reply_shown_ms:shown,input_enabled_ms:performance.now()-requestStarted});try{window.sessionStorage.setItem(`cbs.latency.${data.turn_id}`,JSON.stringify(window.cbsLatency.get(data.turn_id)));}catch{/* Optional telemetry must not interrupt the reply stream. */}if(!$('composer').hidden)$('message').focus();}
      if(type==='expression'){
        const turn=history.find(t=>t.id===data.turn_id),wasPending=turn?.expression_pending;if(turn){turn.expression=data.expression;turn.expression_pending=false;}
        const article=[...document.querySelectorAll('[data-turn-id]')].find(e=>e.dataset.turnId===String(data.turn_id));
        article?.querySelector('.face')?.replaceWith(face(partner.name,data.expression,look.expressions));
        if(history.filter(t=>t.assistant_text&&t.status==='completed').at(-1)?.id===data.turn_id)band?.expression(data.expression);
        if(wasPending&&requestSequence===sendSequence&&!mic.active)autoplayTurn(data.turn_id);
      }
      if(type==='important_days'){
        const turn=history.find(t=>t.id===data.turn_id);if(turn)turn.day_notices=data.notices;
        dayNotice([...document.querySelectorAll('[data-turn-id]')].find(e=>e.dataset.turnId===String(data.turn_id)),data.notices);
      }
      if(type==='partner_requests'){
        const turn=history.find(t=>t.id===data.turn_id);if(turn)turn.request_notices=data.notices;
        requestNotice([...document.querySelectorAll('[data-turn-id]')].find(e=>e.dataset.turnId===String(data.turn_id)),data.notices);
      }
      if(type==='self_forming'){
        const turn=history.find(t=>t.id===data.turn_id);if(turn)turn.self_forming=true;
        formingNotice([...document.querySelectorAll('[data-turn-id]')].find(e=>e.dataset.turnId===String(data.turn_id)));
      }
      if(type==='cost_notice')showCostNotice($('messages')||$('screen'),data);
      if(follow)scrollEnd();
    }
    while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done}).replaceAll('\r\n','\n');let split;while((split=buffer.indexOf('\n\n'))!==-1){const frame=buffer.slice(0,split);buffer=buffer.slice(split+2);if(frame.startsWith('event:'))event(frame);}if(done)break;}
    if(!reply)throw new Error('接続が途切れました。会話を読み直してください。');
  }catch(error){ownWaiting.presentation.finish(reply);if(requestSequence===sendSequence)report(error);if(!reply){$('message').value=text;resizeMessage();sending?.remove();attachments.restore(taken);}}
  finally{void refreshFailures();if(reply)refreshShareInvite();if(waiting===ownWaiting){waiting=null;setBusy(false);}if(!reply){try{await loadConversation();if((location.hash||'#talk')==='#talk')renderHistory();}catch(error){report(error);}}scrollEnd();if(!$('composer').hidden)$('message').focus();}
}
async function noteScreen(parent) {
  const note=await api('/api/note');
  look=await api('/api/look');
  const tabs=node('div',undefined,'row note-tabs');tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','ノートの種類');const content=node('div',undefined,'set');content.id='notebook-panel';content.setAttribute('role','tabpanel');parent.append(tabs,content);
  const buttons=[],factView={public:5,private:5,privateOpen:false};
  async function show(which){
    for(const [i,b]of buttons.entries()){b.setAttribute('aria-selected',String(i===which));b.tabIndex=i===which?0:-1;}
    content.setAttribute('aria-labelledby',`notebook-tab-${which}`);const page=node('div',undefined,'set');content.replaceChildren(page);
    $('heading').textContent=[`${note.name}のこと`,'ふたりのこと','あなたのこと'][which];
    $('subtitle').textContent=[`${note.name||'あなたのパートナー'}を知っていく、ひとつの手帖。`,'話したことも、交わした言葉も、ふたりのあわいに。',`${note.name}が話しながら分かってきた、あなたのこと。`][which];
    const refresh=async()=>{Object.assign(note,await api('/api/note'));await show(which);};
    if(which===0)haruNote(page,note);else if(which===1)await diaryScreen(page,note,refresh);else await knowingScreen(page,note.knowing||{understandings:[],facts:[],stopped:false},note.name,refresh,{factView});
  }
  for(const [i,title]of [`${note.name}のこと`,'ふたりのこと','あなたのこと'].entries()){
    const button=node('button',title,'btn');button.type='button';button.id=`notebook-tab-${i}`;button.setAttribute('role','tab');button.setAttribute('aria-controls',content.id);button.onclick=()=>show(i);
    button.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?2:(i+(e.key==='ArrowRight'?1:2))%3;buttons[next].focus();show(next);}};buttons.push(button);tabs.append(button);
  }
  await show(location.hash==='#note/user'?2:0);
}
function haruNote(parent,note) {
  partnerHeader(parent,note,look);
  parent.append(node('p','会話のなかで、ふと教えてくれたこと。自然な人となりを、少しずつ書き留めています。','muted'));
  if(note.today_haru){const c=node('section',undefined,'card');c.append(node('h3',`今日の${note.name}`),node('p',note.today_haru));parent.append(c);}
  note.books=note.books||[];
  note.self_forming=note.self_forming||[];
  const empty=!(note.self_traits||[]).length&&!note.self_forming.length&&!note.today_haru&&!note.books.length&&!note.now_summary&&!note.interests.length&&!note.activities.length&&!note.activity_weeks?.length&&!note.past.length;
  if(empty){parent.append(node('p',`まだ、白いページです。話していくうちに、${note.name||'あなたのパートナー'}のことが少しずつここに残っていきます。`,'note-closing'));return;}
  const list=node('div',undefined,'note-list');parent.append(list);
  if(note.now_summary){const c=node('section',undefined,'card');c.append(node('h3','今の様子'),node('p',note.now_summary));list.append(c);}
  recentActivities(list,note.activities,note.activity_weeks);
  if(note.books.length){const c=node('section',undefined,'card');c.append(node('h3','読んでいる本'));for(const b of note.books)c.append(node('p',`${b.title} / ${b.author}`),node('p',`${b.read_count} / ${b.chunk_count} 区切り${b.finished_at?'・読了':''}`,'small muted'));list.append(c);}
  if(note.interests.length){const c=node('section',undefined,'card');c.append(node('h3','好きなこと、気になること'));const items=node('ul',undefined,'interest-list');c.append(items);for(const i of note.interests){const item=node('li');item.append(node('span',i.topic));if(i.new)item.append(node('span','NEW','badge note-new'));if(i.detail&&i.detail!==i.topic)item.append(node('p',i.detail,'small muted'));items.append(item);}list.append(c);}
  partnerNote(list,note);
  if(note.past.length){const c=node('section',undefined,'card');c.append(node('h3','教えてくれた昔の話'));for(const text of note.past)c.append(node('p',text));list.append(c);}
  parent.append(node('p',note.has_undisclosed_past?'まだ知らないことも、きっと。話していくうちに、少しずつ。':'この先も、少しずつページが増えていきます。','note-closing'));
}
async function view() {
  if(navigating)return;
  navigating=true;
  const targetHash=location.hash,previousHash=settingsHash();
  try{if(previousHash&&previousHash!==targetHash){window.history.replaceState(null,'',previousHash);if(!await leaveSettings())return;window.history.replaceState(null,'',targetHash);}}finally{navigating=false;}
  clearSettingsNavigation();
  if(!ready)return;for(const cancel of bubbleCancels)cancel();bubbleCancels=[];mic.cancel();stopVoice();const version=++viewVersion;let tab=(location.hash.slice(1)||'talk').split('/')[0];if(!['talk','note','settings','news'].includes(tab))tab='talk';shell(tab,partner.name);newsEntries(tab);void refreshNews();
  $('content').querySelector('.chat-band')?.remove();band=null;const heading=$('heading').closest('header');if(heading)heading.hidden=tab==='talk';
  $('heading').textContent=tab==='talk'?`${partner.name}と話す`:tab==='note'?`${partner.name}のこと`:tab==='news'?'お知らせ':'設定';$('subtitle').textContent=tab==='talk'?'日々のことを、気兼ねなく。':tab==='note'?`${partner.name||'あなたのパートナー'}を知っていく、ひとつの手帖。`:'';
  $('composer').hidden=tab!=='talk';$('content').classList.toggle('chat-col',tab==='talk');
  // In a long chat the page top is far away; keep failure text beside the input instead.
  if(tab==='talk')$('composer').prepend($('llm-failures'),$('error'));else $('screen').before($('llm-failures'),$('error'));$('screen').replaceChildren();$('error').hidden=true;if($('llm-failures'))$('llm-failures').hidden=true;void refreshFailures();
  if(tab==='talk'){const cfg=await api('/api/settings');if(version!==viewVersion)return;display={...displayDefaults,...cfg.display};configureVoice(cfg.tts);attachments.configure(cfg.photos);mic.configure(cfg.stt_status);notifyForming=cfg.early?.notify??true;renderHistory();const ownBand=band;void api('/api/note').then(note=>{if(version!==viewVersion||band!==ownBand)return;ownBand.summary(note.now_summary);if(!history.filter(t=>t.assistant_text&&t.status==='completed').at(-1)?.expression)ownBand.expression(note.current_expression||'neutral');}).catch(()=>{});void checkCostNotice($('messages'),cfg);if(busy&&waiting&&!waiting.replied){if(waiting.shownUser){separator(waiting.userAt);waiting.shownUser=message(waiting.userText,waiting.userAt,{user:true});$('screen').querySelector('.hint')?.remove();}waiting.presentation.paint();}if(openingWait){openingWait.presentation.paint();}scrollEnd();if(pendingResidentFocus){pendingResidentFocus=false;$('message').focus();}return;}
  const detached=node('div',undefined,'set');
  try{const markRead=await (tab==='news'?newsScreen(detached):tab==='note'?noteScreen(detached):settingsScreen(detached,partner.name));if(version===viewVersion){$('screen').append(detached);if(tab==='news')await markRead();if(tab==='settings'&&settingsRoute(location.hash)==='relationship'){const target=$(/days/.test(location.hash)?'partner-days':'partner-requests');target?.focus({preventScroll:true});target?.scrollIntoView?.({block:'start'});}}}catch(e){if(version===viewVersion)report(e);}if(tab!=='settings'||settingsRoute(location.hash)!=='relationship')window.scrollTo(0,0);
}
for(const [id,label,symbol]of [['photo-add','写真を添える','photo'],['mic','マイクで話す','mic'],['send','送る','send']]){const b=$(id);b.setAttribute('aria-label',label);b.classList.add('icon-button');b.replaceChildren(icon(symbol));}
function resizeMessage(){const e=$('message');e.style.height='auto';const line=parseFloat(window.getComputedStyle(e).lineHeight)||30;e.style.height=`${Math.min(e.scrollHeight,line*6+16)}px`;}
$('message').addEventListener('input',resizeMessage);
window.addEventListener('resize',resizeMessage);
$('composer').addEventListener('submit',send);
$('message').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('composer').requestSubmit();}});
window.addEventListener('hashchange',view);
// Failure text in the dock makes it taller; keep the last message clear of it.
if(window.ResizeObserver)new ResizeObserver(()=>{
  const end=document.documentElement.scrollHeight-window.innerHeight-window.scrollY<180;
  // Measure from the dock's top so a phone's bottom tab bar is included.
  $('content').style.paddingBottom=$('composer').hidden?'':`${window.innerHeight-$('composer').getBoundingClientRect().top+40}px`;
  if(end)window.scrollTo(0,document.documentElement.scrollHeight);
}).observe($('composer'));
const failurePoll=setInterval(()=>{if(ready&&!document.hidden)void refreshFailures();},10000);
failurePoll.unref?.();
window.addEventListener('focus',()=>{if(ready){void refreshFailures();void refreshNews();}});
const newsPoll=setInterval(()=>{if(ready&&!document.hidden)void refreshNews();},60000);newsPoll.unref?.();
(async()=>{try{
  const {showRestoreNotice}=await import('./restore.js');
  void showRestoreNotice().catch(()=>{});
  partner=(await api('/api/partner')).partner;
  if(!partner&&partnerId){selectPartner('');partner=(await api('/api/partner')).partner;}
  if(!partner){selectPartner('');location.replace('/wizard'+location.search);return;}
  if(!location.hash.startsWith('#settings')&&await connectionGate($('screen'))){shell('settings',partner.name);$('composer').hidden=true;return;}
  selectPartner(partner.id);look=await api('/api/look');$('message').placeholder=`${partner.name}に話しかける…`;
  await loadConversation();ready=true;await view();await markResidentReady();
  await startResident({opening,settings:()=>api('/api/settings'),busy:()=>busy||mic.active||!!openingWait,
    habit:()=>api('/api/proactive/habit',{session_id:session.id,after_turn_id:Math.max(0,...history.map(t=>t.id))}),
    idle:()=>api('/api/proactive/idle',{session_id:session.id,after_turn_id:Math.max(0,...history.map(t=>t.id))}),
    onReply:proactiveReply,
    onOpen:async target=>{
      if(target.characterId!==partner.id)return;
      pendingResidentTurn=target.turnId;
      if(location.hash!=='#talk')location.hash='#talk';else await view();
    },onFocus:async()=>{
      pendingResidentFocus=true;
      if(location.hash!=='#talk')location.hash='#talk';else await view();
    },onError:report});
}catch(e){report(e);}})();
