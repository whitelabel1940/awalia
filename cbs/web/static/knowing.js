import {api,node,action,field,select,confirmDialog,report} from './ui.js';

export async function privateSettings(parent,refresh,name='パートナー') {
  const data=await api('/api/note/private-settings');
  const section=node('section',undefined,'card set');section.append(node('h3','プライベートなことの気にかけ方'),node('p',`あなたが一度話した健康・宗教・政治などのことに、${name}のほうから後で触れてよいかを、種類ごとに決めます。触れてよい時も、一度だけやさしく様子を聞きます。話していないことを推測したり持ち出したりはしません。事実ごとの指定は「もっとプライベートなこと」で変えられます。`,'small muted'));parent.append(section);
  for(const [key,value] of Object.entries(data.categories)){
    const label=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=value.allow_mention;
    label.append(input,node('span',value.label));section.append(label);
    input.onchange=async()=>{input.disabled=true;try{await api('/api/note/private-settings',{categories:{[key]:input.checked}},'PUT');value.allow_mention=input.checked;await refresh?.();}catch(e){input.checked=value.allow_mention;report(e);}finally{input.disabled=false;}};
  }
}

export async function inferenceSetting(parent,initial) {
  const data=initial??(await api('/api/note')).knowing;if(!data)return;
  const label=node('label',undefined,'switch'),input=node('input');input.type='checkbox';input.checked=data.stopped;
  label.append(input,node('span','推測をやめて、話したことだけ覚えてもらう'));parent.append(label);
  const choice=node('div',undefined,'set');choice.hidden=true;parent.append(choice);
  choice.append(node('p','今までの理解をどうしますか？','small'));
  async function save(clear){await api('/api/note/inference',{stopped:true,clear},'PUT');choice.hidden=true;input.disabled=false;data.stopped=true;}
  action(choice,'今の理解を残す',()=>save(false));action(choice,'今の理解を消す',()=>save(true));
  action(choice,'戻る',()=>{choice.hidden=true;input.checked=false;input.disabled=false;});
  input.onchange=async()=>{if(input.checked){choice.hidden=false;input.disabled=true;}else{input.disabled=true;try{await api('/api/note/inference',{stopped:false},'PUT');data.stopped=false;}catch(e){input.checked=true;report(e);}finally{input.disabled=false;}}};
}

function facts(parent,items,refresh,name,view,key){
  const body=node('div'),more=node('div');parent.append(body,more);
  if(!items.length)parent.append(node('p','まだ書き留めたことはありません。','small muted'));
  function render(){
    body.replaceChildren();more.replaceChildren();
    for(const fact of items.slice(0,view[key])){
      const row=node('article',undefined,'note-entry');row.append(node('p',fact.content));const controls=node('div',undefined,'row');row.append(controls);body.append(row);
      if(fact.private){
        if(fact.category_label)row.append(node('p',fact.category_label,'small muted'));
        const initial=fact.mention_override==null?'default':String(fact.mention_override);
        const choice=select(row,`${name}からこの話に触れてよい？`,{default:`種類の設定に従う${fact.category_allow_mention==null?'':fact.category_allow_mention?'（触れてよい）':'（触れない）'}`,true:`${name}から触れてよい`,false:'触れないで'},initial,async value=>{
          choice.disabled=true;try{await api(`/api/note/facts/${fact.id}/mention`,{mention_override:value==='default'?null:value==='true'},'PUT');fact.mention_override=value==='default'?null:value==='true';}catch(e){choice.value=fact.mention_override==null?'default':String(fact.mention_override);report(e);}finally{choice.disabled=false;}
        });
      }
      action(controls,'直す',()=>{const edit=node('div',undefined,'set');let content=fact.content;field(edit,'話したこと',content,v=>content=v,'textarea');const privacy=node('label'),check=node('input');check.type='checkbox';check.checked=fact.private;privacy.append(check,node('span','もっとプライベートなこと'));edit.append(privacy);action(edit,'保存する',async()=>{await api(`/api/note/facts/${fact.id}`,{content,private:check.checked},'PUT');await refresh();});action(edit,'戻る',()=>{edit.remove();controls.hidden=false;});row.append(edit);controls.hidden=true;});
      action(controls,'消す',async()=>{if(await confirmDialog('この事実をノートから消しますか？','消す')){await api(`/api/note/facts/${fact.id}`,{},'DELETE');await refresh();}});
    }
    if(items.length>view[key])action(more,`もっと見る（残り ${items.length-view[key]} 件）`,()=>{view[key]+=10;render();});
  }
  render();
}

