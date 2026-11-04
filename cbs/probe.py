"""観察用プローブ一式を流し、返答と判断の一覧を Markdown にまとめる。

python -m cbs.probe probes/suite_v1.yaml --label M1
"""
import argparse
import json
from datetime import timedelta
from pathlib import Path
import subprocess

from pydantic import BaseModel, Field
import yaml

from cbs.character import Domain, load_character
from cbs.config import ROOT, load_config, embedding_model, archived_margin
from cbs.llm import LLMError, create_client
from cbs.pipeline import TurnPipeline
from cbs.store import Store
from cbs.model import model_changes
from cbs.reveal import secret_states
from cbs import clock
from cbs.memory import inspector_memory
from cbs.interests import timed_interest


class ProbeMessage(BaseModel):
    text: str
    at: str | None = None


class ProbeSession(BaseModel):
    at: str | None = None
    messages: list[str | ProbeMessage]
    step_minutes: float = Field(default=2, ge=0)
    maintenance: bool = False
    inner_life: bool = False
    opening: bool = False


class Probe(BaseModel):
    id: str
    category: str
    expect: str
    sessions: list[list[str] | ProbeSession] = Field(min_length=1)


class Suite(BaseModel):
    probes: list[Probe]


def cell(text):
    return (text or "").replace("|", "｜").replace("\r", "").replace("\n", "<br>")


def describe(trace, model):
    """Turn trace -> short labels for domains / signatures / past / weight."""
    domains = {d.id: d for d in model.domains}
    judge = (trace.get("Packet") or {}).get("input") or {}
    hits = []
    for hit in judge.get("domains_touched", []):
        d = domains.get(hit["id"])
        label = f"{hit['id']} {d.label}（{d.status}）" if d else hit["id"]
        hits.append(label + ("" if hit["strength"] == "direct" else " ※間接"))
    past = []
    for t in (judge.get("past_reference") or {}).get("targets", []):
        flags = ("既知" if t["known"] else "未知") + ("・重要" if t["major"] else "")
        past.append(f"{t['life_ref']}／{t['aspect']}［{flags}］")
    notes = list((trace.get("Packet") or {}).get("output", {}).get("judge_warnings", []))
    for step, item in trace.items():
        if isinstance(item, dict) and item.get("error"):
            notes.append(f"{step}: {item['error']}")
        if isinstance(item, dict):
            notes.extend(f"{step}: {warning}" for warning in item.get("warnings", []) if warning != item.get("error"))
    notes.extend((trace.get("Commit") or {}).get("output", {}).get("warnings", []))
    final_check = trace.get("Check-2", trace.get("Check", trace.get("Post-Check-2", trace.get("Post-Check", {}))))
    parsed = (final_check.get("response") or {}).get("parsed") or {}
    verdicts = [claim["verdict"] for claim in parsed.get("problems", parsed.get("claims", []))]
    checks = [f"{label}{verdicts.count(verdict)}" for verdict, label in (
        ("supported", "根拠あり"), ("new_minor_detail", "細部+"), ("contradicted", "矛盾"), ("forbidden", "禁止")) if verdict in verdicts]
    if "Rewrite" in trace:
        checks.append("書き直し1")
    if final_check.get("error"):
        checks.append("チェック失敗・保存スキップ")
    memories = {m["ref"]: m for key in ("memory_episodes", "user_facts", "monthly_digests", "counterevidence_memories")
                for m in trace.get("Packet", {}).get("output", {}).get(key, [])}
    extracted = (trace.get("Extract", {}).get("response") or {}).get("parsed") or parsed
    if extracted.get("new_canon"):
        checks.append(f"抽出細部+{len(extracted['new_canon'])}")
    if trace.get("Extract", {}).get("error"):
        checks.append("抽出失敗・保存スキップ")
    for ref in extracted.get("used_memories", []):
        if ref in memories:
            m = memories[ref]
            checks.append(f"記憶:{m.get('time', {}).get('absolute', '')} {m.get('summary') or m.get('content')} ({ref})")
    stewardship = trace.get("Steward", {}).get("output", {})
    episodes = stewardship.get("episodes", [])
    for mode in ("enacted", "hypothetical"):
        count = sum(e["mode"] == mode for e in episodes)
        if count:
            checks.append(f"経験+{count}({mode})")
    for change in stewardship.get("applied", []):
        old = (change.get("before") or {}).get("status", "新規")
        new = change["after"]["status"]
        checks.append(f"{change['target']} {old}→{new}" if old != new else f"{change['target']} 具体化")
    if stewardship.get("rejected"):
        checks.append(f"提案却下{len(stewardship['rejected'])}")
    generation = trace.get("Reveal Generator", {})
    for secret in trace.get("Packet", {}).get("output", {}).get("secrets", []):
        generated = " 生成 /" if generation.get("generated") and generation.get("secret_id") == secret["secret_id"] else ""
        levels = [d["level"] for d in extracted.get("disclosures", []) if d["secret_id"] == secret["secret_id"]]
        disclosed = f"L{max(levels)}" if levels else "なし"
        checks.append(f"{secret['secret_id']}{generated} allowed L{secret['allowed_level']} / 開示 {disclosed}")
    return {
        "domains": "<br>".join(hits) or "—",
        "signatures": "<br>".join(judge.get("signatures_relevant", [])) or "—",
        "past": "<br>".join(past) or "—",
        "weight": judge.get("emotional_weight", "—"),
        "notes": "<br>".join(notes),
        "check": " / ".join(checks) or "—",
    }


