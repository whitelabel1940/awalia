import {api,node,action} from './ui.js';
import {notice} from './onboarding.js';
export const entryTargets={ai:'ai',voice:'look',input:'screen'};
export const entryMessages={ai:'AI のつなぎ方を変えました。',voice:'声を変えました。',input:'文字起こしの方法を変えました。'};
export function returnToSettings(kind,saved=false){if(saved)window.sessionStorage.setItem('cbs.settingsEntry',kind);location.href='/#settings/'+entryTargets[kind];}
export function entryNotice(parent){const kind=window.sessionStorage.getItem('cbs.settingsEntry');window.sessionStorage.removeItem('cbs.settingsEntry');if(entryMessages[kind]){const status=notice(parent,entryMessages[kind]);parent.querySelector('h2,h3')?.after(status);}}
export function summaryRow(parent,title,value,edit,label='直す'){const row=node('div',undefined,'summary-row'),content=node('div');row.append(node('span',title));content.append(node('span',value));if(edit)action(content,label,edit).classList.add('quiet');row.append(content);parent.append(row);return row;}
export function embeddingIdentity(model){return model==='openrouter/google/gemini-embedding-2'?'gemini/gemini-embedding-2':model;}
export async function reindexAI(progress){let result=await api('/api/maintenance/reindex',{});while(result.status==='running'){progress(`記憶の索引を作り直しています… ${result.completed} / ${result.total}`);await new Promise(r=>setTimeout(r,800));result=await api('/api/maintenance/reindex');}if(result.status==='failed')throw new Error(result.error);progress('記憶の索引を作り直しました。');}
