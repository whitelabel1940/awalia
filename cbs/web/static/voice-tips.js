import {node} from './ui.js';

// Tips for recording reference audio (Irodori's guidance: 3-10 s clips, 3-8 of them, about 30 s in total).
const TIPS = [
  ['長さと本数', '1本3〜10秒の自然な文を、3〜8本用意します。合計30秒前後が目安です（上限は120秒）。長い1本より、短い文を数本のほうが声をつかみやすくなります。'],
  ['話し方', 'ふだん話す時の調子を中心にします。少し明るい文と、少し落ち着いた文を1〜2本ずつ混ぜると、声に幅が出ます。演技のように大げさにはしないでください。'],
  ['文の選び方', '会話らしい文にします。「〜よ」「〜ね」「〜かな」、問いかけなど、文の終わりを混ぜてください。1本ずつ、言い終えたところで区切ります。'],
  ['録音', '同じ場所・同じマイクで録ります。雑音や音楽、ほかの人の声が入らないようにします。前後の無音は短くして、WAV で保存してください。'],
  ['使ってよい声', '自分の声か、使ってよいと本人から許可をもらった声だけを使ってください。'],
];

// Everyday sentences to read as they are. Endings may be changed to how the speaker usually talks.
export const EXAMPLES = [
  ['ふだんの調子', [
    'あ、その書類ならさっき確認して、共有フォルダの「最新」ってところに保存しておいたよ。',
    '明日の午後、もし時間が空いてたらでいいんだけど、ちょっとだけ駅前のカフェで作戦会議しない？',
    '昨日は結局、冷蔵庫に残ってたキャベツと豚肉で、適当に炒め物を作って済ませちゃった。',
    'うーん、どっちにするか迷うなあ。とりあえず、それぞれのメリットをもう一回整理してみようか。',
    '駅から歩いて十数分くらいだったかな。ちょっと道が複雑だから、あとで地図のリンク送るね。',
  ]],
  ['少し明るく（感謝や解放感で、自然に少し声が上がるくらい）', [
    'えっ、本当にそれでいいの？ めちゃくちゃ助かる！ じゃあ、遠慮なくお願いしちゃおうかな。',
    'やっと週末だね！ 今週はけっこう頑張ったし、帰りに美味しいものでも食べて帰ろうよ。',
  ]],
  ['少し落ち着いて（相手をいたわるように、少しゆっくり）', [
    'そっか、それは残念だったね……。まあ、そういうタイミングもあるし、あまり気落ちしないでね。',
  ]],
];

async function copy(text, button) {
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = 'コピーしました';
  } catch {
    button.textContent = 'コピーできませんでした';
  }
  setTimeout(() => { button.textContent = label; }, 1500);
}

function copyButton(text, label = 'コピー') {
  const button = node('button', label, 'voice-copy');
  button.type = 'button';
  button.onclick = () => copy(text, button);
  return button;
}

function cell(tag, content, className) {
  const element = node(tag, undefined, className);
  element.append(content);
  return element;
}

export function voiceTips(parent) {
  const box = node('details', undefined, 'voice-tips');
  box.append(node('summary', '参照音声のコツと、そのまま読める例文'));
  for (const [title, text] of TIPS) box.append(node('h5', title), node('p', text, 'small'));
  box.append(node('h5', '例文'), node('p', '下の文をそのまま読めます。5本にするなら、ふだんの調子から3本、明るい文から1本、落ち着いた文から1本がおすすめです。文の終わりは、ふだんの言い方に変えて読んでかまいません。', 'small muted'));
  // The examples fold once more; the tips above stay short to read.
  const examples = node('details', undefined, 'voice-examples-box');
  examples.append(node('summary', '例文を開く'));
  const table = node('table', undefined, 'voice-examples');
  const head = node('tr');
  for (const title of ['カテゴリ', 'コピー', '番号', '例文']) head.append(node('th', title));
  table.append(node('thead'));
  table.tHead.append(head);
  const body = node('tbody');
  let number = 0;
  for (const [title, lines] of EXAMPLES) {
    lines.forEach((line, index) => {
      const row = node('tr');
      if (index === 0) {
        // 「少し明るく（…）」: the short name, with the tone note in small text under it.
        const [name, note] = title.split('（');
        const category = node('th', name, 'voice-category');
        if (note) category.append(node('span', note.replace(/）$/, ''), 'voice-category-note'));
        category.scope = 'rowgroup';
        category.rowSpan = lines.length;
        row.append(category);
      }
      number += 1;
      row.append(cell('td', copyButton(line)), node('td', String(number), 'voice-number'), node('td', line));
      body.append(row);
    });
  }
  table.append(body);
  examples.append(table, copyButton(EXAMPLES.flatMap(([, lines]) => lines).join('\n'), '例文をすべてコピー'));
  box.append(examples);
  parent.append(box);
  return box;
}
