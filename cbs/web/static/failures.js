import {api, $, node, failureText} from './ui.js';

export function showFailures(notices) {
  const banner=$('llm-failures');if(!banner)return;
  banner.replaceChildren();
  for(const notice of notices){const line=node('p');failureText(line,notice);banner.append(line);}
  banner.hidden=notices.length===0 || !['','#talk'].includes(location.hash);
  // The banner already says what the send error said; show it once.
  const error=$('error'),said=error?.firstChild?.textContent;
  if(!banner.hidden&&error&&!error.hidden&&said&&notices.some(n=>n.message.endsWith(said)))error.hidden=true;
}
export async function refreshFailures() {
  try {showFailures((await api('/api/llm/failures')).notices);}catch{/* A polling error must not replace the conversation error. */}
}
