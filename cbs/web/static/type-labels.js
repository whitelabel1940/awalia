// This diagnostic has no observers, listeners or DOM changes without the URL flag.
if(new URLSearchParams(location.search).has('type-labels')){
  const tiers={page:'ページ',title:'大見出し',section:'節',body:'本文',ui:'UI',note:'補足',meta:'注記'};
  const layer=document.createElement('div');layer.setAttribute('aria-hidden','true');layer.id='type-labels';document.body.append(layer);
  let pending=false;
  const draw=()=>{
    pending=false;observer.disconnect();layer.replaceChildren();
    const root=getComputedStyle(document.documentElement),sizes=Object.entries(tiers).map(([key,label])=>[root.getPropertyValue(`--type-${key}`).trim(),label]);
    for(const e of document.body.querySelectorAll('*')){
      if(e===layer||layer.contains(e)||e.closest('script,style,noscript,.sr-only,[hidden]'))continue;
      if(![...e.childNodes].some(n=>n.nodeType===3&&n.textContent.trim())&&!e.matches('input,textarea,select'))continue;
      const style=getComputedStyle(e),r=e.getBoundingClientRect();
      if(style.visibility!=='visible'||style.display==='none'||!r.width||!r.height||r.bottom<0||r.top>innerHeight)continue;
      const label=sizes.find(([size])=>size===style.fontSize)?.[1];if(!label)continue;
      const tag=document.createElement('span');tag.className='type-label';tag.textContent=label;
      tag.style.left=Math.max(0,r.left)+'px';tag.style.top=Math.max(0,r.top-12)+'px';layer.append(tag);
    }
    observer.observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true});
  };
  const schedule=()=>{if(!pending){pending=true;requestAnimationFrame(draw);}};
  const observer=new MutationObserver(schedule),resize=new ResizeObserver(schedule);resize.observe(document.body);
  window.addEventListener('resize',schedule);window.addEventListener('scroll',schedule,true);
  document.fonts?.ready.then(schedule);schedule();
}
