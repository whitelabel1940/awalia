// Share images, drawn on a canvas only from text the user sees in the preview.
// Always the light paper of the app, with the user's accent, so a timeline shows one look.
const ACCENTS={mauve:'#835c75',madder:'#9c534d',evergreen:'#546e61',charcoal:'#595a61'};
const PAPER='#fbf7f2',PANEL='#f3ece6',INK='#2b2725',MUTED='#6b6169',LINE='#e8ded7',NIGHT='20,19,22';
const SERIF='"Noto Serif JP","Yu Mincho",serif',SANS='"Noto Sans JP","Yu Gothic UI",system-ui,sans-serif';
// Japanese line breaking: these never start a line (they hang instead) / never end one.
const NO_START='、。，．・：；？！?!)]}）］｝」』】〉》〕ー～…‥ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ々';
const NO_END='(（[［{｛「『【〈《〔';
const WEEK='日月火水木金土';

export function accentColor(){return ACCENTS[document.documentElement.dataset.accent]||ACCENTS.mauve;}

export function dayLabel(day,{week=true}={}){
  const [y,m,d]=String(day).split('-').map(Number),w=WEEK[new Date(y,m-1,d).getDay()];
  return `${y}年${m}月${d}日`+(week?`（${w}）`:'');
}

export function wrap(ctx,text,width){
  const lines=[];
  for(const para of String(text||'').split('\n')){
    let line='';
    // Latin words and numbers stay whole; everything else may break between characters.
    for(const t of para.match(/[A-Za-z0-9][A-Za-z0-9'’.,:/-]*|\s+|./gsu)||[]){
      if(!line&&/^\s+$/.test(t))continue;
      if(line&&ctx.measureText(line+t).width>width){
        if(NO_START.includes(t[0])){line+=t;continue;}
        let carry='';
        while(line.length>1&&NO_END.includes(line.at(-1))){carry=line.at(-1)+carry;line=line.slice(0,-1);}
        lines.push(line.trimEnd());line=(carry+t).trimStart();
      }else line+=t;
    }
    lines.push(line);
  }
  return lines;
}

function image(src){
  return new Promise(resolve=>{if(!src)return resolve(null);const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>resolve(null);img.src=src;});
}

async function fonts(specs){
  // Web fonts arrive in unicode-range slices; ask for the exact text so every glyph is ready.
  if(document.fonts)await Promise.all(specs.map(([font,text])=>document.fonts.load(font,text).catch(()=>null)));
}

function tinted(img,color,height){
  const width=Math.round(height*img.width/img.height),c=document.createElement('canvas');c.width=width;c.height=height;
  const x=c.getContext('2d');x.drawImage(img,0,0,width,height);x.globalCompositeOperation='source-in';x.fillStyle=color;x.fillRect(0,0,width,height);
  return c;
}

function cover(ctx,img,x,y,w,h){
  const s=Math.max(w/img.width,h/img.height),sw=w/s,sh=h/s;
  ctx.drawImage(img,(img.width-sw)/2,(img.height-sh)/2,sw,sh,x,y,w,h);
}

function circle(ctx,img,cx,cy,r,{shadow=false}={}){
  ctx.save();
  if(shadow){ctx.shadowColor='rgba(79,67,88,.22)';ctx.shadowBlur=48;ctx.shadowOffsetY=16;ctx.fillStyle=PANEL;ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.fill();ctx.shadowColor='transparent';}
  ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.clip();cover(ctx,img,cx-r,cy-r,r*2,r*2);ctx.restore();
}

// The symbol plus the word set in the page's own serif: an SVG drawn as an image cannot
// load web fonts, so the wordmark inside horizontal.svg comes out thin and small.
function brand(ctx,symbol,x,baseline,{align='left',color='#4f4358',size=40}={}){
  ctx.save();ctx.font=`500 ${size}px ${SERIF}`;ctx.fillStyle=color;
  const word=ctx.measureText('Awalia').width,h=size*1.3,w=symbol?h*symbol.width/symbol.height:0,gap=symbol?size*.4:0;
  const left=align==='right'?x-word-gap-w:x;
  if(symbol)ctx.drawImage(symbol,left,baseline-h*.82,w,h);
  ctx.textAlign='left';ctx.fillText('Awalia',left+w+gap,baseline);ctx.restore();
}

function roundRect(ctx,x,y,w,h,r){ctx.beginPath();ctx.roundRect?ctx.roundRect(x,y,w,h,r):ctx.rect(x,y,w,h);}

function fit(ctx,text,font,size,min,width){
  let s=size;for(;s>min;s-=2){ctx.font=font(s);if(ctx.measureText(text).width<=width)break;}
  ctx.font=font(s);return s;
}

// A page of the diary: 4:5 so timelines show it whole. Long entries shrink the type a little,
// then lengthen the page (up to 9:16) rather than cut words.
export async function diaryCard({day,summary,name,partnerComment='',userComment='',faceUrl=null,accent=accentColor()}){
  const W=1080,M=96,inner=W-M*2,date=dayLabel(day),from=`${name}より`;
  await fonts([[`500 30px ${SANS}`,date+from+'あなたより'],[`500 54px ${SERIF}`,'ふたりの日記Awalia'+summary+partnerComment+userComment]]);
  const [symbol,face]=await Promise.all([image('/static/brand/symbol.svg'),image(faceUrl)]);

  function compose(ctx,s,paint){
    const text=(str,x,y,font,color,align='left')=>{if(!paint)return;ctx.font=font;ctx.fillStyle=color;ctx.textAlign=align;ctx.fillText(str,x,y);};
    const para=(str,x,y,width,font,size,lead,color)=>{ctx.font=font;const lines=wrap(ctx,str,width);lines.forEach((l,i)=>text(l,x,y+size+i*size*lead,font,color));return lines.length*size*lead;};
    ctx.textBaseline='alphabetic';
    let y=M;
    text(date,M,y+30,`500 30px ${SANS}`,MUTED);
    text('ふたりの日記',W-M,y+30,`500 30px ${SERIF}`,accent,'right');
    y+=30+44;
    if(paint){ctx.fillStyle=LINE;ctx.fillRect(M,y,inner,2);}
    y+=2+64*s;
    y+=para(summary,M,y,inner,`500 ${54*s}px ${SERIF}`,54*s,1.85,INK);
    // Both one-line comments share one form, so they read as replies of the same rank.
    const comment=(label,body,avatarImage)=>{
      const pad=52*s,size=42*s,avatar=avatarImage?84*s:0;
      ctx.font=`500 ${size}px ${SERIF}`;
      const lines=wrap(ctx,body,inner-pad*2),height=lines.length*size*1.85,head=Math.max(avatar,30);
      const h=pad+head+20*s+height+pad*.7;
      if(paint){ctx.fillStyle=PANEL;roundRect(ctx,M,y,inner,h,28);ctx.fill();}
      let x=M+pad;
      if(avatarImage&&paint)circle(ctx,avatarImage,x+avatar/2,y+pad+head/2,avatar/2);
      if(avatarImage)x+=avatar+20;
      text(label,x,y+pad+head/2+10,`500 28px ${SANS}`,MUTED);
      para(body,M+pad,y+pad+head+20*s,inner-pad*2,`500 ${size}px ${SERIF}`,size,1.85,INK);
      y+=h;
    };
    if(partnerComment){y+=64*s;comment(from,partnerComment,face);}
    if(userComment){y+=(partnerComment?28:64)*s;comment('あなたより',userComment,null);}
    return y+96+72+M;
  }

  const probe=document.createElement('canvas').getContext('2d');
  let scale=1;
  for(const s of [1,.92,.84,.76,.68,.6]){scale=s;if(compose(probe,s,false)<=1350)break;}
  const H=Math.min(1920,Math.max(1350,Math.ceil(compose(probe,scale,false))));
  const canvas=document.createElement('canvas');canvas.width=W;canvas.height=H;
  const ctx=canvas.getContext('2d');
  ctx.fillStyle=PAPER;ctx.fillRect(0,0,W,H);
  compose(ctx,scale,true);
  brand(ctx,symbol,M,H-M,{size:36});
  return canvas;
}

// The first meeting: 16:9, the cover art when there is one, otherwise the face or the brand ribbon.
export async function startCard({name,day,coverUrl=null,faceUrl=null,accent=accentColor()}){
  const W=1600,H=900,M=88,line='と話しはじめました',date=dayLabel(day,{week:false});
  await fonts([[`600 112px ${SERIF}`,name+'Awalia'],[`500 48px ${SERIF}`,line+'Awalia'],[`400 28px ${SANS}`,date]]);
  const [art,face,ribbon,symbol]=await Promise.all([image(coverUrl),coverUrl?null:image(faceUrl),image('/static/brand/ribbon.svg'),image('/static/brand/symbol.svg')]);
  const canvas=document.createElement('canvas');canvas.width=W;canvas.height=H;
  const ctx=canvas.getContext('2d');ctx.textBaseline='alphabetic';

  if(art){
    cover(ctx,art,0,0,W,H);
    const g=ctx.createLinearGradient(0,H*.38,0,H);
    g.addColorStop(0,`rgba(${NIGHT},0)`);g.addColorStop(.55,`rgba(${NIGHT},.5)`);g.addColorStop(1,`rgba(${NIGHT},.82)`);
    ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
    ctx.fillStyle='rgba(255,255,255,.82)';ctx.font=`400 28px ${SANS}`;ctx.fillText(date,M,H-M);
    ctx.fillStyle='#fff';ctx.font=`500 48px ${SERIF}`;ctx.fillText(line,M,H-M-28-36);
    fit(ctx,name,s=>`600 ${s}px ${SERIF}`,112,64,W-M*2-360);ctx.fillText(name,M-4,H-M-28-36-48-28);
    if(ribbon){
      const mark=tinted(ribbon,'#fff',52);ctx.globalAlpha=.92;
      ctx.font=`500 40px ${SERIF}`;ctx.textAlign='right';ctx.fillText('Awalia',W-M,H-M);
      const word=ctx.measureText('Awalia').width;ctx.drawImage(mark,W-M-word-16-mark.width,H-M-44);
      ctx.globalAlpha=1;ctx.textAlign='left';
    }
    return canvas;
  }

  ctx.fillStyle=PAPER;ctx.fillRect(0,0,W,H);
  const r=200,cx=M+40+r,cy=H/2-8;
  if(face)circle(ctx,face,cx,cy,r,{shadow:true});
  else if(symbol){const w=r*2.3,h=w*symbol.height/symbol.width;ctx.drawImage(symbol,cx-w/2,cy-h/2,w,h);}
  const x=cx+r+96,width=W-x-M;
  const size=fit(ctx,name,s=>`600 ${s}px ${SERIF}`,112,64,width);
  const block=size+28+48+40+28,top=cy-block/2;
  ctx.fillStyle=INK;ctx.fillText(name,x-4,top+size);
  ctx.font=`500 48px ${SERIF}`;ctx.fillText(line,x,top+size+28+48);
  ctx.fillStyle=accent;ctx.fillRect(x,top+size+28+48+40-14,56,2);
  ctx.fillStyle=MUTED;ctx.font=`400 28px ${SANS}`;ctx.fillText(date,x,top+block+14);
  // With the brand symbol already large on the left, the corner logo would only repeat it.
  if(face)brand(ctx,symbol,W-M,H-M,{align:'right'});
  return canvas;
}

export function pngBlob(canvas){
  return new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error('画像を作れませんでした。')),'image/png'));
}
