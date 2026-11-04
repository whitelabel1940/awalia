// Only wording and explicit input assignments live here. The Reader finds conflicts.
export const aiQuestionChoices={immersive:'人として答える',playful:'はっきりさせずにかわす',honest:'正直に答える'};
export function aiQuestionConflict(value,quote,imported=false){return `基本と世界で「${aiQuestionChoices[value]}」を選びましたが、${imported?'設定文や前の会話':'設定文'}には「${quote}」とあります。`;}
export const settingTemplates={
  source:{question:'設定文と前の会話、どちらに合わせる？',choices:{setting:'設定文に合わせる',conversation:'前の会話に合わせる'}},
  ai:{question:'「AI なの？」と聞かれたら、どう答える？',choices:aiQuestionChoices},
  appearance:{question:'この見た目を、どう扱う？',choices:{literal:'物語の中で本当にある',expression:'見た目の表現（耳飾りなど）'}},
  world:{question:'設定文の内容を、この世界でどう扱う？',choices:{literal:'この世界で実際にあることとして扱う',expression:'たとえや演出として扱う'}},
  answer:{question:'どちらの内容で迎える？',choices:{text:'設定文のとおり',answer:'質問の回答のとおり'}},
};

export const openStyleChoices=name=>[['hinted',`答えた性格をもとに、${name}らしく振る舞う`,'答えた性格に沿った反応になりやすく、思いがけない一面は少なめです。'],['free','性格に寄せすぎず、その場で決める','思いがけない一面が出やすい一方、答えた性格と違って見える反応が出ることもあります。']];
