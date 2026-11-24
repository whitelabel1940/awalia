import {api,node,field,action,card,confirmDialog,icon} from './ui.js';
import {isResident} from './resident.js';
import {saveOnEdit} from './settings-save.js';

export async function researchSettings(parent,cfg){
  if(!cfg.developer)return;
  const box=card(parent,'判断の記録'),summary=node('p'),warning=node('p','','warn'),notice=node('p','','status');
  notice.setAttribute('role','status');box.append(summary,warning);
  function paint(s){summary.textContent=`${s.count}件 · ${(s.bytes/1024/1024).toFixed(2)} MB · Shadow 未実施 ${s.pending}件・失敗 ${s.failed}件 · 記録できなかった回数 ${s.record_failures} · Shadow の失敗 ${s.shadow_failures}回 · 最後の記録: ${s.last_recorded_at?new Date(s.last_recorded_at).toLocaleString('ja-JP'):'まだありません'}`;const text=s.reason||(s.large?'500MB を超えています。必要に応じて書き出しや削除を行ってください。':'');warning.replaceChildren(icon('warning'),node('span',text));warning.hidden=!text;}
  paint(cfg.research_status||await api('/api/research'));
  box.append(node('p','開発版での比較用に、会話の判断入力と結果をこの PC に保存します。元の写真や会話を取り消しても、研究用の記録は残ります。','small muted'));
  for(const [key,title]of [['enabled','判断を記録する'],['shadow','もう一方の判断も後で比べる（Shadow）']]){
    const label=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=!!cfg.research?.[key];label.append(input,node('span',title));box.append(label);
    saveOnEdit(input,async value=>{const saved=await api('/api/settings',{research:{[key]:value}},'PUT');cfg.research=saved.research;paint(saved.research_status);});
  }
  const folder=field(box,'書き出し先のフォルダ','',()=>{});folder.maxLength=2048;
  if(isResident())action(box,'フォルダを選ぶ',async()=>{const selected=await window.__TAURI__.core.invoke('resident_pick_folder');if(selected)folder.value=selected;});
  action(box,'記録を書き出す',async()=>{
    try{
      let result=await api('/api/research/export',{folder:folder.value});
      if(result.cloud_confirmation_required){if(!await confirmDialog('クラウド同期フォルダにも会話の判断入力と結果が残ります。書き出しますか？','書き出す'))return;result=await api('/api/research/export',{folder:folder.value,cloud_confirmed:true});}
      notice.textContent=`書き出しました: ${result.filename}`;
    }catch(error){notice.textContent=error.message;}
  });
  const before=field(box,'この日より前を消す','',()=>{});before.type='date';
  async function remove(all){
    if(!all&&!before.value){notice.textContent='日付を選んでください。';return;}
    if(!await confirmDialog(all?'研究用の記録を全部消しますか？':'選んだ日より前の研究用の記録を消しますか？','削除する'))return;
    try{paint(await api('/api/research/delete',{confirmed:true,...(all?{}:{before:before.value})}));notice.textContent='削除しました。';}
    catch(error){notice.textContent=error.message;}
  }
  action(box,'この日より前を削除する',()=>remove(false));action(box,'全部消す',()=>remove(true));box.append(notice);
}
