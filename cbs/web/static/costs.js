import {saveOnEdit} from './settings-save.js';
import {api, node, field, card} from './ui.js';

const money=value=>value==null?'—':`$${Number(value).toFixed(4)}`;
const amount=row=>`${money(row.usd)}${row.unknown?`（不明 ${row.unknown} 件）`:''}`;

export function showCostNotice(parent, notice) {
  if(!notice||!parent)return;
  const line=node('p',`今月の費用が月の目安額を超えました。${notice.paused?'自主活動を休止しています。会話と記憶の整理は続けられます。':'会話はそのまま続けられます。'}`,'cost-notice');
  line.setAttribute('role','status');parent.append(line);
}

export async function checkCostNotice(parent, cfg) {
  if(!cfg.cost_summary)return;
  try{showCostNotice(parent,(await api('/api/costs/notice',{})).notice);}catch{}
}

export async function costSettings(parent, cfg, draft, noticeParent=parent,{autosave=false}={}) {
  if(!cfg.cost_summary)return;
  const box=card(parent,autosave?'費用':'使った費用の目安'),summary=cfg.cost_summary;
  box.classList.add('cost-settings');
  box.append(node('p','目安です。正確な請求は提供元の画面で確かめてください。金額は米ドル（USD）です。','small muted'));
  const totals=node('dl',undefined,'cost-totals');
  for(const [key,label]of [['today','今日'],['month','今月'],['previous','先月']]){
    const item=node('div');item.append(node('dt',label),node('dd',amount(summary[key])));totals.append(item);
  }
  box.append(totals);
  const table=node('table'),head=node('tr');
  for(const label of ['用途','今日','今月','先月']){const cell=node('th',label);cell.scope='col';head.append(cell);}
  const thead=node('thead');thead.append(head);table.append(thead);
  const tbody=node('tbody');
  for(const item of summary.month.purposes){
    const row=node('tr'),title=node('th',item.label);title.scope='row';row.append(title);
    for(const period of ['today','month','previous'])row.append(node('td',amount(summary[period].purposes.find(p=>p.id===item.id)||{usd:0,unknown:0})));
    tbody.append(row);
  }
  table.append(tbody);const breakdown=node('details');breakdown.append(node('summary','用途ごとの内訳'),table);box.append(breakdown);
  breakdown.append(node('p',`1ターンあたり ${money(summary.month.per_turn_usd)}（今月の全用途の合計 ÷ 完了した会話 ${summary.month.turns} ターン）。不明の費用は合計に含みません。`,'small muted'));
  draft.costs={monthly_guide_usd:cfg.costs.monthly_guide_usd??null,pause_autonomy:cfg.costs.pause_autonomy??false};
  const guide=field(box,'月の目安額（USD・空欄なら設定なし）',draft.costs.monthly_guide_usd,v=>draft.costs.monthly_guide_usd=v.trim()===''?null:Number(v));
  guide.type='number';guide.min='0.0001';guide.step='any';guide.inputMode='decimal';
  const label=node('label',undefined,'switch'),pause=node('input');pause.type='checkbox';pause.checked=draft.costs.pause_autonomy;
  pause.onchange=()=>draft.costs.pause_autonomy=pause.checked;
  label.append(pause,node('span','超えたら自主活動を止める'));box.append(label);
  box.append(node('p','月の目安額を超えた時に一度だけ知らせます。停止を選んでも会話・記憶の整理は続きます。翌月には自主活動を再開します。','small muted'));
  if(summary.paused)box.append(node('p','月の目安額を超えたため自主活動を休止しています。','status'));
  if(autosave){saveOnEdit(guide,v=>api('/api/settings',{costs:{monthly_guide_usd:v}},'PUT'),{read:()=>guide.value.trim()===''?null:Number(guide.value)});saveOnEdit(pause,v=>api('/api/settings',{costs:{pause_autonomy:v}},'PUT'));}
  if(autosave)box.append(breakdown);await checkCostNotice(noticeParent,cfg);
}
