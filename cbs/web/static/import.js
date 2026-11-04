import {notice} from './onboarding.js';
import {api,node,action,confirmDialog,field,select} from './ui.js';

export function aboutImport(parent,path='/api/import'){
  const box=node('section',undefined,'set');parent.append(box);
  let revision=0,reviewed=false,save;
  const text=field(box,'あなたについてのことや、お願い（任意）','',()=>{revision++;reviewed=false;results.replaceChildren();},'textarea');
  const results=node('div');box.append(results);
  action(box,'入れる内容を確かめる',async()=>{
    const version=revision,value=text.value;
    if(!value.trim())throw new Error('知っていてほしいことを貼り付けてください。');
    const review=await api(path+'/about/review',{text:value});
    if(version!==revision)return;
    results.replaceChildren(node('p','原文と種類を確かめ、入れる・入れないを選んでから保存してください。'));
    const choices=[];
    for(const f of review.candidates){
      const row=node('label',undefined,'choice'),check=node('input');check.type='checkbox';check.checked=false;check.value=f.id;
      row.append(check,node('span',`${f.content}（${f.destination_label||'あなたのこと'}${f.category_label?' ／ '+f.category_label:''}）`),node('blockquote',f.quote));results.append(row);
      let decision;
      if(f.existing_facts?.length){
        const matches=node('div',undefined,'set');results.append(matches);
        matches.append(node('p','同じ内容として覚えていること','small muted'));
        for(const old of f.existing_facts)matches.append(node('blockquote',old.content));
        decision=select(matches,'重なる内容をどうする？',[
          ['','選んでください'],...f.existing_facts.map(old=>[String(old.id),`まとめる：${old.content}`]),['keep','両方残す']
        ],'',()=>{});
      }
      choices.push({check,decision});
    }
    save=async()=>{
      if(version!==revision)throw new Error('貼り付けた内容をもう一度確かめてください。');
      const selected=choices.filter(c=>c.check.checked),decisions={};
      for(const {check,decision} of selected)if(decision){
        if(!decision.value)throw new Error('重なる内容をまとめるか、両方残すか選んでください。');
        decisions[check.value]=decision.value==='keep'?null:Number(decision.value);
      }
      const body={token:review.token,keys:selected.map(c=>c.check.value)};
      if(Object.keys(decisions).length)body.decisions=decisions;
      const saved=await api(path+'/about/save',body);
      if(version!==revision)return;
      reviewed=true;results.replaceChildren(node('p',`${saved.saved}件を保存しました。`));
    };
    action(results,'選んだ内容を保存',save,true);
  });
  return {ready:()=>!text.value.trim()||reviewed,save:async()=>{if(!text.value.trim()||reviewed)return;if(!save)throw new Error('取り込む内容を確かめてから保存してください。');await save();}};
}

export function importSettings(parent){
  const area=node('div',undefined,'set');parent.append(area);
  const host=node('div');let locked=false;
  const open=action(area,'ほかの場所の会話を足す',async()=>{
    if(locked&&host.firstElementChild){host.firstElementChild.focus();host.firstElementChild.scrollIntoView?.({block:'nearest'});return;}
    host.replaceChildren();locked=true;explanation.hidden=true;
    try{await openImport(host,{onClose:()=>{locked=false;explanation.hidden=false;open.focus();},onComplete:()=>{locked=false;}});}
    catch(e){host.replaceChildren();locked=false;explanation.hidden=false;throw e;}
  });
  const explanation=node('p',importExplanation,'small muted');area.append(explanation,host);area.save=async()=>{await host.firstElementChild?.save?.();};return area;
}

const importExplanation='同じ相手との続きとして、元の日付で覚えます。ChatGPT・Claudeのエクスポートしたファイル、ExporterのJSON・Markdownを選べます。';

