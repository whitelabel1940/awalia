import {node} from './ui.js';

export function partnerNote(parent,note){
  const groups=note.self_forming||[],traits=note.self_traits||[];
  if(!groups.length&&!traits.length)return;
  const card=node('section',undefined,'card partner-note');
  card.append(node('h3',`あなたが知った${note.name}のこと`));
  for(const item of groups){
    const group=node('section',undefined,'note-entry'),heading=node('h4',item.label);group.append(heading);
    if(item.new)heading.append(node('span','NEW','badge note-new'));
    if(item.summary)group.append(node('p',item.summary,'scene-summary'));
    const rows=node('div',undefined,'scene-list');
    for(const scene of item.scenes){
      const row=node('article',undefined,'note-scene');row.id=`note-scene-${scene.id}`;row.tabIndex=-1;
      row.append(node('p',scene.text),node('p',`${scene.dates.join('、')} の会話で`,'small muted'));
      const detail=node('details');detail.append(node('summary','なぜそう思った？'));
      for(const e of scene.evidence)detail.append(node('p',`${e.date} — ${e.summary}${e.mode==='hypothetical'?'（もしもの話）':''}`));
      row.append(detail);rows.append(row);
    }
    if(item.summary){const detail=node('details',undefined,'scene-details');detail.append(node('summary',`場面（${item.scenes.length}）`),rows);group.append(detail);}
    else group.append(rows);
    card.append(group);
  }
  if(traits.length){
    const section=node('section',undefined,'note-traits');section.append(node('h4',`見えてきた${note.name}らしさ`));
    for(const trait of traits){
      const entry=node('article',undefined,'note-entry'),text=node('p',trait.text);if(trait.new)text.append(node('span','NEW','badge note-new'));entry.append(text);
      const links=node('ul',undefined,'small muted trait-sources');
      for(const scene of trait.scenes){
        const li=node('li'),a=node('a',`${scene.label}：${scene.text}`);a.href=`#note/scene-${scene.id}`;
        a.onclick=e=>{e.preventDefault();const target=document.getElementById(`note-scene-${scene.id}`);const detail=target?.closest('.scene-details');if(detail)detail.open=true;target?.focus({preventScroll:true});target?.scrollIntoView?.({block:'center'});};
        li.append(a);links.append(li);
      }
      entry.append(links);section.append(entry);
    }
    card.append(section);
  }
  parent.append(card);
}
