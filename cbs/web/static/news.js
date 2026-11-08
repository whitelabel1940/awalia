import {api,node,icon} from './ui.js';

let status={unread_count:0,has_update:false};
let revision=0;
let nativeStatus={},listening=false,renderUpdate;
const resident=()=>typeof window.__TAURI__?.core?.invoke==='function';
const newer=(a,b)=>{
  const left=String(a||'').split('.').map(Number),right=String(b||'').split('.').map(Number);
  if(left.length!==3||right.length!==3||[...left,...right].some(n=>!Number.isFinite(n)))return false;
  for(let i=0;i<3;i++){if(left[i]!==right[i])return left[i]>right[i];}return false;
};
const nativeUpdate=()=>resident()&&newer(nativeStatus.availableVersion,nativeStatus.currentVersion);
async function refreshNative(){
  if(!resident()){nativeStatus={};return;}
  try{
    if(!listening&&window.__TAURI__.event?.listen){
      listening=true;
      await window.__TAURI__.event.listen('resident-update',event=>{
        nativeStatus=event.payload;paintNewsBadges();renderUpdate?.();
      });
    }
    nativeStatus=await window.__TAURI__.core.invoke('resident_update_status');
  }catch{/* An older shell can still use the download link. */}
}
export function paintNewsBadges(data=status){
  status=data;
  for(const link of document.querySelectorAll('[data-news-link]')){
    link.querySelector('.badge')?.remove();
    if(data.unread_count||data.has_update||nativeUpdate()){
      const badge=node('span',data.unread_count?String(data.unread_count):'新','badge news-badge');
      badge.setAttribute('aria-label',`未読 ${data.unread_count}件${data.has_update||nativeUpdate()?'・新しい版があります':''}`);link.append(badge);
    }
  }
  renderOnboardingUpdate();
}
export function newsEntries(active){
  const menu=document.getElementById('menu');
  if(!menu)return;
  let desktop=menu.querySelector('[data-news-link]');
  if(!desktop){desktop=newsLink(false);const partner=menu.querySelector('.partner');menu.insertBefore(desktop,partner);}
  if(active==='news')desktop.setAttribute('aria-current','page');
  const header=document.querySelector('.chat-band')||document.querySelector('.head');
  document.querySelectorAll('.news-mobile').forEach(e=>{if(e.parentNode!==header)e.remove();});
  if(header&&!header.querySelector('.news-mobile'))header.append(newsLink(true));
  paintNewsBadges();
}
function newsLink(mobile){
  const a=node('a',undefined,mobile?'btn quiet icon-button news-mobile':'news-menu');
  a.href='#news';a.dataset.newsLink='';a.setAttribute('aria-label','お知らせ');
  a.append(icon('mail'));if(!mobile)a.append(node('span','お知らせ'));return a;
}
export async function refreshNews(){
  const own=++revision;
  await refreshNative();
  try{const data=await api('/api/news');if(own===revision)paintNewsBadges(data);}catch{if(own===revision)paintNewsBadges();/* Native updates remain available offline. */}
}
function link(parent,label,url){
  try{if(new URL(url).protocol!=='https:')return;}catch{return;}
  const a=node('a',label);a.href=url;a.target='_blank';a.rel='noopener noreferrer';parent.append(a);
}
function latestUpdate(data){
  return nativeUpdate()&&(!data.has_update||newer(nativeStatus.availableVersion,data.latest?.version))
    ?{version:nativeStatus.availableVersion,notes:nativeStatus.notes}:data.has_update?data.latest:null;
}
// Both update surfaces use the same confirmation, install command and progress.
function updateActions(card,data,latest){
  if(data.latest?.url)link(card,'ダウンロード',data.latest.url);
  if(!nativeUpdate())return;
  if(latest.version!==nativeStatus.availableVersion)card.append(node('p',`このボタンで ${nativeStatus.availableVersion} に更新します。`,'small'));
  const button=node('button','更新して再起動','btn');button.type='button';
  button.disabled=['downloading','installing'].includes(nativeStatus.phase);
  const progress=node('p',nativeStatus.error||(
    nativeStatus.phase==='downloading'?`ダウンロード中 ${nativeStatus.total?Math.floor(nativeStatus.downloaded/nativeStatus.total*100)+'%':nativeStatus.downloaded+' bytes'}`:
    nativeStatus.phase==='installing'?'Awalia を閉じて更新しています。':''),'small');
  progress.setAttribute('role','status');card.append(button,progress);
  button.onclick=async()=>{
    if(!window.confirm('Awalia を閉じて更新します。会話は保存されています。'))return;
    button.disabled=true;progress.textContent='ダウンロードと署名を確認しています。';
    try{await window.__TAURI__.core.invoke('resident_update_install');}
    catch(error){progress.textContent=String(error.message||error);button.disabled=false;}
  };
}
function renderOnboardingUpdate(){
  let notice=document.getElementById('onboarding-update');
  const latest=latestUpdate(status);
  if(!document.body.classList.contains('onboarding')||!latest){notice?.remove();return;}
  const parent=document.getElementById('screen')?.parentElement;
  if(!parent)return;
  if(!notice){notice=node('aside',undefined,'card onboarding-update');notice.id='onboarding-update';parent.prepend(notice);}
  notice.replaceChildren(node('h3','バージョンアップのお知らせ'),node('p',`${latest.version} が出ています。確定した内容は保存されています。入力中の内容は、更新前に確定してください。`,'small'));
  updateActions(notice,status,latest);
}
export async function newsScreen(parent){
  const own=++revision,data=await api('/api/news');await refreshNative();if(own===revision)paintNewsBadges(data);
  let card;
  renderUpdate=()=>{
    if(!(data.has_update&&data.latest)&&!nativeUpdate()){card?.remove();card=null;return;}
    if(!card){card=node('section',undefined,'card');parent.prepend(card);}
    card.replaceChildren();
    const latest=latestUpdate(data),current=resident()?nativeStatus.currentVersion:null;
    card.append(node('h3','バージョンアップのお知らせ'),node('p',`${latest.version} が出ています${current?`（今は ${current}）`:''}`,'small muted'),node('p',latest.notes||'','text'));
    updateActions(card,data,latest);
  };
  renderUpdate();
  for(const notice of [...data.notices].sort((a,b)=>b.date.localeCompare(a.date))){
    const c=node('section',undefined,'card'),title=node('h3',notice.title);
    if(!notice.read)title.append(node('span','未読','badge news-unread'));
    c.append(title,node('time',notice.date,'small muted'),node('p',notice.body,'text'));
    for(const item of notice.links||[]){const p=node('p');link(p,item.label,item.url);c.append(p);}parent.append(c);
  }
  if(data.feedback_url){const c=node('section',undefined,'card');c.append(node('h3','フィードバックをお願いします'));link(c,'フォームを開く',data.feedback_url);parent.append(c);}
  // The caller commits the detached page before marking precisely these IDs read.
  return async()=>{if(data.notices.length){const own=++revision;const updated=await api('/api/news/read',{ids:data.notices.map(n=>n.id)});if(own===revision)paintNewsBadges(updated);}};
}
