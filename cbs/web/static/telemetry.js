import {api,node,action,card} from './ui.js';
import {saveOnEdit} from './settings-save.js';
import {renderExplanation} from './onboarding.js';

function policyLink(parent,state){
  if(state.privacy_url){const link=node('a','プライバシーポリシー');link.href=state.privacy_url;link.target='_blank';link.rel='noopener noreferrer';parent.append(link);}
}

// The first screen's notice (onboarding/telemetry.md, Markdown like the other explanations) and the
// required 「上記内容に同意する」. Agreeing also turns usage sending on where this build may send.
export async function telemetryConsent(parent,{onChange=()=>{}}={}){
  const state=await api('/api/telemetry').catch(()=>null);
  if(!state?.text)return {agreed:()=>true,save:async()=>{}};
  const notice=node('div',undefined,'consent-notice');renderExplanation(notice,state.text);policyLink(notice,state);
  const row=node('label',undefined,'choice consent-agree'),input=node('input');
  input.type='checkbox';input.checked=false;input.onchange=()=>onChange(input.checked);
  row.append(input,node('span','上記内容に同意する'));parent.append(notice,row);
  return {agreed:()=>input.checked,save:async()=>{if(input.checked&&state.configured)await api('/api/telemetry',{enabled:true,version:state.version});}};
}

export async function telemetrySettings(parent){
  const state=await api('/api/telemetry').catch(()=>null);if(!state)return;
  const box=card(parent,'利用状況の送信');
  box.append(node('p','使った日・進んだ段階・エラーの種類・会話の数・使っている AI の種類・シェアの利用を、改善のために送ります。会話の内容や設定の中身は送りません。'));policyLink(box,state);
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
