import {api,node,action,icon} from './ui.js';

export function notice(parent,text='',kind='success'){const e=node('p');parent.append(e);e.update=(text,kind='success')=>{e.className=kind==='error'?'error onboarding-notice':kind==='success'?'onboarding-notice success':'status';e.setAttribute('role',kind==='error'?'alert':'status');e.replaceChildren(...(text&&['success','error'].includes(kind)?[icon(kind)]:[]),node('span',text));e.hidden=!text;return e;};return e.update(text,kind);}
export const policyDescriptions={may_elaborate:'書いた内容と矛盾しない範囲で、細かいところを補って話します。',keep:'書いた以上のことは足さず、書いた通りに扱います。'};
export const chapters=['AI','パートナー','くわしく','見た目','声','声で入力','バックアップ'];
// The partner chapter's pages; the AI chapter shows its first one, so both count from here.
export function partnerRoute(bringing='none'){return ['partner','bringing',...(['text','both'].includes(bringing)?['profile']:[]),...(['log','both'].includes(bringing)?['import']:[]),'world','partner-fields','user-fields','summary'];}
export function onboardingChrome(){document.body.classList.add('onboarding');for(const id of ['menu','heading','subtitle','steps','counter']){const e=document.getElementById(id);if(e)e.hidden=true;}}
let choiceId=0;
export function choiceCards(parent,title,options,value,change,{recommended=options[0]?.[0],label=''}={}){
  const box=node('fieldset',undefined,'qcard');if(title)box.append(node('legend',title));else if(label)box.setAttribute('aria-label',label);const name=`onboarding_${++choiceId}`;
  for(const option of options){
    const [key,label,description='']=option,row=node('label',undefined,'choice'),input=node('input'),copy=node('span');
    input.type='radio';input.name=name;input.value=key;input.checked=value===key;input.onchange=()=>{if(input.checked)change(key);};
    copy.append(node(description?'strong':'span',label,description?'':'choice-label'));if(key===recommended)copy.append(node('small','おすすめ','badge'));if(description)copy.append(node('small',description,'choice-description'));row.append(input,copy);box.append(row);
  }
  parent.append(box);return box;
}
const mandatory=new Set(['consent','ai','partner','world','review','done']);
export function renderExplanation(parent,text,name=''){
  const lines=text.replaceAll('{name}',name).split(/\r?\n/);
  // Copy without a heading line shows no heading.
  if(lines[0]?.startsWith('# '))parent.append(node('h2',lines.shift().slice(2)));
  function inline(element,value){
    // A single line break inside a paragraph is kept as a break, as authors expect when editing the copy.
    for(const [n,row]of value.split('\n').entries()){
      if(n)element.append(node('br'));
      for(const [i,part]of row.split('**').entries())element.append(node(i%2?'strong':'span',part));
    }
  }
  let paragraph=[],list;
  const flush=()=>{if(paragraph.length){const p=node('p');inline(p,paragraph.join('\n'));parent.append(p);paragraph=[];}};
  for(const line of lines){
    if(line.startsWith('- ')){flush();if(!list){list=node('ul');parent.append(list);}const li=node('li');inline(li,line.slice(2));list.append(li);}
    else {list=null;if(!line.trim())flush();else paragraph.push(line);}
  }
  flush();
}
export async function explanation(parent,id,name=''){
  let copy;try{copy=await api(`/api/onboarding/${id}`);}catch{copy={text:'# はじめに'};}
  renderExplanation(parent,copy.text,name);if(id==='done')parent.querySelector('p:not(#progress)')?.classList.add('done-message');return copy;
}
// Screens own their draft values; navigation never reconstructs that state.
export function sequence(parent,screens,{telemetryFirst=null,showCount=true,chapter='AI',visibleChapters=chapters,progressFor,onDone=()=>{},onWelcome}={}){
  let index=0,revision=0;const history=[];
  function backTo(id){const target=screens.findIndex(s=>s.id===id),at=history.lastIndexOf(target);if(at>=0)history.splice(at);return show(target,{remember:false,top:true});}
  async function show(next,{remember=true,top=false}={}){
    const previous=revision?index:-1;if(remember&&next!==index)history.push(index);index=next;const token=++revision;
    const scrollTop=document.scrollingElement?.scrollTop||0,jobProgress=document.getElementById('progress');parent.replaceChildren();const keep=next===previous&&!top;if(!keep)window.scrollTo?.(0,0);const screen=screens[index];
    if(telemetryFirst!==null&&ONBOARDING_STEPS.includes(screen.id)&&!visitedSteps.has(screen.id)){visitedSteps.add(screen.id);void api('/api/telemetry/step',{step:screen.id,first:telemetryFirst}).catch(()=>{});}
    // Only the cover sits in the middle; chapter headings stay at the same height.
    parent.classList.toggle('onboarding-centered',screen.id==='welcome');
    const progress=node('nav',undefined,'chapter-progress');progress.setAttribute('aria-label','準備の進み具合');
    const currentChapter=screen.chapter||chapter,chapterIndex=visibleChapters.indexOf(currentChapter);
    for(const [i,name]of visibleChapters.entries()){const segment=node('span',undefined,i===chapterIndex?'on':i<chapterIndex?'done':'');segment.setAttribute('aria-label',name);if(i===chapterIndex)segment.setAttribute('aria-current','step');progress.append(segment);}
    const count=progressFor?.(screen)||{current:index+1,total:screens.length},caption=node('div',undefined,'chapter-caption');caption.append(node('strong',count.label||currentChapter));if(showCount)caption.append(node('span',`${Math.max(1,count.current)} / ${Math.max(1,count.total)}`));
    if(screen.id!=='welcome')parent.append(progress,caption);
    const body=node('section',undefined,['welcome','done'].includes(screen.id)?'onboarding-unframed':'card'),buttons=node('div',undefined,'wizard-actions');parent.append(body,buttons);if(jobProgress)body.append(jobProgress);
    let result=await screen.render(body);if(token!==revision)return;
    if(jobProgress)body.append(jobProgress);
    if(screen.backAction)action(buttons,'戻る',screen.backAction);
    else if(screen.back)action(buttons,'戻る',()=>backTo(typeof screen.back==='function'?screen.back():screen.back));
    else if(history.length)action(buttons,'戻る',()=>show(history.pop(),{remember:false}));
    let nextButton;
    if(!screen.wait)nextButton=action(buttons,screen.label||'次へ',async()=>{
      for(const input of body.querySelectorAll('input,select,textarea'))if(!input.disabled&&!input.reportValidity())return;
      const target=await screen.next?.(result,{body});if(token!==revision)return;
      if(target===false)return;
      if((target==='done'&&!screens.some(s=>s.id==='done'))||(index===screens.length-1&&(!target||target===screen.id))){await onDone();return;}
      await show(typeof target==='string'?screens.findIndex(s=>s.id===target):index+1);
    },true);
    if(screen.explanation&&!mandatory.has(screen.id)){
      action(buttons,screen.skipLabel||'飛ばす',async()=>{const target=await screen.skip?.({body});if(target===false)return;if(target==='done'&&!screens.some(s=>s.id==='done')){await onDone();return;}if(typeof target==='string'){await show(screens.findIndex(s=>s.id===target));return;}const next=screens.findIndex((s,i)=>i>index&&s.explanation);if(next<0)await onDone();else await show(next);});
    }
    if(nextButton){nextButton.disabled=!!screen.pending;buttons.append(nextButton);}
    if(screen.leave)action(body,'このまとまりの残りは決めないでおく',async()=>{const id=await screen.leave({body});if(id===false)return;await show(screens.findIndex(s=>s.id===id));}).classList.add('quiet');
    if(screen.id==='done'){body.classList.add('onboarding-done');buttons.classList.add('done-actions');const back=buttons.querySelector('button:not(.primary)');if(back){back.classList.add('quiet');buttons.append(back);}}
    if(screen.id==='welcome')onWelcome?.(body,buttons);
    if(keep&&document.scrollingElement)document.scrollingElement.scrollTop=scrollTop;
    await screen.enter?.({go:(id,options)=>show(screens.findIndex(s=>s.id===id),options),active:()=>token===revision,body,setReady:ready=>{if(token===revision&&nextButton)nextButton.disabled=!ready;}});
  }
  return {start:(id,options={})=>show(id?screens.findIndex(s=>s.id===id):0,{remember:false,...options}),go:(id,options)=>show(screens.findIndex(s=>s.id===id),options),show};
}
// One expanded item, summaries remain visible. Disabled items explain why.
export function accordion(parent,items,{activeIndex,onActive=()=>{}}={}){
  let active=activeIndex??items.findIndex(i=>!i.disabled);const root=node('div');parent.append(root);
  function paint(focus=false){root.replaceChildren();items.forEach((item,index)=>{
    const section=node('section',undefined,'accordion-item'),heading=node('button',undefined,'accordion-row');
    heading.append(node('span',item.title),node('span',item.disabled?item.reason:(item.summary?.()||'未指定'),'accordion-summary'),icon(active===index?'up':'down'));
    heading.type='button';heading.disabled=!!item.disabled;heading.setAttribute('aria-expanded',String(active===index));section.append(heading);root.append(section);
    heading.onclick=()=>{active=active===index?-1:index;onActive(active);paint(true);};
    if(item.disabled)return;
    if(active===index){const body=node('div');section.append(body);item.render(body);action(body,'次の項目へ',()=>{for(const input of body.querySelectorAll('input,textarea'))if(!input.disabled&&!input.reportValidity())return;active=items.findIndex((i,n)=>n>index&&!i.disabled);onActive(active);paint(true);});}
  });if(focus&&active>=0){const heading=root.children[active].querySelector('button');heading.focus({preventScroll:true});heading.scrollIntoView?.({block:'start'});}}
  paint();return {refresh:paint};
}

export const ONBOARDING_STEPS = `welcome ai method oauth combination kind actor role-actor role-judge role-embedding role-research role-vision download keys zdr check summary partner
bringing profile import world partner-fields user-fields now likes now-question scenes scenes-question
open-style depth depth-question life life-question more more-question checks review confirmation review-wait
look look-style look-profile look-face look-hair look-outfit look-confirm look-sheet look-single look-faces
look-cover look-cover-import voice voice-autoplay voice-engine voice-download voice-method voice-description
voice-reference voice-url voice-model voice-name voice-style voice-confirm voice_input input-model
input-download input-device input-options input-confirm export export-trial export-options export-confirm done
entry-summary entry-actor entry-custom`.split(/\s+/);
const visitedSteps = new Set();
