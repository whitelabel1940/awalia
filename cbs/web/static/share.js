import {api,node,action,field,openExternal,partnerId} from './ui.js';
import {startCard,diaryCard,pngBlob} from './share-card.js';

const COMPOSE={x:'https://x.com/intent/post?text=',bluesky:'https://bsky.app/intent/compose?text=',misskey:'https://misskey-hub.net/share/?text='};
const START_TEXT=name=>`AI のパートナー、${name}と話しはじめました。`;
const DIARY_TEXT=(name,day)=>{const [,m,d]=day.split('-').map(Number);return `${name}とのふたりの日記（${m}月${d}日）`;};
export const composeUrl=(service,text)=>COMPOSE[service]+encodeURIComponent(text);
const record=(where,service)=>{void api('/api/telemetry/share',{where,service}).catch(()=>{});};
const invitationVersions=new WeakMap();

export async function shareDialog({where,entry}){
  const cid=partnerId;
  const state=await api('/api/share/state');
  if(!state.available||cid!==partnerId)return;
  const look=await api('/api/look'),day=entry?.day||state.day,name=state.name;
  if(cid!==partnerId)return;
  const asset=slot=>`/looks/${encodeURIComponent(cid)}/${slot}?v=${Date.now()}`;
  const faceUrl=cid&&look.expressions?.includes('neutral')?asset('neutral'):null;
  const coverUrl=cid&&look.cover?asset('cover'):null;
  const dialog=node('dialog',undefined,'share-dialog'),body=node('div',undefined,'set');
  dialog.setAttribute('aria-label',where==='start'?'話しはじめのシェア':'日記のシェア');
  const preview=node('div',undefined,'share-preview'),status=node('p','画像を作っています…','status');status.setAttribute('role','status');
  body.append(preview);
  if(where==='diary'){
    // Diaries are written from private conversation; ask for a careful look before anything leaves.
    body.append(node('p','日記は会話から書かれています。名前、住んでいる場所、職場、体調など、公開したくない情報が入っていないか、画像と投稿の文をよく確かめてから投稿してください。','warn share-caution'));
  }
  let partnerComment=true,userComment=false,revision=0,canvas,closed=false;
  const controls=[];
  function check(label,checked,change){
    const wrap=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=checked;
    input.onchange=()=>{change(input.checked);void render();};wrap.append(input,node('span',label));body.append(wrap);
  }
  if(where==='diary'){
    check(`${name}からの一言を載せる`,true,value=>partnerComment=value);
    if(entry.user_comment)check('あなたからの一言を載せる',false,value=>userComment=value);
  }
  const text=field(body,'投稿の文',`${where==='start'?START_TEXT(name):DIARY_TEXT(name,day)}\n#${state.hashtag}\n${state.url}`,()=>{},'textarea');
  const imageActions=node('div',undefined,'row');body.append(imageActions);
  function button(parent,label,fn){const b=action(parent,label,async()=>{if(!canvas)return;await fn(canvas);});controls.push(b);b.disabled=true;return b;}
  button(imageActions,'画像を保存',async current=>{
    record(where,'save');
    const url=URL.createObjectURL(await pngBlob(current)),link=node('a');link.href=url;
    link.download=`awalia-${where}-${where==='start'?day.replaceAll('-',''):day}.png`;
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  if(typeof navigator.clipboard?.write==='function'&&typeof ClipboardItem!=='undefined'){
    button(imageActions,'画像をコピー',current=>{
      record(where,'copy');
      // Start the clipboard write during the user's activation, even while PNG encoding is pending.
      return navigator.clipboard.write([new ClipboardItem({'image/png':pngBlob(current)})]);
    });
  }
  const services=node('div',undefined,'row');body.append(services);
  for(const [service,label]of [['x','X'],['bluesky','Bluesky'],['misskey','Misskey']]){
    button(services,label,()=>{record(where,service);return openExternal(composeUrl(service,text.value));});
  }
  body.append(node('p','画像は保存かコピーをして、投稿画面に貼ってください','small muted'),status);
  const closing=node('div',undefined,'row');body.append(closing);
  function close(){closed=true;revision++;dialog.close();dialog.remove();}
  action(closing,'閉じる',close);dialog.oncancel=e=>{e.preventDefault();close();};
  dialog.append(body);document.body.append(dialog);dialog.showModal();
  async function render(){
    const own=++revision;canvas=null;preview.replaceChildren();controls.forEach(b=>b.disabled=true);status.textContent='画像を作っています…';
    const partnerText=partnerComment?entry?.partner_comment||'':'',userText=userComment?entry?.user_comment||'':'';
    try{
      const next=where==='start'?await startCard({name,day,coverUrl,faceUrl}):await diaryCard({day,summary:entry.summary,name,partnerComment:partnerText,userComment:userText,faceUrl});
      if(closed||own!==revision)return;
      next.setAttribute('role','img');next.setAttribute('aria-label',where==='start'?`${name}と話しはじめた記念。${day}`:`${day}のふたりの日記。${entry.summary}${partnerText?` ${name}から: ${partnerText}`:''}${userText?` あなたから: ${userText}`:''}`);
      preview.replaceChildren(next);canvas=next;controls.forEach(b=>b.disabled=false);status.textContent='';
    }catch{if(!closed&&own===revision)status.textContent='画像を作れませんでした。閉じて、もう一度お試しください。';}
  }
  await render();return dialog;
}

export async function startShareNotice(parent,{active=()=>true}={}){
  const version=(invitationVersions.get(parent)||0)+1;invitationVersions.set(parent,version);
  parent.querySelector('.share-invite')?.remove();
  try{
    const cid=partnerId,state=await api('/api/share/state');
    if(invitationVersions.get(parent)!==version||!active()||cid!==partnerId||!state.available||!state.invite||parent.querySelector('.share-invite'))return;
    const card=node('section',undefined,'card set share-invite'),buttons=node('div',undefined,'row');
    card.append(node('p',`${state.name}と話しはじめて、やりとりが 10 回を超えました。シェアしますか？`),buttons);
    async function choose(actionName){await api('/api/share/start',{character_id:cid,action:actionName});invitationVersions.set(parent,(invitationVersions.get(parent)||0)+1);card.remove();}
    action(buttons,'シェアする',async()=>{await choose('shared');await shareDialog({where:'start'});});
    action(buttons,'今はしない',()=>choose('dismissed'));
    parent.prepend(card);
  }catch{/* Optional invitations never interrupt conversation. */}
}
