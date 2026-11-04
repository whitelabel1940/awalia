const native=window.__TAURI__,byId=id=>document.getElementById(id);
let imageUrl='',origin,moved=false;
const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)');
let motionPreference=null,hasBubble=false,nativeVisible=false,motionActive=false,blinkTimer,restoreTimer;
function stopMotion(){
  clearTimeout(blinkTimer);clearTimeout(restoreTimer);
  blinkTimer=restoreTimer=undefined;motionActive=false;
  byId('face').classList.remove('idle-motion');byId('blink-image').hidden=true;
}
function scheduleBlink(){
  blinkTimer=setTimeout(()=>{
    if(!motionActive)return;
    const blink=byId('blink-image'),image=byId('image');
    if(!image.hidden&&blink.complete&&blink.naturalWidth>0){
      blink.hidden=false;
      restoreTimer=setTimeout(()=>{blink.hidden=true;restoreTimer=undefined;},120);
    }
    scheduleBlink();
  },15000+Math.random()*10000);
}
function reconcileMotion(){
  const active=nativeVisible&&(motionPreference??!reduced?.matches)&&!document.hidden&&!hasBubble;
  if(active===motionActive)return;
  stopMotion();
  if(active){motionActive=true;byId('face').classList.add('idle-motion');scheduleBlink();}
}
reduced?.addEventListener('change',reconcileMotion);
document.addEventListener('visibilitychange',()=>{stopMotion();reconcileMotion();});
window.addEventListener('pagehide',stopMotion);
byId('blink-image').onerror=()=>{byId('blink-image').hidden=true;};
byId('image').onerror=()=>{byId('image').hidden=true;byId('initial').hidden=false;byId('blink-image').hidden=true;};
for(const id of ['face','bubble']){
  const button=byId(id);
  button.onpointerdown=event=>{
    if(event.button!==0)return;
    origin={x:event.screenX,y:event.screenY};moved=false;
    button.setPointerCapture?.(event.pointerId);
  };
  button.onpointermove=event=>{
    if(!origin||moved||Math.hypot(event.screenX-origin.x,event.screenY-origin.y)<5)return;
    moved=true;origin=null;
    void native.core.invoke('resident_corner_drag').catch(showError);
  };
  button.onpointerup=()=>{origin=null;};
  button.onpointercancel=()=>{origin=null;};
  button.onclick=()=>{if(!moved)void native.core.invoke('resident_corner_open').catch(showError);};
}
function showError(error){byId('face').title=String(error);}
await native.event.listen('resident-corner-visibility',({payload})=>{
  nativeVisible=payload;reconcileMotion();
});
await native.event.listen('resident-corner',({payload})=>{
  // A state update cancels the transient overlay, so a new reaction is never covered.
  clearTimeout(restoreTimer);byId('blink-image').hidden=true;
  motionPreference=payload.idleMotion??null;hasBubble=!!payload.body;
  const neutralUrl=payload.neutralImageUrl;
  if(neutralUrl&&byId('blink-image').getAttribute('src')!==neutralUrl)byId('blink-image').src=neutralUrl;
  reconcileMotion();
  document.documentElement.style.setProperty('--face-size',`${payload.faceSize||96}px`);
  // Scale the whole layout only to fit a small work area, never with face size.
  document.documentElement.style.setProperty('--corner-scale',String(payload.layoutScale??1));
  byId('name').textContent=payload.name;
  byId('body').textContent=payload.body||'';
  byId('bubble').hidden=!payload.body;
  byId('initial').textContent=[...payload.name][0]||'○';
  if(imageUrl!==payload.imageUrl){
    imageUrl=payload.imageUrl;byId('image').hidden=false;byId('initial').hidden=true;byId('image').src=imageUrl;
  }
});
// Readiness means both the DOM and its event listener are usable.
await native.core.invoke('resident_corner_ready');
