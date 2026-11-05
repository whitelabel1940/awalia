import {api,node,action,card} from './ui.js';
import {saveOnEdit} from './settings-save.js';

function description(parent,state){
  parent.append(node('p',state.text));
  if(state.privacy_url){const link=node('a','プライバシーポリシー');link.href=state.privacy_url;link.target='_blank';link.rel='noopener noreferrer';parent.append(link);}
}

export async function telemetryConsent(parent){
  // Shown whenever this build has a destination, so お試し and development show the real screen;
  // the server still refuses to turn sending on where it is not allowed.
  const state=await api('/api/telemetry').catch(()=>null);if(!state?.configured)return async()=>{};
  const row=node('label',undefined,'choice'),input=node('input'),copy=node('span');
  input.type='checkbox';input.checked=false;
  description(copy,state);row.append(input,copy);parent.append(row);
  return async()=>{if(input.checked)await api('/api/telemetry',{enabled:true,version:state.version});};
}

export async function telemetrySettings(parent){
  const state=await api('/api/telemetry').catch(()=>null);if(!state)return;
  const box=card(parent,'利用状況の送信');description(box,state);
  const label=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=state.enabled;
  label.append(input,node('span','利用状況を送信する'));box.append(label);input.disabled=!state.available;
  if(!state.configured)box.append(node('p','この版では送信先が設定されていません','muted'));
  box.append(node('p','オフにすると、ID とまだ送っていない分を削除します。','small muted'));
  saveOnEdit(input,enabled=>api('/api/telemetry',{enabled,version:state.version}));
  const content=node('div');content.hidden=true;box.append(content);
  action(box,'送った内容を見る',async()=>{
    const sent=await api('/api/telemetry/sent');content.replaceChildren();content.hidden=false;
    if(!sent.length)content.append(node('p','送った内容はまだありません。'));
    for(const event of sent){content.append(node('p',`${event.event} · ${event.timestamp}`),node('pre',JSON.stringify(event.properties,null,2)));}
  });
}
