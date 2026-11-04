import {api,node,field,action,card} from './ui.js';

export async function daysSettings(parent,name,{habitsParent}={}){
  let state=await api('/api/partner/days');
  const box=card(parent,habitsParent?'大切な日':'大切な日と習慣'),habitBox=habitsParent?node('section',undefined,'set'):box;if(habitsParent)habitsParent.append(habitBox);
  let days,habits;box.id='partner-days';box.tabIndex=-1;
  const status=node('p','','small muted'),habitStatus=node('p','','small muted');status.setAttribute('role','status');habitStatus.setAttribute('role','status');
  function render(){
    box.replaceChildren(node('h3',habitsParent?'大切な日':'大切な日と習慣'));if(habitsParent)habitBox.replaceChildren();
    days??=structuredClone(state.days);habits??=structuredClone(state.habits);
    const dates=node('div',undefined,'set'),addActions=node('div',undefined,'row day-add-actions');box.append(dates);
    function dateRow(item,title){
      const row=node('fieldset',undefined,'day-row set');row.append(node('legend',title));dates.append(row);
      const summary=node('p',`${item.name||title} · ${item.year?item.year+'年':''}${item.month}月${item.day}日`),editor=node('div',undefined,'set');row.append(summary,editor);
      summary.hidden=!item.id;editor.hidden=!!item.id;
      const buttons=node('div',undefined,'row');row.append(buttons);const edit=action(buttons,'直す',()=>{summary.hidden=true;editor.hidden=false;edit.hidden=true;});edit.hidden=!item.id;
      if(item.kind==='anniversary')field(editor,'記念日の名前',item.name,v=>item.name=v).maxLength=100;
      const inputs=node('div',undefined,'day-date-fields');editor.append(inputs);
      for(const [key,label,min,max] of [['year','年（任意）',1,9999],['month','月',1,12],['day','日',1,31]]){
        const input=field(inputs,label,item[key]??'',v=>item[key]=v===''?null:Number(v));input.type='number';input.min=min;input.max=max;
      }
      action(buttons,'この日を削除',()=>{days.splice(days.indexOf(item),1);row.remove();const add=[...addActions.querySelectorAll('.day-add-birthday')].find(b=>b.dataset.kind===item.kind);if(add)add.hidden=false;});
    }
    for(const [kind,title] of [['user_birthday','あなたの誕生日'],['partner_birthday',`${name}の誕生日`]]){
      const existing=days.find(d=>d.kind===kind);
      if(existing)dateRow(existing,title);
      const add=action(addActions,`${title}を追加`,()=>{
        if(days.some(d=>d.kind===kind))return;
        const item={id:null,kind,name:title,month:null,day:null,year:null};days.push(item);dateRow(item,title);add.hidden=true;
      });add.classList.add('day-add-birthday');add.dataset.kind=kind;add.hidden=!!existing;
    }
    for(const item of days.filter(d=>d.kind==='anniversary'))dateRow(item,'記念日');
    action(addActions,'記念日を追加',()=>{const item={id:null,kind:'anniversary',name:'',month:null,day:null,year:null};days.push(item);dateRow(item,'記念日');});
    box.append(addActions);box.append(node('p','年は空欄でも保存できます。はじめて話した日から100日・毎年の節目は自動で数えます。','small muted'));
    const list=node('div',undefined,'set'),empty=node('p','まだありません。','small muted');let habitSave;const habitUnsaved=node('span','保存していない変更があります','small muted');habitUnsaved.hidden=true;const showSave=()=>{empty.hidden=habits.length>0;if(habitSave)habitSave.hidden=habitUnsaved.hidden=JSON.stringify(habits)===JSON.stringify(state.habits);};habitBox.append(node('h4','時刻の習慣'),list,empty);
    function habitRow(item){
      const row=node('fieldset',undefined,'habit-row set');row.append(node('legend','習慣'));list.append(row);
      const summary=node('p',`${item.name} · ${item.time} · ${item.weekdays.map(d=>'月火水木金土日'[d]).join('・')}曜日 · ${item.enabled?'有効':'お休み'}${item.allow_quiet?' · 静かな時間も話しかける':''}`),editor=node('div',undefined,'set');row.append(summary,editor);summary.hidden=!item.id;editor.hidden=!!item.id;
      const edit=action(row,'直す',()=>{summary.hidden=true;editor.hidden=false;edit.hidden=true;showSave();});edit.hidden=!item.id;
      field(editor,'名前（例：おはよう、おやすみ）',item.name,v=>item.name=v).maxLength=100;
      const time=field(editor,'時刻',item.time,v=>item.time=v);time.type='time';
      const weekdays=node('div',undefined,'row');weekdays.setAttribute('role','group');weekdays.setAttribute('aria-label','曜日');editor.append(weekdays);
      for(const [day,label] of ['月','火','水','木','金','土','日'].entries()){
        const wrap=node('label',undefined,'switch'),check=node('input');check.type='checkbox';check.checked=item.weekdays.includes(day);
        check.onchange=()=>item.weekdays=check.checked?[...item.weekdays,day]:item.weekdays.filter(d=>d!==day);wrap.append(check,node('span',label));weekdays.append(wrap);
      }
      for(const [key,title] of [['enabled','この習慣を使う'],['allow_quiet','この習慣は静かな時間帯にも話しかけてよい']]){
        const label=node('label',undefined,'switch'),check=node('input');check.type='checkbox';check.checked=item[key];check.onchange=()=>item[key]=check.checked;label.append(check,node('span',title));editor.append(label);
      }
      if(habitsParent){const rowSave=action(editor,'この習慣を保存',()=>habitBox.saveHabit(item),true),hint=node('span','保存していない変更があります','small muted');editor.append(hint);const paint=()=>rowSave.hidden=hint.hidden=JSON.stringify(item)===JSON.stringify(state.habits.find(h=>h.id===item.id));editor.addEventListener('input',paint);editor.addEventListener('change',paint);paint();}
      action(row,'この習慣を削除',()=>{habits.splice(habits.indexOf(item),1);row.remove();showSave();});
    }
    habits.forEach(habitRow);habitBox.oninput=habitBox.onchange=()=>showSave();
    action(habitBox,'習慣を追加',()=>{const item={id:null,name:'',time:'07:00',weekdays:[0,1,2,3,4,5,6],allow_quiet:false,enabled:true};habits.push(item);habitRow(item);showSave();});
    habitBox.append(node('p','時刻の前後に、一つの習慣につき一日一度だけ。アプリが動いている間、ブラウザでは開いている会話、常駐では端の吹き出しに届きます。','small muted'));
    const save=async kind=>{for(const e of (kind==='habits'?habitBox:box).querySelectorAll('input'))if(!e.reportValidity())return false;
      state=await api('/api/partner/days',{version:state.version,days:kind==='habits'?state.days:days,habits:kind==='days'?state.habits:habits},'PUT');
      if(kind!=='habits')days=structuredClone(state.days);if(kind!=='days')habits=structuredClone(state.habits);
      status.textContent='大切な日と習慣を保存しました。';window.dispatchEvent(new window.Event('habits-changed'));habitStatus.textContent='時刻の習慣を保存しました。';render();if(kind!=='habits')box.onSaved?.();if(kind!=='days')habitBox.onSaved?.();};
    box.save=()=>save(habitsParent?'days':'both');habitBox.saveHabits=()=>save('habits');
    habitBox.dirty=()=>JSON.stringify(habits)!==JSON.stringify(state.habits);
    habitBox.saveHabit=async item=>{
      const row=[...list.children].find((_,i)=>habits[i]===item);for(const e of row.querySelectorAll('input'))if(!e.reportValidity())return false;
      const before=new Set(state.habits.map(h=>h.id)),updated=item.id?state.habits.map(h=>h.id===item.id?item:h):[...state.habits,item];
      state=await api('/api/partner/days',{version:state.version,days:state.days,habits:updated},'PUT');
      Object.assign(item,state.habits.find(h=>item.id?h.id===item.id:!before.has(h.id)));habitStatus.textContent='時刻の習慣を保存しました。';render();window.dispatchEvent(new window.Event('habits-changed'));habitBox.onSaved?.();
    };
    const dateSave=action(box,habitsParent?'大切な日を保存':'大切な日と習慣を保存',()=>box.save(),true),dateUnsaved=node('span','保存していない変更があります','small muted');box.append(dateUnsaved);const paintDates=()=>dateSave.hidden=dateUnsaved.hidden=JSON.stringify(days)===JSON.stringify(state.days)&&(habitsParent||JSON.stringify(habits)===JSON.stringify(state.habits));box.oninput=box.onchange=box.onclick=paintDates;paintDates();
    if(habitsParent){habitSave=action(habitBox,'時刻の習慣を保存',()=>habitBox.saveHabits(),true);showSave();habitBox.append(habitUnsaved,habitStatus);}
    box.append(status);
  }
  render();box.habits=habitBox;return box;
}

export function dayNotice(article,notices=[]){
  if(!article||!notices.length||article.querySelector('.day-notice'))return;
  const line=node('p',notices.join('。')+'。 ','small muted day-notice'),link=node('a','大切な日を見る');
  link.href='#settings/relationship?focus=days';line.append(link);article.lastElementChild.append(line);
}