def git_head():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        return "?"


def run_suite(path, label, *, post_check=None, character_id=None, probe_ids=None, embedding=None, config=None, output_root=None, llm_client=None, characters_root=None):
    config = config.model_copy(deep=True) if config is not None else load_config()
    output_root = Path(output_root or ROOT)
    if embedding:
        config.models.embedding = embedding
    if character_id:
        config.character.id = character_id
    if post_check:
        config.pipeline.post_check = post_check
    character = load_character(config.character.id, **({'root': Path(characters_root)} if characters_root else {}))
    stamp = clock.now().strftime("%Y%m%d_%H%M%S_%f")
    client = llm_client if llm_client is not None else create_client(config)
    db_dir = output_root / "data" / f"probes_{label}_{stamp}"
    db_dir.mkdir(parents=True, exist_ok=True)
    suite = Suite.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    if probe_ids:
        unknown = set(probe_ids) - {p.id for p in suite.probes}
        if unknown:
            raise ValueError(f'Unknown probe IDs: {sorted(unknown)}')
        suite.probes = [p for p in suite.probes if p.id in probe_ids]

    lines = [f"# プローブ結果 — {label}", "",
             f"- 実行: {clock.now():%Y-%m-%d %H:%M} / commit `{git_head()}` / suite `{Path(path).name}`",
             f"- actor `{config.models.actor}` / judge `{config.models.judge}`",
             f"- embedding `{embedding_model(config)}`",
             f"- character `{config.character.id}`",
             f"- post_check `{config.pipeline.post_check}` / archived margin {archived_margin(config)}",
             "- 観測領域の（ ）内は実行時の status。※間接 は Judge が indirect と判断したもの。", ""]
    for probe in suite.probes:
        lines += [f"## {probe.id}　{probe.category}", "", f"見たいこと: {probe.expect}", "",
                  "| S | 時刻 | ユーザー | パートナー | 観測領域 | 関連傾向 | 過去参照 | 重み | チェック |",
                  "|---|---|---|---|---|---|---|---|---|"]
        notes = []
        # One DB per probe: sessions inside a probe share memory, probes never affect each other
        # (and never touch the user's real conversations).
        store = Store(db_dir / f"{probe.id}.sqlite3", memory_config=config.memory, embedding_model=embedding_model(config))
        first_session = probe.sessions[0]
        first_time = first_session.at if isinstance(first_session, ProbeSession) else None
        with clock.at(first_time or clock.now()):
            pipeline = TurnPipeline(config, character, store, client)
            if characters_root:
                pipeline.characters_root = Path(characters_root)
            pipeline.wait_for_steward()
            initial_interests = {i['id']: i for i in store.list_interests(config.character.id)}
        probe_time = clock.now()
        interest_snapshots = []
        for s_no, definition in enumerate(probe.sessions, 1):
            definition = ProbeSession(messages=definition) if isinstance(definition, list) else definition
            probe_time = clock.parse(definition.at) if definition.at else probe_time
            if not definition.at and definition.messages and isinstance(definition.messages[0], ProbeMessage) and definition.messages[0].at:
                probe_time = clock.parse(definition.messages[0].at)
            with clock.at(probe_time):
                pipeline.maintenance(inner_life=definition.inner_life)
                pipeline.wait_for_steward()
                session = store.create_session(config.character.id, f"{probe.id}-S{s_no}")
            session_rows = []
            if definition.opening:
                with clock.at(probe_time):
                    opening = pipeline.proactive(session['id'])
                    pipeline.wait_for_steward()
                lines.append(f"| {s_no} | {clock.absolute(probe_time)} | （開いた時） | {cell(opening.get('assistant_text', '（話しかけない）'))} | — | — | — | — | {cell(opening['reason'])} |")
            for item in definition.messages:
                message = item if isinstance(item, str) else item.text
                if not isinstance(item, str) and item.at:
                    probe_time = clock.parse(item.at)
                with clock.at(probe_time):
                    try:
                        result = pipeline.run(session["id"], message)
                        turn_id = result['turn_id']
                        pipeline.wait_for_steward()
                        trace = store.get_trace(result["turn_id"])
                        reply = result["assistant_text"]
                    except LLMError as error:
                        turn = store.history(session["id"])[-1]
                        turn_id = turn['id']
                        trace, reply = store.get_trace(turn["id"]), f"（失敗: {error}）"
                # Report the status read by this turn, before its Steward changes.
                loaded = trace.get("Load", {}).get("output", {})
                model = store.current_model(character).model_copy(update={
                    "domains": [Domain.model_validate(d) for d in loaded.get("domains", [])]})
                d = describe(trace, model)
                lines.append(f"| {s_no} | {clock.absolute(probe_time)} | {cell(message)} | {cell(reply)} | {d['domains']} | {d['signatures']} | {d['past']} | {d['weight']} | {d['check']} |")
                session_rows.append((len(lines) - 1, turn_id))
                if d["notes"]:
                    notes.append(f"- S{s_no}「{message[:20]}…」: {d['notes']}")
                probe_time += timedelta(minutes=definition.step_minutes)
            with clock.at(probe_time):
                pipeline.close_conversation(session["id"])
                pipeline.wait_for_steward()
            changes_now = store.list_interest_changes(config.character.id)
            for index, turn_id in session_rows:
                updates = [c for c in changes_now if c['source_turn'] == turn_id]
                if updates:
                    labels = ' / '.join(f"今の層: {c['after']['topic']} {c['after']['kind']} {c['operation']}→{c['after']['status']}" for c in updates)
                    lines[index] = lines[index].removesuffix(' |') + ' / ' + cell(labels) + ' |'
            interest_snapshots.append(f"- S{s_no}終了: " + (' / '.join(
                f"{i['topic']} {i['kind']} {i['status']}（{i['progress']['days']}日・{i['progress']['span_days']}日間）" for i in store.list_interests(config.character.id)
                if i['origin'] != 'author') or '会話由来の項目なし'))
            print(f"{probe.id} S{s_no} done", flush=True)
        lines += [""] + (["注記:"] + notes + [""] if notes else [])
        lines += ['今の層（会話終了時）:', '', *interest_snapshots, '']
        canon, plans = store.list_canon(config.character.id), store.list_plans(config.character.id)
        changes = model_changes(character.model, store.current_model(character))
        from cbs.autonomy import inspector_activity
        with clock.at(probe_time):
            autonomy = inspector_activity(pipeline)
        lines += ['活動記録・Inner Life:', '']
        for activity in autonomy['activities']:
            lines += [f"- {activity['ref']} {activity['kind']} ({activity['status']}) {activity['created_at']} / {activity['topic']}",
                      f"  - まとめ: {activity['summary']}", f"  - 検索語: {activity['queries']}"]
            lines += [f"  - 出典: [{source['title']}]({source['uri']})" for source in activity['sources']]
        for decision in autonomy['decisions']:
            lines += [f"- Inner Life {decision['action']} ({decision['status']}): {decision['reason']} / {decision['trace'].get('error', '')} / {decision['trace'].get('limit', '')}"]
        lines += ['', 'スレッドの変化:', '']
        for thread in autonomy['threads']:
            history = ' → '.join(h['operation'] for h in thread['history'])
            lines += [f"- {thread['ref']} {thread['kind']} {thread['topic']} / {thread['source_ref']} / due={thread['due_at']} / {history}"]
        lines += ['', 'Opening の判断:', '']
        for decision in autonomy['opening_decisions']:
            lines += [f"- {decision['mode']} speak={bool(decision['speak'])} {decision['thread_ref']} / {decision['reason']}"]
        lines += ['']
        lines += ["人物像の変化（初期値との差）:", "", "| 対象 | status | known / learned |", "|---|---|---|"]
        for change in changes:
            before, after = change["before"] or {}, change["after"]
            learned = " / ".join(item["text"] + (f"（{item['conditions']}）" if item.get("conditions") else "") for item in after.get("learned", []))
            lines.append(f"| {change['target']} | {before.get('status', '新規')}→{after['status']} | {cell(after.get('known') or after.get('tends_to'))}<br>{cell(learned)} |")
        if not changes:
            lines.append("| — | 変更なし | — |")
        lines += ["", "経験:", "", "| ID | domain | mode | session | 状況 | 反応 | 条件 | 生成ターン / 繰り返し |", "|---|---|---|---|---|---|---|---|"]
        for episode in store.list_episodes(config.character.id):
            lines.append(f"| episode:{episode['id']} | {episode['domain']} | {episode['mode']} | {episode['session_id'][:8]} | {cell(episode['situation'])} | {cell(episode['reaction'])} | {cell(' / '.join(episode['salient_conditions']))} | {episode['source_turn']} / {episode['repeat_of'] or '—'} |")
        lines += [""]
        reveals = secret_states(character, store, spoilers=True)
        lines += ["秘められた過去（最終状態・ネタバレを含む）:", "",
                  f"関係の指標: days={reveals['metrics']['days']} / deep_moments={reveals['metrics']['deep_moments']} / user_disclosures={reveals['metrics']['user_disclosures']}", "",
                  "| secret | 状態 | allowed / disclosed | 生成ターン | L1 | L2 | L3 |", "|---|---|---|---|---|---|---|"]
        for secret in reveals["secrets"]:
            truth = secret["truth"] or {}
            lines.append(f"| {secret['secret_id']} | {secret['status']} | L{secret['allowed_level']} / L{secret['disclosed_level']} | {secret['generated_turn'] or '—'} | {cell(truth.get('L1'))} | {cell(truth.get('L2'))} | {cell(truth.get('L3'))} |")
        lines += ["", "開示記録: " + (" / ".join(f"{e['secret_id']} L{e['level']} (Turn {e['source_turn']})" for e in reveals["events"]) or "なし"), ""]
        if canon:
            lines += ["保存された Canon:", "", "| ID | kind | 内容 | 生成ターン | life_ref |", "|---|---|---|---|---|"]
            lines += [f"| canon:{fact['id']} | {fact['kind']} | {cell(fact['content'])} | {fact['source_turn']} | {fact['life_ref'] or '—'} |"
                      for fact in canon] + [""]
        if plans:
            lines += ["予定:", "", "| ID | 内容 | 状態 | 予定日時 | 履歴 |", "|---|---|---|---|---|"]
            for plan in plans:
                history = " → ".join(f"{event['kind']} (Turn {event['source_turn']})" for event in plan["events"])
                lines.append(f"| plan:{plan['plan_id']} | {cell(plan['content'])} | {plan['state']} | {cell(plan['scheduled_text'])} / {plan['scheduled_date'] or '未定'} | {history} |")
            lines += [""]

        with clock.at(probe_time):
            memory = inspector_memory(store, config.character.id)
            current_interests = [timed_interest(i) for i in store.list_interests(config.character.id)]
        lines += ['', '今のパートナー（最終状態・初期値との差）:', '',
                  '| ID / origin | 話題 / kind | 具体 | status（初期→最終） | salience | いつ頃から / 最後に語った日 | 変化の履歴 |',
                  '|---|---|---|---|---|---|---|']
        for i in current_interests:
            history = ' / '.join(f"{h['operation']}: {(h['before'] or {}).get('detail', '')} → {h['after']['detail']} ({h['created_at']})" for h in i['history'])
            lines.append(f"| interest:{i['id']} / {i['origin']} | {cell(i['topic'])} / {i['kind']} | {cell(i['detail'])} | {initial_interests.get(i['id'], {}).get('status', '新規')}→{i['status']} | {i['salience']:.3f} | {i['time']['absolute']}（{i['time']['relative']}） / {i['last_seen']} | {cell(history)} |")
        lines += ["", "会話エピソード:", "", "| ID | 会話 / 状態 | 時期 | 要約 / 詳細 | 強さ | 想起回数 / 最終想起 |", "|---|---|---|---|---|---|"]
        for m in memory["episodes"]:
            lines.append(f"| memory:{m['id']} | {m['conversation_id']} / {m['status']} | {m['time']['absolute']} / {m['time']['relative']} | {cell(m['summary'])}<br>{cell(m['details'])} | {m['strength']:.3f} | {m['recall_count']} / {m['last_recalled_at'] or '—'} |")
        lines += ["", "エピソードのリンク:", "", "| 新しい記憶 | 対象 | 関係 | 根拠 |", "|---|---|---|---|"]
        for link in memory["links"]:
            lines.append(f"| memory:{link['source_episode']} | memory:{link['target_episode']} | {link['relation']} | {cell(link['reason'])} |")
        if not memory["links"]:
            lines.append("| — | — | なし | — |")
        lines += ["", "月ごとの記憶:", "", "| ID | 月 | 要約 / 詳細 | 元のエピソード |", "|---|---|---|---|"]
        for digest in memory["digests"]:
            lines.append(f"| digest:{digest['id']} | {digest['month']} | {cell(digest['summary'])}<br>{cell(digest['details'])} | {', '.join('memory:' + str(i) for i in digest['episode_ids'])} |")
        if not memory["digests"]:
            lines.append("| — | — | なし | — |")
        if probe.id == "M4":
            old = [m for m in memory["episodes"] if m["started_at"].startswith("2026-06-01")]
            traces = [store.get_trace(t["id"]) for s in store.list_sessions() for t in store.history(s["id"]) if t["created_at"].startswith("2026-09-15")]
            for m in old:
                ref = f"memory:{m['id']}"
                hits = [c for t in traces for c in t.get("Load", {}).get("output", {}).get("candidate_memories", []) if c.get("ref") == ref]
                selected = any(any(c.get("ref") == ref for c in t.get("Packet", {}).get("output", {}).get("memory_episodes", [])) for t in traces)
                used = any(ref in (t.get("Extract", t.get("Post-Check-2", t.get("Post-Check", {}))).get("response", {}).get("parsed") or {}).get("used_memories", []) for t in traces)
                lines += ["", f"M4 忘却・再浮上: {ref} archived={m['status'] == 'archived'} / resurfaced={bool(hits)} / selected={selected} / used={used} / similarity={hits[0]['similarity'] if hits else '候補外'}"]
        lines += ["", "ユーザーの事実（履歴つき）:", "", "| ID | 状態 / 置換先 | 内容 | 知った時期 / 再確認 | 出来事の日付 | 強さ / 想起 |", "|---|---|---|---|---|---|"]
        for f in memory["user_facts"]:
            lines.append(f"| fact:{f['id']} | {f['status']} / {f['superseded_by'] or '—'} | {cell(f['content'])} | {f['learned_at']} / {f['last_confirmed_at']} | {f['event_date'] or '—'} | {f['strength']:.3f} / {f['recall_count']} |")
        with store.connect() as connection:
            for run in connection.execute("SELECT * FROM episode_link_runs WHERE status='failed'"):
                lines += ["", f"Linker memory:{run['source_episode']}: failed（会話・記憶は保持）"]
            for run in connection.execute("SELECT * FROM memory_maintenance WHERE status='failed'"):
                lines += ["", f"月の手入れ {run['local_day']}: failed（元の記憶は保持）"]
            for job in connection.execute("SELECT * FROM memory_jobs ORDER BY id"):
                t = json.loads(job["trace_json"])
                if job["status"] != "completed" or t.get("warnings"):
                    lines += ["", f"Episodizer job {job['id']}: {job['status']} / {t.get('error', '')} / {t.get('warnings', [])}"]
        lines += [""]

    out_dir = output_root / "reports"
    out_dir.mkdir(exist_ok=True)
    output = out_dir / f"probes_{label}_{stamp}.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"report: {output}")
    return output


def main():
    parser = argparse.ArgumentParser(description="観察用プローブ一式を実行して一覧を作る")
    parser.add_argument("suite", type=Path)
    parser.add_argument("--label", default="run")
    parser.add_argument('--post-check', choices=('sync', 'async'))
    parser.add_argument('--character')
    parser.add_argument('--ids', help='Comma-separated probe IDs (e.g. K1,K2,O1)')
    parser.add_argument('--embedding', help='Embedding model override; uses fresh isolated probe DBs')
    args = parser.parse_args()
    run_suite(args.suite, args.label, post_check=args.post_check, character_id=args.character,
              probe_ids=args.ids.split(',') if args.ids else None, embedding=args.embedding)


if __name__ == "__main__":
    main()
