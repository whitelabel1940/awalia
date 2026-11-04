export function preference(key, fallback = '') {
  try { return localStorage.getItem(`cbs.${key}`) ?? fallback; } catch { return fallback; }
}
export function remember(key, value) {
  try { localStorage.setItem(`cbs.${key}`, value); } catch { /* Storage may be unavailable. */ }
}
const systemTheme=window.matchMedia?.('(prefers-color-scheme: dark)');
function brandIcons(){
  const value=document.documentElement.dataset.theme,theme=value==='dark'||(value!=='light'&&systemTheme?.matches)?'dark':'light';
  const favicon=document.getElementById('brand-favicon'),touch=document.getElementById('brand-touch');
  if(favicon)favicon.href=`/static/brand/icon-${theme}.svg`;
  if(touch)touch.href=`/static/brand/app-${theme}.png`;
}
export function applyTheme(value = preference('theme', 'system')) { document.documentElement.dataset.theme = value;brandIcons(); }
systemTheme?.addEventListener('change',brandIcons);
export const accents={mauve:['藤紫','#835c75','#b898b0'],madder:['茜','#9c534d','#c2746e'],evergreen:['常盤','#546e61','#7b9b8b'],charcoal:['薄墨','#595a61','#888994']};
export function applyAccent(value = preference('accent', 'mauve')) { value=({amber:'mauve',akane:'madder',tokiwa:'evergreen',usuzumi:'charcoal'})[value]||value;document.documentElement.dataset.accent=Object.hasOwn(accents,value)?value:'mauve'; }
applyTheme();
applyAccent();
