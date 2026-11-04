import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const mode=process.argv[2];
if(!mode){for(const name of ['look_editor','settings','augment','chat','first','canvas','settings_empty','note','note_interests','k2_simple','k2_gate','k2_callback','k2_choices','late_expression','m8b_settings','m8b_note','m8c_note','m8c_inner','sp1_opening','sp1_opening_error']){execFileSync(process.execPath,[fileURLToPath(import.meta.url),name],{stdio:'inherit'});}process.exit(0);}
// New-partner UI coverage lives in test_ob1_client.mjs and test_ob2_client.mjs.
// Offline DOM unit checks only: no browser, HTTP requests, resource loading or real LLM.
const require=createRequire(import.meta.url),{JSDOM}=require('../.tmp/m9b_dom/node_modules/jsdom');
const isWizard=['im_welcome','fb2','fb2_compatible','fb2_cleared','fb1','wizard','augment','profile','sections','failure','layout','conflicts','confirmation','progress','k2_wizard','k2_resume','k2_connection_fail'].includes(mode);
const html=readFileSync(new URL(`../cbs/web/static/${mode==='m8c_inner'?'dev':isWizard?'wizard':'index'}.html`,import.meta.url),'utf8');
const dom=new JSDOM(html,{url:`https://unit.invalid/${isWizard?'wizard':''}${mode==='augment'?'?augment=test_partner':''}`,runScripts:'outside-only'});
for(const key of ['window','document','location','localStorage','history','sessionStorage'])globalThis[key]=dom.window[key];
localStorage.setItem('cbs.accent','tokiwa');
dom.window.scrollTo=()=>{};
const calls=[],cfg={models:{actor:'gemini/actor',judge:'openai/judge',steward:'gemini/steward',embedding:'gemini/embed'},llm:{providers:{openai:{api:'responses',base_url:null}}},keys:{gemini:'unset',openai:'set'},warnings:[],reindex_required:false,pipeline:{post_check:'sync'},autonomy:{inner_life:true,inner_life_interval_hours:4,research:true,research_daily_max:3,quiet_start_hour:0,quiet_end_hour:8,opening:'normal'}};
if(mode.startsWith('k2_')){
  cfg.advanced_models=structuredClone(cfg.models);cfg.llm.mode='simple';cfg.ai_ready=false;
  cfg.models={actor:'openrouter/google/gemini-3.8-flash',judge:'openrouter/openai/gpt-6-luna',steward:'openrouter/google/gemini-3.8-flash',embedding:'openrouter/google/gemini-embedding-2',research:'openrouter/google/gemini-3.8-flash'};
  cfg.keys.openrouter=mode==='k2_callback'?'set':'unset';
  if(mode==='k2_callback')dom.reconfigure({url:'https://unit.invalid/?openrouter=connected#settings'});
  if(['k2_first_run','k2_resume','k2_connection_fail'].includes(mode)){
    cfg.connection_verified=mode==='k2_resume';cfg.ai_ready=mode!=='k2_first_run';
  }
  if(mode==='k2_choices'){
    cfg.simple={actor:cfg.models.actor,judge:cfg.models.judge};
    cfg.model_candidates={actor:[{model:cfg.models.actor,label:'標準',description:'確認済みの標準',input_usd:.75,output_usd:3.75,status:'passed'},
      {model:'openrouter/failed/model',label:'失敗',description:'検証で問題',input_usd:1,output_usd:2,status:'failed'}],judge:[{model:cfg.models.judge,label:'判断',description:'標準の判断',input_usd:.1,output_usd:.5,status:'passed'}]};
  }
}
cfg.early={notify:true};
cfg.display={split_replies:true,delay_user:false,typing_seconds:0};
cfg.stt={auto_send:false,max_seconds:60,silence_seconds:2,device:'auto'};
cfg.inner={show_monologue:true,baseline_hours:12,want_decay_per_day:.1,long_gap_hours:24};
if(mode==='late_expression'){
  cfg.tts={enabled:true,autoplay:true};
  globalThis.Audio=class {async play(){} pause(){}};
}
const q={id:'R1',group:'answers',section:'relationship',domain:'R01',scene:'今日は疲れたと話しかけられた。',choices:{leave_open:'あなたのパートナーに任せる',A:'ゆっくり聞く',unknown:'まだ分からない'},multi:false};
const catalog={questionnaire:{common_choices:{leave_open:'あなたのパートナーに任せる',unknown:'まだ分からない'},sections:['relationship','depth','now_life','life'].map(id=>({id,description:`区切りの説明: ${id}`}))},domains:[]};
const plan={questions:[q],reader:{items:[]},coverage:{R01:'open'},confirmation_required:[],warnings:[]};
if(mode==='sections'){
  plan.questions=[q,{...q,id:'R2',domain:'R02'}, {...q,id:'Q1',group:'life',section:'life'}, {...q,id:'L1now',group:'now',section:'now_life'}, {...q,id:'A1',domain:'CBS01',section:'depth'}];
  Object.assign(plan.coverage,{R02:'open',CBS01:'open'});
}
if(mode==='fb1'){
  plan.questions=[q,{...q,id:'L1now',group:'now',section:'now_life',scene:'普段は何をして過ごしている？'}];
  plan.reader.setting_conflicts=[
    {setting:'persona.ai_question',topic:'答え方',quote:'開発されたAI',detail:'存在の指定と答え方が食い違っています。'},
    {setting:'world',choice_template:'appearance',topic:'猫耳',quote:'猫耳がある',detail:'現実の世界と耳の指定が食い違っています。'},
    {setting:'world',topic:'世界',quote:'空を飛ぶ島',detail:'現実の世界と島の指定が食い違っています。'},
  ];
}
let partnerPersona={ai_question:'playful'};
const jobs=new Map();let jobNumber=0, reindexed=false, uploaded=0;
let sources=[{id:1,title:'読む先',url:'https://reader.example/',enabled:1,error:'robots.txt により取得できません。'},{id:3,title:'一枚のページ',url:'https://single.example/',kind:'page',structure_checked:1,enabled:1,error:null},{id:4,title:'確認待ち',url:'https://pending.example/',kind:'page',structure_checked:0,enabled:1,error:null}];
if(mode==='m8b_settings')cfg.autonomy.tools={research:{enabled:true,daily_max:3},read_source:{enabled:true,daily_max:3},read_book:{enabled:true,daily_max:2}};
const at='2026-10-14T12:00:00+09:00',old='2026-10-11T12:00:00+09:00';
let turns=mode==='chat'?[{id:1,user_text:'前の会話',assistant_text:'またね',created_at:old,assistant_at:old,status:'completed',kind:'conversation',expression:null}]:[];
let releaseStream,releaseExpression;const streamGate=new Promise(resolve=>releaseStream=resolve),expressionGate=new Promise(resolve=>releaseExpression=resolve);
let releaseOpening;const openingGate=new Promise(resolve=>releaseOpening=resolve);
const lookCatalog=JSON.parse(execFileSync(fileURLToPath(new URL('../.venv/Scripts/python.exe',import.meta.url)),['-c','import json; from cbs.look import field_catalog; print(json.dumps(field_catalog()))'],{encoding:'utf8'}));
const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(path,options={})=>{
  const route=path.split('?')[0],body=options.body&&typeof options.body==='string'?JSON.parse(options.body):null;calls.push({route,path,method:options.method||'GET',body});
  if(route==='/api/onboarding/state')return json({consent:{at:'test',version:'test'}});
  if(route.startsWith('/api/onboarding/'))return json({text:'# パートナーについて'});
  if(route.startsWith('/api/bootstrap/import/'))return json([]);
  if(route==='/api/partner/requests')return json({version:0,fields:{first_person:'私',second_person:'あなた',register:'自然に',length:'短く'},lines:[],limits:{max_lines:40,max_line_chars:300,max_field_chars:1000},can_undo:false});
  if(route==='/api/persona'){if(body)partnerPersona=body;return json(partnerPersona);}
  if(route==='/api/partner')return json({partner:mode==='k2_first_run'?null:{id:'test_partner',name:'ユキ'}});
  if(route==='/api/sources'){if(options.method==='POST'){const source={id:2,title:'新しいサイト',url:body.url,enabled:true};sources.push(source);return json(source);}return json(sources);}
  if(route.startsWith('/api/sources/')){if(options.method==='DELETE')sources=sources.filter(s=>s.id!==Number(route.split('/')[3]));return json({ok:true});}
  if(route==='/api/settings/connection')return mode==='k2_connection_fail'?json({detail:'残高または利用上限を確認してください。'},422):json({status:'connected'});
  if(route==='/api/settings/keys'){cfg.keys[body.provider]='set';return json({status:'set'});}
  if(route==='/api/settings'){if(options.method==='PUT'){if(body.models?.embedding&&body.models.embedding!==cfg.models.embedding)cfg.reindex_required=true;for(const [key,value]of Object.entries(body))Object.assign(cfg[key],value);}return json(cfg);}
  if(mode==='late_expression'&&route.endsWith('/audio/stream'))return new Response(new Uint8Array([82,73,70,70]),{headers:{'Content-Type':'audio/wav'}});
  if(route==='/api/look')return json({accent:'amber',features:'',expressions:mode==='late_expression'?['neutral','thinking','smile']:[]});
  if(route==='/api/look/fields')return json(lookCatalog);
  if(route==='/api/look/draft')return json({assignments:[{path:'person.hair.color',value:'銀色',quote:'銀色の髪',source:'text'},{path:'person.hair.length',value:'肩まで',quote:'肩まで',source:'text'},{path:'person.face.marks',value:'そばかす',quote:'そばかす',source:'text'}],suggestions:[{path:'person.profile.vibe',value:'穏やかな微笑み',quote:'ゆっくり聞く',source:'model'}],dropped:1});
  if(route==='/api/look/expression-suggestions'||route.endsWith('/look-expressions'))return json({suggestions:[
    {path:'person.face.habits',value:'目元に気持ちが出る',quote:'感情は目元で示す',source:'model'},
    ...['smile','sad','pout','thinking'].map(id=>({path:`person.expressions.${id}`,value:`固有の描写:${id}`,quote:'控えめに感情を表す',source:'expression'}))],dropped:0});
  if(route==='/api/look/meta')return json(body);
  if(route==='/api/look/prompt')return json({prompt:'APIが作った4列2行のプロンプト'});
  if(route.startsWith('/api/look/')&&options.method==='PUT'){uploaded++;return json({});}
  if(route.startsWith('/api/look/')&&options.method==='DELETE')return json({});
  if(route==='/api/maintenance/reindex'){reindexed=true;return json({status:'completed',completed:4,total:4});}
  if(route==='/api/embedding/download')return json({status:'ready',model:new URL(path,'https://unit.invalid').searchParams.get('choice'),total:140032944,completed:140032944,percent:100});
  if(route==='/api/bootstrap/questionnaire')return json(catalog);
  if(route==='/api/characters/test_partner/augment'&&!options.method)return json({questions:mode==='settings_empty'?[]:[q]});
  if(route==='/api/bootstrap/jobs/draft/confirm')return json({id:'test_partner'});
  if(route.startsWith('/api/bootstrap/jobs/')&&!options.method)return json(jobs.get(route.split('/').pop()));
  if(route==='/api/bootstrap/read'||route==='/api/bootstrap/plan'||route.endsWith('/adaptive')||route==='/api/bootstrap/create'||route.endsWith('/revise')||route.endsWith('/augment')){
    if(mode==='failure')return json({id:'failed',status:'failed',stage:failureStage,error:'DO NOT SHOW raw exception secret'},202);
    let result=route.endsWith('/read')?{reader:{items:[],profile:{name:{value:'ユキ',quote:'名前はユキ'},world:{era:{value:'現代',quote:'現代'},region:{value:'北の国',quote:'北の国'},being:null,reality:null,contact:null}},voice:{first_person:{value:'私',quote:'一人称は私'},second_person_default:{value:'きみ',quote:'きみと呼ぶ'},speech_register:{value:'やわらかい口調',quote:'やわらかい口調'},length_default:null}}}:route.endsWith('/plan')?plan:route.endsWith('/adaptive')?{questions:[],parents:{}}:(route.endsWith('/create')||route.endsWith('/revise'))?{name:'ユキ',introduction:'静かな人。',open_domains:[{id:'R01',label:'internal-label',description:'人に頼られた時のこと。'}],open_past:[],open_now:[],seed_memories:[{content:'本を読んだ。'}],author_review:[{ref:'now:like:0',supplemented:'窓辺で読む習慣',based_on:'読書が好きという設定'}]}:{domains:['R01']};
    if(mode==='fb1'&&route.endsWith('/plan'))result={...plan,confirmation_required:body.now.life.L1now?.answer.includes('A')&&!body.conflict_resolutions.length?[{topic:'仕事',quote:'働かずに過ごす',question_ref:'now:L1now',answer_keys:['A'],question:'普段は何をして過ごしている？',choices:['ゆっくり聞く']}]:[]};
    if(mode==='fb2_cleared'&&route.endsWith('/plan')&&!calls.some(c=>c.route==='/api/bootstrap/plan'&&c!==calls.at(-1)))result={...plan,reader:{items:[],setting_conflicts:[{setting:'persona.ai_question',topic:'Answer',quote:'AI',detail:'Review this.'}]}};
    if(mode==='fb2'&&route.endsWith('/plan'))result={...plan,reader:{items:[],setting_conflicts:body.persona.ai_question==='honest'?[{setting:'persona.ai_question',topic:'答え方',quote:'人間だと答える',detail:'正直に答える指定と、人として答える設定文が食い違います。'}]:[]}};
    if(mode==='conflicts'&&route.endsWith('/plan')){
      const required=body.answers.R1?.answer.includes('A')&&!body.conflict_resolutions?.length;
      result={...plan,confirmation_required:required?[{topic:'距離感',quote:'少し離れて見守る',question_ref:'author:R1',answer_keys:['A'],question:q.scene,choices:['ゆっくり聞く']},{topic:'声のかけ方',quote:'声をかけずに待つ',question_ref:'author:R1',answer_keys:['A'],question:q.scene,choices:['ゆっくり聞く']}]:[]};
    }
    if(['layout','conflicts'].includes(mode)&&route.endsWith('/adaptive'))result={questions:[1,2].map(i=>({id:`AQ${i}`,domain:'R01',parent_question:'R1',added_condition:'急いでいる時',scene:'急いでいる時は？',choices:{A:'話す',B:'待つ',C:'聞く',D:'休む',E:'離れる'},multi:false})),parents:{R1:{title:'疲れた時',choices:['ゆっくり聞く']}}};
    if(mode==='confirmation'&&(route.endsWith('/create')||route.endsWith('/revise'))){
      result.open_domains=Array.from({length:5},()=>({description:'危ない場面でどう動くか'}));
      result.speaking_style={first_person:'僕',second_person:{default:'呼ばない',when_needed:'あなた'},register:'丁寧',length:{default:'二文',expand_when:'感動した時'},sentence_endings:[]};
      result.setting_conflicts=[{setting:'persona.ai_question',topic:'答え方',quote:'架空の設定文',detail:'AI なのか聞かれた時の答え方を確認してください。'}];
    }
    const id=route.endsWith('/create')?'draft':`job${++jobNumber}`,j={id,status:'done',result};jobs.set(id,j);return json(mode==='progress'?{id,status:'running',stage:'reading',progress:'読み取りを実行中です。',progress_percent:10}:j,202);
  }
  if(route==='/api/sessions')return json(options.method?{id:'session'}:[{id:'session'}]);
  if(route.endsWith('/turns'))return json(turns);
  if(route==='/api/proactive/opening'){if(mode.startsWith('sp1_opening'))await openingGate;if(mode==='sp1_opening_error')throw new Error('late opening failure');const reply={speak:true,turn_id:2,assistant_text:'はじめまして、ユキです。',assistant_at:at,expression:'shy'};turns.push({...reply,id:2,created_at:at,status:'completed',kind:'proactive',user_text:''});return json(reply);}
  if(route.endsWith('/messages/stream'))return new Response(new ReadableStream({async start(controller){const encode=new TextEncoder();controller.enqueue(encode.encode('event: stage\ndata: {"stage":"thinking"}\n\nevent: delta\ndata: {"text":"途中の言葉"}\n\n'));await streamGate;controller.enqueue(encode.encode('event: retract\ndata: {}\n\nevent: delta\ndata: {"text":"整えた言葉"}\n\nevent: reply\ndata: {"turn_id":3,"assistant_text":"最終の返答","assistant_at":"2026-10-14T12:01:00+09:00","expression":"'+(mode==='late_expression'?'thinking':'smile')+'","expression_pending":'+(mode==='late_expression')+'}\n\n'));if(mode==='late_expression'){await expressionGate;controller.enqueue(encode.encode('event: expression\ndata: {"turn_id":3,"expression":"smile"}\n\n'));}if(mode==='late_expression')controller.enqueue(encode.encode('event: error\ndata: {"message":"old stream failure"}\n\n'));controller.close();}}),{headers:{'Content-Type':'text/event-stream'}});
  if(route==='/api/note')return json({name:'ユキ',today_haru:mode==='m8c_note'?'少しほっとしている。':null,now_summary:null,books:mode==='m8b_note'?[{title:'銀河鉄道の夜',author:'宮沢賢治',read_count:2,chunk_count:7,finished_at:null}]:[],interests:mode==='note_interests'?[{topic:'読書',detail:'静かな本が好き'},{topic:'散歩',detail:'散歩'}]:[],activities:mode==='note'?[{title:'**読書**',summary:'# 発見\n- **静かな時間**\n1. *良い本*\n<img src=x onerror=alert(1)>',impression:'静かな本が気に入ったようだ。',date:'2026-10-14',sources:[{title:'書評'}]}]:[],past:[],has_undisclosed_past:true});
  throw new Error(`Unexpected fake endpoint: ${route}`);
};
const tick=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));if(document.querySelector('#screen h2')?.textContent==='パートナーについて'){await document.querySelector('#screen .primary').onclick();for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));}};
const click=async text=>{const button=[...document.querySelectorAll('button')].find(e=>e.textContent===text);assert(button,`Missing button: ${text}`);await button.onclick?.();await tick();};
const input=(label,value)=>{const l=[...document.querySelectorAll('label')].find(e=>e.firstChild?.textContent===label);assert(l,`Missing label: ${label}`);const e=l.control||l.querySelector('input,select,textarea');e.value=value;e.dispatchEvent(new dom.window.Event('input'));return e;};
const canvasDraws=[];
let failureStage='reading';
dom.window.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};dom.window.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
dom.window.HTMLCanvasElement.prototype.getContext=function(){return {clearRect(){},drawImage(...args){canvasDraws.push(args);}};};
dom.window.HTMLCanvasElement.prototype.toDataURL=()=> 'data:image/png;base64,cG5n';
dom.window.HTMLCanvasElement.prototype.toBlob=function(callback){callback(new Blob(['png'],{type:'image/png'}));};
globalThis.createImageBitmap=async()=>({width:2048,height:1024});
if(mode==='m8c_inner'){
  const source=readFileSync(new URL('../cbs/web/static/app.js',import.meta.url),'utf8');
  const body=source.slice(source.indexOf('async function loadInner()'),source.indexOf("$('inner-tab').addEventListener"));
  const element=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const data={feelings:[{created_at:old,ref:'inner:1',sentence:'気になってる。',trigger_ref:'memory_job:1',levels:{energy:'ふつう',social:'高い',concern:'少し'}}],
    wants:[{ref:'thread:1',history:[{created_at:old,operation:'inner_update',after:{topic:'面接',status:'open',direction:'気にかける',motive:'緊張していたから',trigger_ref:'memory_job:1'}}]}],
    monologues:[{created_at:old,text:'ゆっくり休めてるといいな。',trigger_ref:'activity:1'}],
    decisions:[{created_at:at,speak:false,want_ref:'thread:1',why_now:'今は休んでいそうだから。',reason:'休息を尊重する'}],
    updates:[{created_at:old,status:'completed',trigger_ref:'memory_job:1',trace:{request:{messages:[{content:JSON.stringify({event:{kind:'conversation',summary:{episodes:[{summary:'面接前に緊張していた。'}]}}})}]}}}]};
  const load=new Function('api','element','$',body+';return loadInner;')(async()=>data,element,id=>document.getElementById(id));await load();
  const text=document.getElementById('inner-panel').textContent;
  for(const expected of ['内面','面接前に緊張していた。','緊張していたから','ゆっくり休めてるといいな。','今は休んでいそうだから。','thread:1','memory_job:1'])assert(text.includes(expected));
  assert(text.indexOf('なぜ今')<text.indexOf('気になってる。'));
}else if(mode==='m8b_settings'){
  location.hash='#settings/pace';const {selectPartner}=await import('../cbs/web/static/ui.js');selectPartner('test_partner');
  const {settingsScreen}=await import('../cbs/web/static/settings.js');await settingsScreen(document.getElementById('screen'),'ユキ');
  assert(document.body.textContent.includes('よく読むサイト'));assert(document.body.textContent.includes('robots.txt'));assert.equal(document.body.textContent.split('このページからは新しい記事を見つけられません').length-1,1);
  for(const [title,value,tool,key]of [['サイトの記事','4','read_source','daily_max'],['本','1','read_book','daily_max'],['動画（本）','2','watch_video','daily_max'],['動画1本の長さ（分）','10','watch_video','max_minutes'],['世界の中で過ごす','4','world_activity','daily_max'],['Wikipedia','5','read_wikipedia','daily_max'],['買い物の検索','2','browse_shopping','daily_max']]){
    input(title,value);const e=[...document.querySelectorAll('label')].find(l=>l.firstChild?.textContent===title).querySelector('input');e.dispatchEvent(new window.Event('blur'));await tick();assert.equal(calls.filter(c=>c.route==='/api/settings'&&c.method==='PUT').at(-1).body.autonomy.tools[tool][key],Number(value));
  }
  input('サイトの URL','https://reader.example/new');await click('サイトを追加する');assert(document.body.textContent.includes('新しいサイト'));assert.equal([...document.querySelectorAll('button')].filter(b=>b.textContent==='もう一度確かめる').length,1);await click('もう一度確かめる');await click('外す');assert(calls.some(c=>c.method==='DELETE'));
}else if(mode==='m8c_note'){
  location.hash='#note';await import('../cbs/web/static/user.js');await tick();
  assert(document.body.textContent.includes('今日のユキ'));assert(document.body.textContent.includes('少しほっとしている。'));
  assert(!document.body.textContent.includes('元気さ'));assert(!document.querySelector('meter'));
}else if(mode==='m8b_note'){
  location.hash='#note';await import('../cbs/web/static/user.js');await tick();
  assert(document.body.textContent.includes('読んでいる本'));
  assert(document.body.textContent.includes('銀河鉄道の夜 / 宮沢賢治'));
  assert(document.body.textContent.includes('2 / 7 区切り'));
}else if(mode==='k2_choices'){
  location.hash='#settings/ai';const {settingsScreen}=await import('../cbs/web/static/settings.js');await settingsScreen(document.getElementById('screen'),'ユキ');
  assert(document.querySelector('details').textContent.includes('検証で問題あり'));assert(!document.querySelector('details').open);assert(!document.querySelector('option[value="openrouter/failed/model"]'));
  const model=input('会話のモデル','custom');const e=[...document.querySelectorAll('label')].find(l=>l.firstChild?.textContent==='会話のモデル').querySelector('select');e.dispatchEvent(new window.Event('change'));input('一覧にないモデル名','vendor/custom-model');const custom=[...document.querySelectorAll('label')].find(l=>l.firstChild?.textContent==='一覧にないモデル名').querySelector('input');custom.dispatchEvent(new window.Event('blur'));await tick();
  const saved=calls.find(c=>c.route==='/api/settings'&&c.method==='PUT');assert.deepEqual(saved.body,{simple:{actor:'openrouter/vendor/custom-model'}});assert(document.querySelector('a[href="/wizard?ai=1"]'));
}else if(mode==='k2_simple'||mode==='k2_callback'){
  location.hash='#settings/ai';const {settingsScreen,embeddingIdentity}=await import('../cbs/web/static/settings.js');await settingsScreen(document.getElementById('screen'),'');assert.equal(embeddingIdentity(cfg.models.embedding),'gemini/gemini-embedding-2');assert(document.querySelector('a[href="/wizard?ai=1"]'));assert(!document.querySelector('input[type=password]'));assert(!document.body.textContent.includes('API キー'));if(mode==='k2_callback')assert(document.body.textContent.includes('つながっています'));assert(!calls.some(c=>c.route==='/api/settings'&&c.body));
}else if(mode==='k2_gate'){
  await import(mode==='k2_wizard'?'../cbs/web/static/wizard.js':'../cbs/web/static/user.js');await tick();
  assert(document.body.textContent.includes('まず、使う AI につなぎます'));
  assert([...document.querySelectorAll('button')].some(e=>e.textContent.startsWith('OpenRouter で')));
  assert(!calls.some(c=>c.route==='/api/bootstrap/questionnaire'||c.route==='/api/proactive/opening'||c.route==='/api/sessions'));
}else if(mode==='settings'||mode==='settings_empty'){
  location.hash='#settings/relationship';const {selectPartner,shell}=await import('../cbs/web/static/ui.js');selectPartner('test_partner');shell('settings','ユキ');const {settingsScreen}=await import('../cbs/web/static/settings.js');await settingsScreen(document.getElementById('screen'),'ユキ');
  assert.deepEqual([...document.querySelectorAll('.settings-shelves button')].map(e=>e.textContent),['いまの設定','話し方と関係','話しかけと過ごし方','見た目と声','画面と入力','AI と費用','記録とデータ']);assert.equal(!!document.querySelector('a[href*="augment="]'),mode!=='settings_empty');assert(document.getElementById('partner-requests').textContent.includes('「AI なの？」への答え方'));
  input('「AI なの？」への答え方','honest');input('あなたの名前（任意）','葵');await click('話し方とお願いを保存');const saved=calls.find(c=>c.route==='/api/persona'&&c.method==='PUT');assert.equal(saved.body.ai_question,'honest');assert(saved.path.includes('character_id=test_partner'));
  await click('画面と入力');const split=[...document.querySelectorAll('.switch')].find(l=>l.textContent==='返答を分けて出す').querySelector('input');split.checked=false;split.dispatchEvent(new window.Event('change'));await tick();assert.deepEqual(calls.filter(c=>c.route==='/api/settings'&&c.method==='PUT').at(-1).body,{display:{split_replies:false}});
  await click('記録とデータ');const dev=[...document.querySelectorAll('.switch')].find(l=>l.textContent==='開発者画面へのリンクを表示する').querySelector('input');dev.checked=true;dev.dispatchEvent(new window.Event('change'));await tick();assert.equal(localStorage.getItem('cbs.dev'),'true');
}else if(mode==='augment'){
  await import('../cbs/web/static/wizard.js');await tick();
  assert.equal(document.querySelectorAll('fieldset').length,1);assert(document.querySelector('input[value=leave_open]').checked);
  await click('残りは全部決めないでおく');await click('深掘りを飛ばす');
  assert(document.body.textContent.includes('追加しました'));const call=calls.find(c=>c.route.endsWith('/augment')&&c.method==='POST');assert.deepEqual(call.body.answers.R1.answer,['leave_open']);
}else if(mode.startsWith('sp1_opening')){
  await import('../cbs/web/static/user.js');await tick();
  assert(calls.some(c=>c.route==='/api/proactive/opening'));
  assert(!document.getElementById('message').disabled,'opening leaves composer usable');
  assert.equal(calls.find(c=>c.route==='/api/proactive/opening').body.after_turn_id,0);
  input('メッセージ','user first');document.getElementById('composer').dispatchEvent(new dom.window.Event('submit',{cancelable:true}));await tick();
  assert(document.getElementById('message').disabled);
  releaseOpening();await tick();
  assert(document.getElementById('message').disabled,'stale opening finalizer cannot unlock the user turn');
  await new Promise(r=>setTimeout(r,5));assert.equal(document.querySelectorAll('.thinking').length,1);
  assert(!document.body.textContent.includes('はじめまして、ユキです。'));
  assert(document.getElementById('error').hidden);
  releaseStream();await tick();
  assert(!document.getElementById('message').disabled);
  assert(document.body.textContent.includes('最終の返答'));
  assert(!document.body.textContent.includes('はじめまして、ユキです。'));
}else if(mode==='chat'||mode==='first'||mode==='note'||mode==='note_interests'||mode==='late_expression'){
  await import('../cbs/web/static/user.js');await tick();assert.equal(document.getElementById('heading').textContent,'ユキと話す');assert(document.body.textContent.includes('ユキから'));assert(document.body.textContent.includes('はじめまして、ユキです。'));
  assert.equal(document.documentElement.dataset.accent,'evergreen');
  if(mode==='first')assert.equal(document.querySelectorAll('.hint').length,1);
  else if(mode==='chat'||mode==='late_expression'){
    if(mode==='chat')assert(document.body.textContent.includes('3日ぶりの会話'));
    input('メッセージ','こんにちは');document.getElementById('composer').dispatchEvent(new dom.window.Event('submit',{cancelable:true}));await tick();assert(!document.body.textContent.includes('途中の言葉'));assert(document.getElementById('send').disabled);
    releaseStream();await tick();assert(document.body.textContent.includes('最終の返答'));assert(!document.body.textContent.includes('途中の言葉'));assert.equal(document.querySelectorAll('.thinking').length,0);
    if(mode==='late_expression'){
      assert.equal(document.getElementById('message').disabled,false,'reply immediately restores input while expression stream is open');
      const latency=JSON.parse(window.sessionStorage.getItem('cbs.latency.3'));
      assert(latency.input_enabled_ms>=latency.reply_shown_ms);
      assert(document.querySelector('[data-turn-id="3"] img').src.includes('/thinking'));
      const originalFetch=globalThis.fetch;
      let releaseSecond;
      const secondGate=new Promise(resolve=>releaseSecond=resolve);
      globalThis.fetch=async(path,options={})=>path.split('?')[0].endsWith('/messages/stream')
        ? new Response(new ReadableStream({async start(controller){await secondGate;controller.enqueue(new TextEncoder().encode('event: reply\ndata: {"turn_id":4,"assistant_text":"Second checked reply","assistant_at":"2026-10-14T12:02:00+09:00","expression":"neutral"}\n\n'));controller.close();}}))
        : originalFetch(path,options);
      input('メッセージ','Second message');
      document.getElementById('composer').dispatchEvent(new dom.window.Event('submit',{cancelable:true}));await tick();
      assert(document.getElementById('message').disabled);
      releaseExpression();await tick();
      assert(document.getElementById('message').disabled,'old stream finalizer must leave the new request busy');
      await new Promise(r=>setTimeout(r,5));assert.equal(document.querySelectorAll('.thinking').length,1,'new pending reply survives old stream completion');
      assert(!calls.some(c=>c.route==='/api/turns/3/audio/stream'),'late expression must not restart an older reply after the next send');
      assert(document.getElementById('error').hidden,'stale stream errors must not replace the current request status');
      releaseSecond();await tick();
      assert(!document.getElementById('message').disabled);
      assert(document.querySelector('[data-turn-id="3"] img').src.includes('/smile'));
      globalThis.fetch=originalFetch;
      location.hash='#note';window.dispatchEvent(new dom.window.HashChangeEvent('hashchange'));await tick();
      releaseExpression();await tick();
      location.hash='#talk';window.dispatchEvent(new dom.window.HashChangeEvent('hashchange'));await tick();
      assert(document.querySelector('[data-turn-id="3"] img').src.includes('/smile'));assert.equal(document.querySelectorAll('[data-turn-id="3"]').length,1);
    }
    assert(!document.getElementById('send').disabled);
  }
  location.hash='#note';window.dispatchEvent(new dom.window.HashChangeEvent('hashchange'));await tick();
  if(mode==='note_interests'){const items=document.querySelector('.interest-list');assert.equal(items.children.length,2);assert.equal(items.querySelectorAll('h3').length,0);assert.equal(items.querySelectorAll('li>span').length,2);assert.equal(items.querySelectorAll('li>p.small').length,1);}
  else if(mode==='note'){assert(document.body.textContent.includes('発見\n静かな時間\n良い本'));assert(document.body.textContent.includes('10月14日'));assert(document.body.textContent.includes('静かな本が気に入ったようだ。'));assert(document.body.textContent.includes('読んだもの: 書評'));assert(!document.body.textContent.includes('**'));assert(!document.querySelector('#screen img'));assert(document.body.textContent.includes('<img src=x onerror=alert(1)>'));}
  else assert(document.body.textContent.includes('まだ、白いページ'),document.body.textContent);
}else if(mode==='look_editor'){
  const {newLook,mountLook,saveLook}=await import('../cbs/web/static/look-editor.js');
  const root=document.getElementById('screen'),state=newLook({features:'旧形式の補足'});
  await mountLook(root,state,{world:{being:'精霊',preset:'和風ファンタジー'}});await tick();
  let copied='',resetCopy;Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async text=>{copied=text;}}}});
  const realTimeout=globalThis.setTimeout;globalThis.setTimeout=(fn,ms)=>{assert.equal(ms,2000);resetCopy=fn;};
  try{await click('コピー');}finally{globalThis.setTimeout=realTimeout;}
  assert.equal(copied,'APIが作った4列2行のプロンプト');assert([...document.querySelectorAll('button')].some(b=>b.textContent==='コピーしました'&&b.querySelector('svg')));resetCopy();assert([...document.querySelectorAll('button')].some(b=>b.textContent==='コピー'));
  navigator.clipboard.writeText=async()=>{throw new Error('blocked');};await click('コピー');assert(document.body.textContent.includes('本文を選択してコピーしてください'));assert(window.getSelection().toString().includes('APIが作った'));
  assert.equal(document.querySelector('.look-tabs[aria-label="見た目の項目"]').querySelectorAll('[role=tab]').length,5);
  assert.equal(document.querySelectorAll('[role=tabpanel]:not([hidden])').length,2);
  assert.equal(document.querySelector('[data-look-path="person.profile.age"]').value,'');
  assert(document.querySelector('[data-look-path="person.profile.age"]').placeholder.includes('40代'));
  assert(!document.body.textContent.includes('プロンプトを作る'));
  await click('人物 ●');assert(document.getElementById('look-panel-profile').querySelector('details').open);
  input('髪の色','手で入力');input('長さ','消す');input('長さ','');
  await tick();assert(calls.filter(c=>c.route==='/api/look/prompt').length>=4);
  await click('設定文と性格から下書きする');
  assert.equal(state.person.hair.color,'手で入力');assert.equal(state.person.hair.length,'','cleared manual field stays empty');
  assert.equal(state.person.face.marks,'そばかす');assert(document.getElementById('look-panel-face').querySelector('details').open);
  assert(document.body.textContent.includes('引用や欄を確認できない'));
  assert(document.body.textContent.includes('性格からの提案: 穏やかな微笑み'));
  assert.equal(state.person.profile.vibe,'');await click('使う');assert.equal(state.person.profile.vibe,'穏やかな微笑み');
  assert(document.body.textContent.includes('そばかす'));
  const group=[...document.querySelectorAll('details')].find(d=>d.querySelector('summary')?.textContent==='8つの表情の描き分け');
  assert(group&&!group.open&&!group.hidden);assert.equal(group.querySelectorAll('input').length,8);
  input('悲しい','手書きの悲しみ');input('すねる','消す');input('すねる','');
  await click('性格から提案');
  assert(group.open);assert.equal(state.person.expressions.smile,'');assert.equal(state.person.face.habits,'');
  assert(document.body.textContent.includes('感情は目元で示す'));
  const smileNotes=document.querySelector('[data-look-path="person.expressions.smile"]').parentElement.nextElementSibling;
  await smileNotes.querySelector('button').onclick();await tick();
  assert.equal(state.person.expressions.smile,'固有の描写:smile');
  assert(calls.filter(c=>c.route==='/api/look/prompt').at(-1).body.person.expressions.smile);
  input('何に使う画像か','single');assert(group.hidden);input('何に使う画像か','sheet');assert(!group.hidden);
  await click('全部使う');
  assert.equal(state.person.face.habits,'目元に気持ちが出る');assert.equal(state.person.expressions.thinking,'固有の描写:thinking');
  assert.equal(state.person.expressions.sad,'手書きの悲しみ');assert.equal(state.person.expressions.pout,'');
  const beforeFailure=structuredClone(state.person),pending=structuredClone(state.suggestions),normalFetch=globalThis.fetch;
  globalThis.fetch=async(path,options)=>path.startsWith('/api/look/expression-suggestions')?json({detail:'failed'},502):normalFetch(path,options);
  await click('性格から提案');assert.deepEqual(state.person,beforeFailure);assert.deepEqual(state.suggestions,pending);
  globalThis.fetch=normalFetch;
  const d=document.getElementById('look-panel-style').querySelector('details');d.open=true;d.ontoggle();
  root.replaceChildren();await mountLook(root,state);assert(document.getElementById('look-panel-style').querySelector('details').open);
  assert([...document.querySelectorAll('details')].find(d=>d.querySelector('summary')?.textContent==='8つの表情の描き分け').open);
  const file=document.querySelector('input[type=file]');Object.defineProperty(file,'files',{value:[{type:'image/png',size:1000}]});await file.onchange();await tick();
  const reference=document.querySelector('[data-look-path="image.from_reference"]');assert(!reference.parentElement.hidden);reference.checked=true;reference.onchange();await tick();
  assert(!document.querySelector('.look-reference').hidden);assert(document.querySelector('a[download="neutral.png"]').href.startsWith('data:image/png'));
  input('何に使う画像か','single');await tick();assert.equal(calls.filter(c=>c.route==='/api/look/prompt').at(-1).body.image.use,'single');
  assert(calls.filter(c=>c.route==='/api/look/prompt').at(-1).body.image.from_reference);
  await click('画像を外す');assert(!state.image.from_reference);assert(reference.parentElement.hidden);
  input('特徴の補足','');await saveLook(state);assert.equal(calls.find(c=>c.route==='/api/look/meta').body.features,'');
  assert.equal(calls.find(c=>c.route==='/api/look/meta').body.person.hair.color,'手で入力');
  root.replaceChildren();await mountLook(root,newLook(),{expressionPath:'/api/bootstrap/jobs/draft/look-expressions'});
  await click('性格から提案');assert.equal(calls.at(-1).route,'/api/bootstrap/jobs/draft/look-expressions');
}else if(mode==='canvas'){
  const {newLook,mountLook,saveLook}=await import('../cbs/web/static/look-editor.js');const state=newLook();await mountLook(document.getElementById('screen'),state);
  const file=[...document.querySelectorAll('input[type=file]')][0];Object.defineProperty(file,'files',{value:[{type:'image/png',size:1000}]});await file.onchange();await tick();assert.equal(state.slots.filter(Boolean).length,8);
  assert.deepEqual(state.slots.map(s=>[s.sx,s.sy,s.w,s.h]),[[0,0,512,512],[512,0,512,512],[1024,0,512,512],[1536,0,512,512],[0,512,512,512],[512,512,512,512],[1024,512,512,512],[1536,512,512,512]]);
  input('長さ','長い髪');await saveLook(state);assert.equal(uploaded,8);assert.equal(state.person.hair.length,'長い髪');assert(canvasDraws.length>=16);
  assert(!calls.find(c=>c.route==='/api/look/meta').body.accent);
}
if(['settings','settings_empty'].includes(mode))assert.deepEqual(calls.find(c=>c.route==='/api/settings'&&c.method==='PUT').body,{display:{split_replies:false}});
console.log(`PASS offline DOM: ${mode}`);