export async function knowingScreen(parent,data,name,refresh,{shared=false,factView={public:5,private:5,privateOpen:false}}={}) {
  if(data.stopped&&!shared)parent.append(node('p','今は、はっきり話したことだけを覚えています。','muted'));
  const labels=shared?{shared:'ふたりの間のこと'}:{interaction:'関わり方',interest:'好きなこと・気になること',situation:'今のこと'};
  for(const [kind,title]of Object.entries(labels)){
    const section=node('section',undefined,'card');section.append(node('h3',title));parent.append(section);
    const items=data.understandings.filter(i=>i.kind===kind);
    if(!items.length)section.append(node('p','話していくうちに、少しずつ。','small muted'));
    for(const item of items){
      const row=node('article',undefined,'note-entry'),head=node('div',undefined,'row');head.append(node('p',item.text),node('span',item.stage_label,'badge'));row.append(head);
      if(item.condition)row.append(node('p',item.condition,'small muted'));
      if(item.expires_at)row.append(node('p',`${new Date(item.expires_at).toLocaleDateString('ja-JP')} まで`,'small muted'));
      if(item.status==='review')row.append(node('p','教えてくれたことをもとに、見直しています。','small muted'));
      const why=node('details'),summary=node('summary','なぜそう思った？');why.append(summary);
      for(const o of [...item.evidence,...item.counterevidence])why.append(node('p',`${new Date(o.observed_at).toLocaleDateString('ja-JP')} ${o.summary}`,'small'));
      row.append(why);const controls=node('div',undefined,'row');row.append(controls);section.append(row);
      async function update(action,detail=''){await api(`/api/note/understandings/${item.id}`,{action,detail},'PUT');await refresh();}
      if(item.stage!=='known')action(controls,'合ってる',()=>update('agree'));
      action(controls,'違う',()=>{const edit=node('div',undefined,'set');let detail='';field(edit,'どこが違う？（一言でも、空欄でも）','',v=>detail=v);action(edit,'伝える',()=>update('disagree',detail));action(edit,'戻る',()=>edit.remove());row.append(edit);});
      action(controls,'忘れて',async()=>{if(await confirmDialog('この理解を忘れます。同じ観察からは作り直しません。','忘れて'))await update('forget');});
    }
  }
  if(shared)return;
  const explicit=node('section',undefined,'card');explicit.append(node('h3','あなたが話してくれたこと'));facts(explicit,data.facts,refresh,name,factView,'public');parent.append(explicit);
  const privateSection=node('details',undefined,'card');privateSection.append(node('summary','もっとプライベートなこと'));parent.append(privateSection);
  privateSection.addEventListener('toggle',async()=>{factView.privateOpen=privateSection.open;if(!privateSection.open||privateSection.dataset.loaded)return;const body=node('div');privateSection.append(body);try{const privateData=await api('/api/note/private');facts(body,privateData.facts,refresh,name,factView,'private');privateSection.dataset.loaded='true';}catch(e){body.textContent='読み込めませんでした。開き直してください。';}});
  privateSection.open=factView.privateOpen;
  await privateSettings(parent,refresh,name);
  const inference=node('section',undefined,'card set');inference.append(node('h3','あなたについての理解'));parent.append(inference);
  await inferenceSetting(inference,data);
}