export async function openImport(parent,options={}){
  const path=options.path||'/api/import';
  const panel=node('section',undefined,'card set');parent.append(panel);
  panel.tabIndex=-1;
  panel.append(node('h3','ほかの場所の会話を足す'),node('p',importExplanation));
  const file=node('input');file.type='file';file.accept='.zip,.json,.md,.markdown';file.setAttribute('aria-label','会話のエクスポートしたファイル');panel.append(file);
  const list=node('div',undefined,'set'),paste=node('div',undefined,'set'),quote=node('div',undefined,'set'),progress=node('div',undefined,'set'),past=node('div',undefined,'set');panel.append(list,paste,quote,progress,past);
  let setting;
  if(options.onContinue)setting=field(paste,'設定・指示（任意）',options.text||'',()=>{},'textarea');
  const about=aboutImport(paste,path);
  if(options.onContinue)action(paste,'会話ファイルなしで人物像づくりへ',()=>{
    if(!about.ready())throw new Error('あなたについての内容を確かめて保存してください。');
    return options.onContinue(null,setting.value);
  });
  let preview,choices=[],watching=null,pollTimer,pendingFile=false;
  panel.save=async()=>{await about.save();if(pendingFile)throw new Error('会話ファイルの時間と費用の目安を確かめ、取り込みを始めてください。');};
  if(options.onClose)action(panel,'閉じる',()=>{clearTimeout(pollTimer);watching=null;panel.remove();options.onClose();});
  async function show(state,notifyComplete=true){
    if(!panel.parentNode)return;
    if(state.status!=='completed')options.onPending?.();
    clearTimeout(pollTimer);watching=state.id;
    progress.replaceChildren();
    const labels={running:'覚えています',cancelling:'区切りまで終えて止めています',cancelled:'途中で止めました',failed:'途中で止まりました',completed:'会話を覚えました',undone:'取り込みを取り消しました'};
    const label=!state.total&&state.status==='completed'?'あなたについての内容を保存しました':labels[state.status]||state.status;
    if(notifyComplete&&state.status==='completed')options.onComplete?.();
    const status=node('p',label+(state.total?`（${state.completed} / ${state.total} 会話・${state.percent}%）`:''));status.setAttribute('role','status');status.setAttribute('aria-live','polite');progress.append(status);
    if(options.onboarding&&['completed','failed'].includes(state.status)){status.hidden=true;notice(progress,state.error||label,state.status==='failed'?'error':'success');}else if(state.error)progress.append(node('p',state.error));
    if(options.onboarding&&state.status==='completed'&&state.total){
      if(about.ready())await options.onContinue(state.id,setting.value);
    }
    if(!options.onboarding&&options.onContinue&&state.total&&state.status!=='undone')action(progress,'人物像づくりへ',()=>{
      if(!about.ready())throw new Error('あなたについての内容を確かめて保存してください。');
      return options.onContinue(state.id,setting.value);
    },true);
    if(['running','cancelling'].includes(state.status)){
      if(state.status==='running')action(progress,'いったん止める',async()=>show(await api(`${path}/${state.id}/cancel`,{})));
      pollTimer=setTimeout(async()=>{if(!panel.isConnected||watching!==state.id)return;try{const next=await api(`${path}/${state.id}`);if(watching===state.id)await show(next);}catch(e){if(watching===state.id)status.textContent=e.message;}},1500);pollTimer.unref?.();
    }else if(state.status!=='undone'){
      if(['cancelled','failed'].includes(state.status))action(progress,'続きから再開',async()=>{
        if(await confirmDialog(`残りを同じ見積もり（全体で約${Math.ceil(state.quote.estimated_seconds/60)}分・$${state.quote.estimated_usd}）で再開します。`,'再開する'))await show(await api(path+'/start',{token:state.id,confirmed:true}));
      });
      action(progress,'この取り込みをまとめて取り消す',async()=>{if(await confirmDialog('この回で足した会話と記憶をまとめて取り消します。','取り消す'))await show(await api(`${path}/${state.id}/undo`,{}));});
    }
  }
  file.onchange=async()=>{
    options.onPending?.();
    list.replaceChildren();quote.replaceChildren();choices=[];
    try{
      const selected=file.files[0];pendingFile=!!selected;if(!selected)return;if(selected.size>64*1024*1024)throw new Error('エクスポートしたファイルは64 MiB以下に分けてください。');
      const bytes=new Uint8Array(await selected.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
      preview=await api(path+'/preview',{filename:selected.name,payload:btoa(binary)});
      const all=node('input');all.type='checkbox';all.checked=true;const allLabel=node('label','すべて選ぶ');allLabel.prepend(all);list.append(allLabel);
      for(const c of preview.conversations){
        const row=node('label',undefined,'choice'),check=node('input');check.type='checkbox';check.checked=!c.error&&!c.duplicate;check.disabled=!!c.error||c.duplicate;check.value=c.key;
        row.append(check,node('span',`${c.title} ／ ${c.first_at?.slice(0,10)||'日付不明'} 〜 ${c.last_at?.slice(0,10)||''} ／ ${c.message_count}発言 ／ ${c.format}${c.duplicate?'（取り込み済み・重複）':''}${c.error?' ／ '+c.error:''}${c.inferred_times?' ／ 時刻を補完':''}`));list.append(row);choices.push(check);
      }
      all.onchange=()=>{for(const check of choices)if(!check.disabled)check.checked=all.checked;};
      action(list,'時間と費用の目安を見る',async()=>{
        if(!about.ready())throw new Error('あなたについての内容を確かめて保存してください。');
        const plan=await api(path+'/plan',{preview_id:preview.preview_id,keys:choices.filter(c=>c.checked&&!c.disabled).map(c=>c.value)});
        quote.replaceChildren(node('p',`${plan.conversations}会話・${plan.messages}発言。約${Math.ceil(plan.estimated_seconds/60)}分、$${plan.estimated_usd}。${plan.estimate_note}`));
        action(quote,'この目安で取り込む',async()=>{if(!about.ready())throw new Error('あなたについての内容を確かめて保存してください。');const state=await api(path+'/start',{token:plan.token,confirmed:true});pendingFile=false;quote.replaceChildren();list.replaceChildren();await show(state);},true);
      });
    }catch(e){list.replaceChildren(node('p',e.message));}
  };
  const batches=await api(path);
  for(const batch of batches)action(past,`${batch.created_at.slice(0,10)}の取り込み（${batch.total?`${batch.completed}/${batch.total} 会話`:'あなたについて'}）`,async()=>show(await api(`${path}/${batch.id}`),false));
  if(batches[0])await show(batches[0],false);
  return panel;
}
