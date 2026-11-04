import {api,openExternal} from './ui.js';

// A completed round trip, including reconnects, stays on the initiating screen.
export async function openRouterLogin(host,onConnected,onWaiting=()=>{},onError=()=>{}){
  // A browser tab must open within the click, before any await, or popup blockers stop it.
  const opening=openExternal('/oauth/openrouter/start');
  const before=await api('/api/openrouter/status');
  await opening;onWaiting();
  let stopped=false,timer,busy=false;
  const stop=()=>{stopped=true;clearTimeout(timer);window.removeEventListener('focus',check);document.removeEventListener('visibilitychange',check);window.removeEventListener('pagehide',stop);};
  async function check(){
    if(stopped||busy)return;if(!host.isConnected){stop();return;}busy=true;
    try{const status=await api('/api/openrouter/status');if(stopped||!host.isConnected)return;
      if(status.completed>before.completed){const cfg=await api('/api/settings');if(cfg.keys.openrouter==='set'&&host.isConnected){stop();await onConnected(cfg);}}}
    catch(error){if(host.isConnected){host.dataset.loginError=error.message;onError(error);}}
    finally{busy=false;clearTimeout(timer);if(!stopped)timer=setTimeout(check,1000);}
  }
  window.addEventListener('focus',check);document.addEventListener('visibilitychange',check);window.addEventListener('pagehide',stop);void check();return stop;
}
