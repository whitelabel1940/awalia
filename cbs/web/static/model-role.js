import {node,field,select} from './ui.js';

// Shared by settings and the one-role onboarding screens. Presets may replace a role.
export function modelRoleFields(parent,title,options,value,onChange,choose=select,{onboarding=false,examples=()=>''}={}){
  const current=typeof value==='function'?value:()=>value;
  const pair=node('div',undefined,onboarding?'model-role-stack':'pair');parent.append(pair);
  const p=choose(pair,`${title}のサービス`,options,current().provider,v=>{current().provider=v;if(onboarding){current().model='';m.value='';m.placeholder=examples(v);}m.disabled=v==='none';m.required=v!=='none';onChange(current());});
  const m=field(pair,`${title}のモデル名`,current().model,v=>{current().model=v;onChange(current());});
  if(onboarding)m.placeholder=examples(current().provider);
  m.required=current().provider!=='none';m.disabled=current().provider==='none';m.autocomplete='off';
  return {provider:p,model:m};
}
