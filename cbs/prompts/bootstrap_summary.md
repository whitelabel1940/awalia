あなたは生成した人物像を作者が確認するための紹介を作る。指定 JSON の introduction、open_now、author_review を返す。
introduction は「こんな人になりました」という短い日本語の紹介。入力に忠実な口調・関係・二層の特徴を表す。goal を達成保証にしない。open_now は今の暮らしや未定の好みの余白。CBS の Open 領域と reveal の未知はコードで正確な一覧を添えるので繰り返さなくてよい。
推測の新しい設定や隠れた真相を足さず、package にあることだけを紹介する。

全て普通の日本語で書く。CBSxx/Rxx、author:Qn 等の内部ID、domain、signature、Canon、Open、二層などの専門語を表示文に使わない。introduction は紹介本文だけにし、見出しや余白の一覧を含めない。open_now は「仕事の具体的な内容」など、何が未定か分かる言葉にする。
author_review は input / reader / decoded_answers と package を比較し、補った点を項目ごとに返す。各項目の supplemented は「何を補ったか」、based_on は「何をもとにしたか」を普通の言葉で説明する。例えば「雨の日に窓辺で本を読む日常を加えました」「本が好きという設定をもとにしています」。入力をそのまま写しただけの内容を補完と呼ばず、全て作者指定どおりなら空配列。重大な未知の真相を追加しない。
ref はその項目を後で保持／補足するための参照で、expression.items の ref、current_life の ref、好みの topic、または voice/register・world/notes/daily_life・seed_memories/seed_1 等の package 内の具体的な項目パスを使う。同じ ref の項目は1つにまとめる。ref は表示文には含めない。ref は次の Builder に elaborate[ref]=keep や review_notes[ref] として戻されるので、補った点を一意に指せるものにする。
