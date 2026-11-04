export function questionnaireNamed(value,name){
  if(typeof value==='string')return value.replaceAll('{name}',name?.trim()||'あなたのパートナー');
  if(Array.isArray(value))return value.map(v=>questionnaireNamed(v,name));
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,questionnaireNamed(v,name)]));
  return value;
}
