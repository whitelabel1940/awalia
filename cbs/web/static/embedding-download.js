import {notice} from './onboarding.js';
import {api,node,action,card,confirmDialog} from './ui.js';

export function embeddingDownload(parent,selected,{endpoint='/api/embedding',title='ローカルの記憶検索モデル',label='記憶検索モデル',parameters=()=>({}),onReady,onboarding=false}={}){
  const section=card(parent,title),status=node('p','','status'),bar=node('progress');
  status.setAttribute('role','status');bar.max=100;bar.setAttribute('aria-label',`${label}の取得率`);section.append(status,bar);const result=onboarding?notice(section,''):null;
  let timer,version=0;
  const bytes=n=>`${((n||0)/1e6).toFixed(1)} MB`;
  function choice(){const model=selected();return model.startsWith('local/')?model.slice(6):null;}
  function paint(state){
    const active=state.status==='downloading';
    const labels={missing:'未取得',downloading:'取得中',ready:'準備完了',cancelled:'取り消しました。途中から再開できます。',error:'取得エラー'};
    status.textContent=`${onboarding?label:state.model}: ${labels[state.status]||state.status} ${bytes(state.completed)} / ${bytes(state.total)}（${state.percent??0}%）${active?(state.eta_seconds==null?' 残り時間を計算中':` 残り約${Math.ceil(state.eta_seconds/60)}分`):''} ${state.reason||''} ${state.error||''}`;
    if(onboarding){status.hidden=['ready','error'].includes(state.status);result.update(state.status==='ready'?(state.model||choice())+'の取得が完了しました。次へ進めます。':state.status==='error'?(state.error||'取得に失敗しました。'):'',state.status==='error'?'error':'success');}
    bar.hidden=!active;bar.value=state.percent||0;cancel.hidden=!active;acquire.hidden=active||state.status==='ready';
    acquire.textContent=onboarding?'取得する':state.status==='cancelled'||state.status==='error'?'途中から再開する':`${label}を取得する`;
    if(state.status==='ready'&&onReady&&section.isConnected)void onReady();
    clearTimeout(timer);if(active)timer=setTimeout(()=>{if(section.isConnected)void refresh();},750);
  }
  async function refresh(){
    const id=++version,c=choice();section.hidden=!c;
    if(!c){clearTimeout(timer);return;}
    try{const state=await api(endpoint+'/download?choice='+encodeURIComponent(c)+'&'+new URLSearchParams(parameters()));if(id===version)paint(state);}
    catch(e){if(id===version){if(onboarding)result.update(e.message,'error');else status.textContent=e.message;bar.hidden=true;acquire.hidden=false;cancel.hidden=true;}}
  }
  const acquire=action(section,onboarding?'取得する':`${label}を取得する`,async()=>{
    const c=choice();if(!c)return;
    try{const plan=await api(endpoint+'/download/plan',{choice:c,...parameters()});
    if(!onboarding&&!await confirmDialog(`${c} を取得します。${plan.reason||''} 全体 ${bytes(plan.total)}、残り ${bytes(plan.download_bytes)}。途中で取り消しても再開できます。`))return;
    if(c!==choice())return;
    paint(await api(endpoint+'/download',{choice:c,token:plan.token,confirmed:true}));}catch(error){if(onboarding)result.update(error.message,'error');else throw error;}
  });

  const cancel=action(section,'取得を取り消す',async()=>{await api(endpoint+'/download/cancel',{});await refresh();});if(onboarding)cancel.classList.add('quiet');
  section.hidden=true;bar.hidden=true;cancel.hidden=true;
  return {refresh,acquire,cancel,async requireReady(){
    const c=choice();if(!c)return;
    const state=await api(endpoint+'/download?choice='+encodeURIComponent(c)+'&'+new URLSearchParams(parameters()));paint(state);
    if(state.status!=='ready'||state.model!==c)throw new Error(`${label}の取得を完了してから設定を保存してください。`);
  }};
}
