import {node,partnerId} from './ui.js';

export function partnerHeader(parent,note,look={}){
  const header=node('header',undefined,'note-header ribbon-background'),visual=node('div',undefined,'note-portrait');
  const caption=node('div',undefined,'note-header-caption');caption.append(node('h2',note.name));
  const hint=node('p',undefined,'note-cover-hint small muted'),link=node('a','扉絵は 設定 →「顔と表情を整える」で作れます');
  link.href='/wizard?look=1&use=cover';hint.append(link);
  caption.append(hint);
  header.append(visual,caption);parent.append(header);
  const candidates=[];
  if(look.cover)candidates.push('cover');
  if(look.expressions?.includes(note.current_expression))candidates.push(note.current_expression);
  if(look.expressions?.length&&!candidates.includes('neutral'))candidates.push('neutral'); // Image API supplies the first saved face if neutral is absent.
  function show(){
    const slot=candidates.shift();
    header.classList.toggle('has-cover',slot==='cover');
    hint.hidden=slot==='cover';
    visual.classList.toggle('note-symbol',!slot);
    if(!slot){visual.replaceChildren();visual.setAttribute('role','img');visual.setAttribute('aria-label','Awaliaのシンボル');return;}
    const img=node('img');img.alt=slot==='cover'?`${note.name}の暮らし`:`${note.name}の表情`;
    img.src=`/looks/${encodeURIComponent(partnerId)}/${slot}?v=${Date.now()}`;
    img.onerror=show;
    visual.replaceChildren(img);
  }
  show();return header;
}

export function daysHeader(parent,days={},nearby=[]){
  const header=node('header',undefined,'note-header note-days ribbon-background');
  header.append(node('h2','ふたりの時間'));
  if(days.first_day){
    header.append(node('p',`はじめて話した日から${days.elapsed_days}日`,'days-elapsed'),node('p',`話した日 ${days.talked_days}日`));
    const date=node('time',`はじまりは ${days.first_day}`,'small muted');date.dateTime=days.first_day;header.append(date);
  }else header.append(node('p','はじめての言葉から、ふたりの時間がはじまります。'));
  if(nearby.length){
    const list=node('ul',undefined,'nearby-days');list.setAttribute('aria-label','近い大切な日');
    for(const day of nearby){const item=node('li'),time=node('time',day.date);time.dateTime=day.date;item.append(node('span',day.name+' · '),time,node('span',day.in_days===0?' · 今日':` · あと${day.in_days}日`));list.append(item);}
    const link=node('a','大切な日を直す');link.href='#settings/partner/days';header.append(list,link);
  }
  parent.append(header);return header;
}
