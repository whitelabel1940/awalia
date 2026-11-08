import {api,node,action,field,card,face} from './ui.js';
import {isResident,desktopHeaders} from './resident.js';

export function restoreEntry(parent,{welcome=false,label}={}){
  const section=card(parent);section.classList.add('restore-settings');
  action(section,label||(welcome?'バックアップから始める':'バックアップから元に戻す'),async()=>{
    const state=await api('/api/restore');
    if(state.trial){section.replaceChildren(node('p','お試しでは記録を戻せません。通常の起動でお試しください。'));return;}
    await restoreForm(section,state);
  });
  return section;
}

async function restartGuide(section){
  section.replaceChildren(node('h3','戻す準備ができました'));
  const status=node('p',isResident()?'アプリを再起動しています…':'アプリを終了して、もう一度起動してください。次の起動で記録を入れ替えます。','status');status.setAttribute('role','status');section.append(status);
  if(isResident()){
    const restart=async()=>{
      try{await window.__TAURI__.core.invoke('resident_restore_restart');status.textContent='アプリを再起動しています…';}
      catch{status.textContent='再起動できませんでした。もう一度押すか、アプリを終了して起動してください。';}
    };
    action(section,'再起動する',restart,true);await restart();
  }
}

export async function restoreForm(section,state){
  if(state.staged){await restartGuide(section);return;}
  section.replaceChildren(node('h3','バックアップから元に戻す'));
  section.append(node('p',`バックアップの Zip またはパスワード付きファイルを選びます（上限 ${Math.floor(state.max_bytes/1024/1024)} MB）。`,'small muted'));
  const file=field(section,'バックアップファイル','',()=>{});file.type='file';file.accept='.zip,.awalia';file.id='restore-file';
  const password=field(section,'バックアップのパスワード','',()=>{});password.type='password';password.autocomplete='off';password.maxLength=1024;password.id='restore-password';password.parentElement.hidden=true;
  const status=node('p','','status');status.setAttribute('role','status');section.append(status);
  let selected,preview;
  const cancelPreview=async()=>{if(preview){try{await api(`/api/restore/previews/${preview.id}`,{},'DELETE');}catch{/* An older confirmation may already have expired. */}finally{preview=null;}}};
  file.onchange=async()=>{await cancelPreview();selected=file.files?.[0];password.value='';password.parentElement.hidden=true;confirm.replaceChildren();status.textContent='';};
  action(section,'中身を確かめる',async()=>{
    if(!selected){status.textContent='ファイルを選んでください。';return;}
    if(selected.size>state.max_bytes){status.textContent='ファイルが大きすぎます。';return;}
    const value=password.value;password.value='';status.textContent='中身を確かめています…';
    try{
      await cancelPreview();confirm.replaceChildren();
      const headers=await desktopHeaders();
      const response=await fetch('/api/restore/preview',{method:'POST',headers:{...headers,'Content-Type':'application/octet-stream','X-Awalia-Restore-Password':encodeURIComponent(JSON.stringify(value))},body:selected});
      const result=await response.json();
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'ファイルを確認してください。');
      if(result.password_required){password.parentElement.hidden=false;password.focus();status.textContent='このファイルのパスワードを入力して、もう一度確かめてください。';return;}
      preview=result;status.textContent='中身を確認しました。';
      confirm.append(node('h4','この記録から戻します'),node('p',`バックアップ: ${new Date(result.created_at).toLocaleString('ja-JP')}`));
      const partners=node('ul',undefined,'restore-partners');
      for(const partner of result.partners){
        const row=node('li',undefined,'restore-partner'),portrait=face(partner.name),info=node('div'),heading=node('div',undefined,'row');
        if(partner.face){const img=node('img');img.src=partner.face;img.alt='';portrait.append(img);}
        heading.append(node('span',partner.name));if(partner.selected)heading.append(node('span','いつもの相手','badge'));
        info.append(heading,node('p',partner.turns?`最初の会話 ${new Date(partner.first_at).toLocaleDateString('ja-JP')} · 会話 ${partner.turns} 件`:'会話なし','small muted'));
        row.append(portrait,info);partners.append(row);
      }
      confirm.append(partners,node('p',`会話 約${result.turns}件 · ${(result.bytes/1024/1024).toFixed(1)} MB`),node('p','今の記録はバックアップしてから入れ替えます。','warn'));
      action(confirm,'この記録に入れ替える',async()=>{
        try{await api(`/api/restore/previews/${preview.id}/stage`,{});preview=null;await restartGuide(section);}
        catch(error){status.textContent=error.message;}
      },true);
      action(confirm,'入れ替えずに閉じる',async()=>{await cancelPreview();confirm.replaceChildren();status.textContent='入れ替えをやめました。今の記録はそのままです。';});
    }catch(error){status.textContent=error.message;}
  },true);
  const confirm=node('div',undefined,'restore-confirm');section.append(confirm);
}

export async function showRestoreNotice(){
  const state=await api('/api/restore/notice',{});
  if(!state.created_at)return;
  const notice=node('aside',undefined,'backup-notice');notice.setAttribute('role','status');
  notice.append(node('p',`${new Date(state.created_at).toLocaleDateString('ja-JP',{year:'numeric',month:'long',day:'numeric'})}にバックアップした記録から戻しました。`));
  // The old PC's backup folder is not carried over; say so before the user relies on it.
  if(state.backup_folder){
    notice.append(node('p','別の場所へのバックアップは引き継いでいません。今は、この PC の中にだけ保存しています。設定の「記録とデータ」で保存先を選び直してください。','warn'),
      node('p',`前の PC での保存先（参考）: ${state.backup_folder}`,'small muted'));
    action(notice,'記録とデータを開く',()=>{notice.remove();location.href='/#settings/data';});
  }
  document.body.append(notice);action(notice,'閉じる',()=>notice.remove());
}

export function welcomeRestore(body,buttons){buttons.classList.add('welcome-actions');const section=node('div');body.append(section);action(buttons,'バックアップから始める',async()=>{const state=await api('/api/restore');if(state.trial)section.replaceChildren(node('p','お試しでは記録を戻せません。通常の起動でお試しください。'));else await restoreForm(section,state);});}
