import {saveOnEdit} from './settings-save.js';
import {accordion} from './onboarding.js';
import {icon,api, node, field, select, action, card} from './ui.js';
import {restoreEntry} from './restore.js';
import {isResident} from './resident.js';

let watching;
const oldError='書き出しに失敗しました。空き容量・フォルダの権限・人物ファイルを確認してください。',currentError='バックアップに失敗しました。空き容量・フォルダの権限・人物ファイルを確認してください。';
export const backupError=text=>text===oldError?currentError:text;
export function backupSettings(parent, cfg, {managed=false,onboarding=false,autosave=false,name="パートナー"}={}) {
  const section=card(parent,autosave?'バックアップ':'記録とパートナーのバックアップ');section.id='backup-settings';
  if(cfg.backup_status.trial){section.append(node('p','お試しではバックアップを行いません。','small muted'));return section;}
  const last=node('p',undefined,'small muted');section.append(last);
  const display=status=>{last.textContent=`この PC の中には毎日自動で保存しています　最後: ${status.auto_success?new Date(status.auto_success).toLocaleString('ja-JP'):'まだありません'}`;};
  display(cfg.backup_status);
  for(const kind of (autosave?['migration']:['auto','scheduled','migration']))if(cfg.backup_status[`${kind}_error`])section.append(node('p',backupError(cfg.backup_status[`${kind}_error`]),'warn'));
  section.append(node('p',`会話や日記、${name}の設定をまとめて保存します。PC の故障や買い替えの時は、ここから元に戻せます。`,'small muted'));
  const password=field(section,autosave?'パスワード（任意）':'バックアップのパスワード（任意）','',()=>{});password.id='backup-password';password.type='password';password.autocomplete='new-password';password.maxLength=1024;
  const warning=node('p','パスワードを付けると、このファイルを開く時にパスワードが必要になります。パスワードは保存しないので、忘れると元に戻せません。自動のバックアップには付けられません。','small muted');warning.id='backup-password-warning';password.setAttribute('aria-describedby',warning.id);section.append(warning);
  const progress=node('p','','status');progress.setAttribute('role','status');section.append(progress);
  const start=action(section,'今すぐバックアップする',async()=>{
    const value=password.value;password.value='';
    try{const job=await api('/api/backup/exports',{password:value});await watch(job.id);}
    catch(error){progress.textContent=error.message;}
  },true);start.id='backup-export';
  async function watch(id){
    start.disabled=true;progress.textContent='バックアップしています。会話はそのまま使えます。';
    if(watching?.id===id){progress.textContent=await watching.promise;start.disabled=false;return;}
    let finished;watching={id,promise:new Promise(resolve=>finished=resolve)};
    const notice=node('div',progress.textContent,'backup-notice');notice.setAttribute('role','status');document.body.append(notice);
    try{
      let job;
      do{
        job=await api(`/api/backup/exports/${encodeURIComponent(id)}`);
        if(['queued','running'].includes(job.status))await new Promise(resolve=>setTimeout(resolve,800));
      }while(['queued','running'].includes(job.status));
      if(job.status==='failed')throw new Error(job.error||'バックアップに失敗しました。');
      progress.textContent='バックアップができました。';notice.textContent='バックアップができました。 ';
      const download=node('a','ダウンロード');download.href=`/api/backup/exports/${encodeURIComponent(id)}/download`;download.download=job.filename;notice.append(download);download.click();
      action(notice,'閉じる',()=>notice.remove());
    }catch(error){progress.textContent=error.message;notice.textContent=error.message;action(notice,'閉じる',()=>notice.remove());}
    finally{watching=null;start.disabled=false;finished(progress.textContent);}
  }
  const automatic=node('div',undefined,'set');section.append(automatic);if(!autosave)section.append(node('p','OneDrive や Google ドライブの同期フォルダを選ぶと、クラウドにも残ります。','small muted'));
  const folderRow=node('div',undefined,'set');automatic.append(folderRow);const folder=field(folderRow,autosave?'保存先のフォルダ':'自動バックアップ先のフォルダ',cfg.backup.folder,()=>{});folder.id='backup-folder';folder.maxLength=2048;folder.placeholder='C:\\Users\\…\\OneDrive\\Awalia';
  if(isResident())action(folderRow,'フォルダを選ぶ',async()=>{const selected=await window.__TAURI__.core.invoke('resident_pick_folder');if(selected){folder.value=selected;folder.dispatchEvent(new window.Event('input',{bubbles:true}));folder.dispatchEvent(new window.Event('blur'));}});
  const frequency=select(automatic,'自動バックアップの頻度',{off:'しない',daily:'毎日',weekly:'毎週'},cfg.backup.frequency,()=>{});frequency.id='backup-frequency';
  if(!autosave)automatic.append(node('p','この環境の自動バックアップはパスワードなしのみです。秘密の保管機能がないため、パスワードは保存しません。','small muted'));
  const saved=node('p','','status');saved.setAttribute('role','status');
  section.save=async()=>{
    try{const result=await api('/api/backup/settings',{...cfg.backup,folder:folder.value,frequency:frequency.value},'PUT');cfg.backup=result.config;folder.value=result.config.folder;display(result);saved.textContent='バックアップの設定を保存しました。';}
    catch(error){saved.textContent=error.message;throw error;}
  };
  password.dataset.transient='true';
  if(!managed&&!autosave)action(automatic,'バックアップの設定を保存',()=>section.save(),true);automatic.append(saved);
  if(autosave){
    const description=[...section.querySelectorAll('p')].find(p=>p.textContent.startsWith('会話や日記'));section.querySelector('h3').after(description);
    section.insertBefore(node('h4','別の場所にも保存する'),password.parentElement);section.insertBefore(automatic,password.parentElement);
    automatic.prepend(frequency.parentElement);frequency.parentElement.hidden=true;
    const radios=node('div',undefined,'row');automatic.prepend(radios);saveOnEdit(frequency,value=>api('/api/backup/settings',{frequency:value},'PUT'));radios.after(frequency.parentElement.querySelector('.saved-badge'));
    radios.classList.add('backup-frequency');const extra=node('div',undefined,'set');automatic.append(extra);extra.append(folderRow);const visibility=()=>{extra.hidden=folderRow.hidden=folder.parentElement.hidden=frequency.value==='off';};
    for(const [value,title]of [['off','しない'],['daily','毎日'],['weekly','毎週']]){const label=node('label',undefined,'choice'),radio=node('input');radio.type='radio';radio.name='backup-frequency';radio.value=value;radio.checked=frequency.value===value;label.append(radio,node('span',title));radios.append(label);radio.onchange=()=>{frequency.value=value;visibility();frequency.dispatchEvent(new window.Event('change'));};}
    saveOnEdit(folder,value=>api('/api/backup/settings',{folder:value},'PUT'));visibility();
    extra.append(node('p',`OneDrive や Google ドライブの同期フォルダを選ぶと、PC が壊れても残ります。新しいものから${cfg.backup.export_keep}つ残します。`,'small muted'),node('p',`最後: ${cfg.backup_status.scheduled_success?new Date(cfg.backup_status.scheduled_success).toLocaleString('ja-JP'):'まだありません'}`,'small muted'));

    section.insertBefore(node('h4','今すぐバックアップ'),password.parentElement);section.insertBefore(node('p','今の状態をファイルにしてダウンロードします。','small muted'),password.parentElement);
  }
  if(autosave){
    const automaticTitle=[...section.querySelectorAll('h4')].find(e=>e.textContent==='別の場所にも保存する'),manualTitle=[...section.querySelectorAll('h4')].find(e=>e.textContent==='今すぐバックアップ');automaticTitle.classList.add('backup-section');manualTitle.classList.add('backup-section');
    const passwordDetails=node('details',undefined,'set backup-password-details');passwordDetails.append(node('summary','パスワードを付ける'));section.insertBefore(passwordDetails,password.parentElement);passwordDetails.append(password.parentElement,warning);
    const scheduled=cfg.backup_status.scheduled_error,auto=cfg.backup_status.auto_error;
    for(const message of section.querySelectorAll('.warn')){if(auto&&message.textContent===backupError(auto))action(message,'今すぐバックアップする',()=>start.click());else if(scheduled&&message.textContent===backupError(scheduled))action(message,'保存先のフォルダを確かめる',()=>folder.focus());}
    const states=node('div',undefined,'set backup-states');section.querySelector('h3').nextElementSibling.after(states);
    for(const [title,frequencyValue,time,error]of [['この PC','daily',cfg.backup_status.auto_success,auto],['別の場所',cfg.backup.frequency,cfg.backup_status.scheduled_success,scheduled]]){const row=node('p',`${title}: ${frequencyValue==='off'?'未設定':`${{daily:'毎日',weekly:'毎週'}[frequencyValue]||'未設定'} · 最後: ${time?new Date(time).toLocaleString('ja-JP'):'まだありません'}`}`);if(error){row.className='warn';row.prepend(icon('warning'));row.append(node('span',' · '+backupError(error)));action(row,title==='この PC'?'今すぐバックアップする':'保存先のフォルダを確かめる',()=>{if(title==='この PC')start.click();else {folderRow.parentElement.hidden=folderRow.hidden=folder.parentElement.hidden=false;folder.focus();}});}states.append(row);}last.remove();
  }
  const active=cfg.backup_status.jobs?.find(j=>j.kind==='manual'&&['queued','running'].includes(j.status));
  if(active)void watch(active.id);
  if(onboarding){
    for(const child of section.children)child.hidden=true;
    accordion(section,[
      {title:'フォルダ',summary:()=>folder.value,render:p=>{p.append(folder.parentElement);folder.parentElement.hidden=false;if(isResident())action(p,'参照',async()=>{const selected=await window.__TAURI__.core.invoke('resident_pick_folder');if(selected)folder.value=selected;});}},
      {title:'頻度',summary:()=>({off:'しない',daily:'毎日',weekly:'毎週'})[frequency.value],render:p=>{p.append(frequency.parentElement);frequency.parentElement.hidden=false;}},
      {title:'パスワード',disabled:true,reason:'自動バックアップはパスワードなしで保存します。'}
    ]);
    section.append(node('p','自動バックアップはパスワードなしのみです。パスワードを付ける場合は、あとで設定から手動でバックアップできます。','small muted'));
  }else {const restore=restoreEntry(section,{label:autosave?'バックアップから元に戻す':undefined});if(autosave){restore.classList.add('settings-restore-entry','backup-section');restore.prepend(node('h4','元に戻す'));const entry=restore.querySelector('button');entry.classList.add('settings-flow');entry.append(icon('right'));}restore.insertBefore(node('p','今の記録は控えを取ってから入れ替えます。','small muted'),restore.querySelector('button'));}
  return section;
}
