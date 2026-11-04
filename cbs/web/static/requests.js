import {api, node, field, action, card} from './ui.js';

export async function requestSettings(parent, name, {extra,afterSave}={}) {
  let state=await api('/api/partner/requests');
  const box=card(parent,'話し方とお願い');box.id='partner-requests';box.tabIndex=-1;
  const status=node('p','','small muted');status.setAttribute('role','status');
  function render() {
    box.replaceChildren(node('h3','話し方とお願い'));
    const draft={...state.fields};
    for(const [key,title] of Object.entries({first_person:'一人称',second_person:'あなたの呼び方',user_name:'あなたの名前（任意）',register:'口調',length:'返事の長さ'})) {
      const input=field(box,title,draft[key],v=>draft[key]=v);input.id=`request-${key}`;input.maxLength=state.limits.max_field_chars;
    }
    const area=field(box,`${name}へのお願い`,state.lines.map(l=>l.text).join('\n'),()=>{},'textarea');area.id='request-lines';area.rows=5;
    const help=node('span','これからも続けてほしい話し方や関わり方を、1行に1件ずつ書いてください。','small muted');help.id='request-lines-help';
    area.before(help);area.setAttribute('aria-describedby',help.id);
    area.placeholder='これからも続けてほしいことを1行に1件\n例：絵文字を使わない\n相談には先に結論から答える';
    box.append(node('p',`最大${state.limits.max_lines}件・1行${state.limits.max_line_chars}文字まで。`,'small muted'));
    const info=state.updated_at?`${new Date(state.updated_at).toLocaleString('ja-JP')} · ${state.source==='conversation'?'会話で':state.source==='import'?'取り込み':state.source==='undo'?'一つ前に戻しました':'設定で'}`:'迎えた時の話し方です。';
    box.append(node('p',info,'small muted'));
    extra?.(box);
    const buttons=node('div',undefined,'row');box.append(buttons,status);
    box.save=async()=>{
      const texts=area.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),used=new Set();
      // Preserve unchanged row IDs across edits/reordering; changed lines receive new IDs.
      const lines=texts.map(text=>{const old=state.lines.find(l=>l.text===text&&!used.has(l.id));if(old)used.add(old.id);return {id:old?.id??null,text};});
      state=await api('/api/partner/requests',{version:state.version,fields:draft,lines},'PUT');
      await afterSave?.();status.textContent='話し方とお願いを保存しました。';render();box.onSaved?.();
    };
    const saveButton=action(buttons,'話し方とお願いを保存',()=>box.save(),true),unsaved=node('span','保存していない変更があります','small muted');buttons.prepend(unsaved);const snapshot=()=>JSON.stringify([...box.querySelectorAll('input,textarea,select')].filter(e=>!e.dataset.transient).map(e=>e.value)),baseline=snapshot(),paint=()=>saveButton.hidden=unsaved.hidden=snapshot()===baseline;box.oninput=box.onchange=paint;paint();
    const undo=action(buttons,'一つ前に戻す',async()=>{
      state=await api('/api/partner/requests/undo',{version:state.version});status.textContent='一つ前の版に戻しました。';render();box.onSaved?.();
    });undo.disabled=!state.can_undo;
  }
  render();return box;
}

export function requestNotice(article, notices=[]) {
  if(!article||!notices.length||article.querySelector('.request-notice'))return;
  const line=node('p',notices.join('。')+'。 ','small muted request-notice'),link=node('a','話し方とお願いを見る');
  link.href='#settings/relationship';line.append(link);article.lastElementChild.append(line);
}
