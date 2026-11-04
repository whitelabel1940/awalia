import {api,node,plainMarkdown} from './ui.js';

export function recentActivities(parent,activities,archive){
  if(!activities.length&&!archive?.length)return;
  const section=node('section',undefined,'card recent-activities');section.append(node('h3','最近のこと'));parent.append(section);
  const weeks=new Map();
  for(const activity of [...activities].sort((a,b)=>b.date.localeCompare(a.date))){
    const [y,m,d]=activity.date.split('-').map(Number),date=new Date(Date.UTC(y,m-1,d));
    date.setUTCDate(date.getUTCDate()-(date.getUTCDay()+6)%7);
    const key=date.toISOString().slice(0,10);if(!weeks.has(key))weeks.set(key,[]);weeks.get(key).push(activity);
  }
  const headings=archive??[...weeks].map(([week_start,items])=>({week_start,count:items.length,loaded:true}));
  for(const [index,{week_start:key,count,loaded}]of headings.entries()){
    const [,month,day]=key.split('-').map(Number),week=node('details',undefined,'activity-week');week.open=index===0;week.dataset.week=key;
    week.append(node('summary',`${month}月${day}日の週（${count} 件）`));section.append(week);
    let cached=loaded?(weeks.get(key)||[]):null,loading=false,rendered=false;
    const render=items=>{
      const days=new Map();
      for(const activity of [...items].sort((a,b)=>b.date.localeCompare(a.date))){if(!days.has(activity.date))days.set(activity.date,[]);days.get(activity.date).push(activity);}
      for(const [index,[date,entries]]of [...days].entries()){
        const [,m,d]=date.split('-').map(Number),day=node('details',undefined,'activity-day');day.open=index===0;
        const weekday='日月火水木金土'[new Date(`${date}T00:00:00Z`).getUTCDay()];
        day.append(node('summary',`${m}月${d}日（${weekday}）${entries.length} 件`));week.append(day);
        for(const activity of entries){
      // The day heading already carries the date.
      const entry=node('article',undefined,'note-entry');
      entry.append(node('h4',plainMarkdown(activity.title)),node('p',plainMarkdown(activity.summary)),node('p',plainMarkdown(activity.impression),'impression'));
      const titles=activity.sources.map(s=>s.title).filter(Boolean);if(titles.length)entry.append(node('p',`読んだもの: ${titles.join('、')}`,'small muted'));day.append(entry);
    }}rendered=true;};
    const load=async()=>{
      if(!week.open||rendered||loading)return;
      if(cached!==null){render(cached);return;}
      loading=true;const status=node('p','読み込んでいます…','small muted');status.setAttribute('role','status');week.append(status);
      try{cached=(await api(`/api/note/activities?week=${encodeURIComponent(key)}`)).activities;status.remove();if(week.open)render(cached);}
      catch{status.textContent='読み込めませんでした。閉じてから、もう一度開いてください。';}
      finally{loading=false;}
    };
    week.addEventListener('toggle',()=>{if(week.open){for(const status of week.querySelectorAll('[role="status"]'))if(!loading)status.remove();void load();}});
    if(week.open)void load();
  }
  return section;
}
