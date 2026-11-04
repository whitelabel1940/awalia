{
  "emotional_weight": {"type": "score", "instructions": "emotional_weight: このターンの感情的な重さ", "criteria": ["low: 軽い日常", "medium: 気持ちが動く", "high: 強く気持ちが動く"]},
  "asks_about_past": {"type": "noul", "instructions": "past_reference.asks_about_past: パートナーの過去を尋ねているか"},
  "user_disclosure": {"type": "noul", "instructions": "user_disclosure: ユーザーが自分の弱さや悩みを打ち明けたか"},
  "reveal_secret": {"type": "choice", "instructions": "reveal_request.secret_id: 秘められた過去を聞く対象。該当しなければ none", "criteria": {"none": "秘められた過去への問いではない"}},
  "reveal_manner": {"type": "choice", "instructions": "reveal_request.manner: 過去を聞く態度", "criteria": {"casual": "casual", "sincere": "sincere", "pushy": "pushy"}},
  "domain": {"type": "choice", "instructions": "domains_touched: {id} {description}", "criteria": {"none": "該当なし", "direct": "直接パートナーの判断が問われる", "indirect": "間接的に関わる"}},
  "signature": {"type": "noul", "instructions": "signatures_relevant: {id} {when} が今の返答に関係するか"},
  "past_target": {"type": "choice", "instructions": "past_reference.targets: {id} {description} について質問されたか、既知か重要か", "criteria": {"none": "質問されていない", "known_minor": "既知・軽微", "known_major": "既知・重要", "unknown_minor": "未知・軽微", "unknown_major": "未知・重要"}},
  "memory": {"type": "choice", "instructions": "memory_use: {ref} の {field}", "criteria": {"yes": "yes", "no": "no", "unclear": "unclear"}},
  "expression": {"type": "choice", "instructions": "元の expression 指示に従い、今回の返答に合う顔の枠を1つ選ぶ", "criteria": {"neutral": "neutral", "smile": "smile", "shy": "shy", "surprised": "surprised", "sad": "sad", "troubled": "troubled", "thinking": "thinking", "pout": "pout"}},
  "check_sentence": {"type": "choice", "instructions": "今回の返答のこの文を元の Check 基準で判定: {sentence}", "criteria": {"supported": "記録で支持される・自然な推論・問題なし", "new_minor_detail": "許可された新しい軽微な細部", "contradicted": "記録や設定と明確に矛盾", "forbidden": "許可されない過去・未実行の共有経験等を断定"}}
}
