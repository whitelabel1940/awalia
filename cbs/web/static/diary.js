import {api,node,action,field,icon} from './ui.js';
import {knowingScreen} from './knowing.js';
import {daysHeader} from './note-header.js';
import {shareDialog} from './share.js';

export async function diaryScreen(parent,note,refresh){
  daysHeader(parent,note.days,note.nearby_days);
  const section=node('section',undefined,'set');section.append(node('h3','ふたりの日記'));parent.append(section);
  if(!note.diaries?.length)section.append(node('p','話した日のことを、翌日から少しずつ書き留めます。','muted'));
  for(const entry of note.diaries||[]){
    const article=node('article',undefined,'card set diary-entry'),date=node('time',entry.day);date.dateTime=entry.day;
    const heading=node('div',undefined,'row diary-date');heading.append(date);
    if(note.share_enabled!==false){const share=action(heading,'シェア',()=>shareDialog({where:'diary',entry}));share.classList.add('diary-share');share.prepend(icon('share'));}
    article.append(heading,node('p',entry.summary));
    const comment=node('div',undefined,'set');comment.append(node('h4',`${note.name}からの一言`),node('p',entry.partner_comment));article.append(comment);
    const userComment=node('div',undefined,'set diary-user-comment');article.append(userComment);
    const status=node('p','','status');status.setAttribute('role','status');
    function display(focus=false){
      userComment.replaceChildren();
      if(!entry.user_comment){edit(focus);return;}
      userComment.append(node('h4','あなたからの一言'),node('p',entry.user_comment,'diary-comment-text'));
      const change=action(userComment,'直す',()=>{status.textContent='';edit(true);});change.classList.add('diary-edit');
      if(focus)change.focus();
    }
    function edit(focus=false){
      userComment.replaceChildren();let value=entry.user_comment||'';
      const input=field(userComment,'あなたからのコメント（任意）',value,v=>value=v,'textarea');input.maxLength=2000;
      const controls=node('div',undefined,'row');userComment.append(controls);
      action(controls,'コメントを保存',async()=>{
        const saved=value;input.disabled=true;cancel.disabled=true;
        try{
          await api(`/api/note/diaries/${entry.day}/comment`,{comment:saved},'PUT');entry.user_comment=saved.trim();
          status.textContent=`保存しました。次の日記や会話で、${note.name}に届きます。`;display(true);
        }finally{input.disabled=false;cancel.disabled=false;}
      });
      const cancel=action(controls,'やめる',()=>{status.textContent='';display(true);});cancel.hidden=!entry.user_comment;
      if(focus)input.focus();
    }
    display();article.append(status);section.append(article);
  }
  await knowingScreen(parent,note.knowing||{understandings:[],facts:[]},note.name,refresh,{shared:true});
}
