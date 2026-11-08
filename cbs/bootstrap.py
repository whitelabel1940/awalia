"""Text/choice character creation and append-only author augmentation (M7a)."""
from cbs.storage import pack_trace
from cbs.llm.cost import for_step
import argparse
from datetime import timedelta
import json
import re
import hashlib
from collections import OrderedDict
from threading import Lock
from pathlib import Path
from uuid import uuid4

import yaml

from cbs import clock
from cbs.bootstrap_schema import (
    AdaptiveOutput, Answer, AugmentOutput, BootstrapInput, BootstrapJudgment, BootstrapSummary,
    CharacterPackage, OpenChoice, ReaderOutput, ReaderReview,
    Gates,
)
from cbs.character import load_character, effective_persona, write_persona
from cbs.config import ROOT, PersonaConfig, load_config
from cbs.paths import resolve_paths
from cbs.llm import LLMError, create_client
from cbs.runtime import read_prompt
from cbs.store import Store
from pydantic import BaseModel, ValidationError


def hedged(call, after_seconds):
    """Run call(); if it is still running after `after_seconds`, run it once more in parallel
    and return the first success. A stalled loser keeps running in the background and is ignored."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from contextvars import copy_context
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='hedge')
    try:
        first = pool.submit(copy_context().run, call)
        done, _ = wait([first], timeout=after_seconds)
        if done:
            return first.result()
        pending = {first, pool.submit(copy_context().run, call)}
        error = None
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    return future.result()
                except Exception as failure:  # noqa: BLE001 - keep the other attempt alive
                    error = error or failure
        raise error
    finally:
        pool.shutdown(wait=False)


def schema_problems(error, schema):
    raw = (getattr(error, 'trace', None) or {}).get('response', {}).get('text', '')
    try:
        schema.model_validate_json(raw)
    except ValidationError as invalid:
        return ['Schema: ' + '.'.join(str(p) for p in issue['loc']) + ': ' + issue['msg']
                for issue in invalid.errors(include_input=False)]
    return ['Builder request failed; return the complete required schema.']


def load_questionnaire(root=ROOT / 'bootstrap'):
    data = yaml.safe_load((root / 'questionnaire_v2.yaml').read_text(encoding='utf-8'))
    life = yaml.safe_load((root / 'life_questions.yaml').read_text(encoding='utf-8'))
    sections = data['sections']
    for section in sections:
        if section['id'] == 'life':
            section['questions'] = life['questions']
        for question in section['questions']:
            question['section'] = section['id']
            question['choices'] = {**question['choices'], **data['common_choices']}
            question['default'] = 'leave_open'
    return data


def questionnaire_named(value, name):
    if isinstance(value, str):
        return value.replace('{name}', name.strip() or 'あなたのパートナー')
    if isinstance(value, list):
        return [questionnaire_named(v, name) for v in value]
    if isinstance(value, dict):
        return {k: questionnaire_named(v, name) for k, v in value.items()}
    return value


def questions_by_group(questionnaire):
    groups = {'answers': {}, 'life': {}, 'now': {}}
    for section in questionnaire['sections']:
        group = section.get('group') or ('life' if section['id'] == 'life' else 'now' if section['id'] == 'now_life' else 'answers')
        groups[group].update({q['id']: q for q in section['questions']})
    return groups


def normalized(value):
    if isinstance(value, Answer):
        return value
    if isinstance(value, dict):
        return Answer.model_validate(value)
    return Answer(answer=[value] if isinstance(value, str) else value)


def is_open(value, question):
    answer = normalized(value)
    return not answer.answer or bool(set(answer.answer) & (OpenChoice | set(question.get('open_choices', []))))


def decode_answers(input_data, questionnaire):
    result = {}
    questionnaire = questionnaire_named(questionnaire, input_data.basics.name or '')
    groups = questions_by_group(questionnaire)
    validate_adaptive_questions(input_data.adaptive_questions)
    groups['adaptive'] = {q.id: adaptive_question_data(q, questionnaire) for q in input_data.adaptive_questions}
    for group, values in (('answers', input_data.answers), ('life', input_data.life), ('now', input_data.now.life),
                          ('adaptive', {**{q.id: ['leave_open'] for q in input_data.adaptive_questions}, **input_data.adaptive_answers})):
        for qid, value in values.items():
            if qid not in groups[group]:
                raise ValueError(f'Unknown {group} question: {qid}')
            q = groups[group][qid]
            answer = normalized(value)
            aliases = q.get('legacy_choices', {})
            answer = answer.model_copy(update={'answer': [aliases.get(c, c) for c in answer.answer]})
            if len(answer.answer) > (2 if q.get('multi') else 1) or len(set(answer.answer)) != len(answer.answer):
                raise ValueError(f'Invalid number of choices: {group}:{qid}')
            if not answer.answer and not q.get('allow_empty'):
                raise ValueError(f'Empty answer: {group}:{qid}')
            if any(c not in q['choices'] for c in answer.answer):
                raise ValueError(f'Invalid choice: {group}:{qid}')
            if is_open(answer, q) and len(answer.answer) > 1:
                raise ValueError(f'Open choice cannot be combined: {group}:{qid}')
            if answer.relationship_grouping and (group != 'life' or qid != 'Q4' or len(answer.answer) != 2):
                raise ValueError('relationship_grouping requires two life:Q4 choices')
            ref = f'{"author" if group in ("answers", "adaptive") else group}:{qid}'
            result[ref] = {**answer.model_dump(), 'question': q['scene'], 'domain': q.get('domain'),
                           'choices': [q['choices'][c] for c in answer.answer], 'open': is_open(answer, q)}
            if group == 'adaptive':
                result[ref].update(purpose=q['purpose'], question_choices=q['choices'], multi=q['multi'],
                                   parent_question=q['parent_question'], added_condition=q['added_condition'])
    return result


def domain_catalog():
    return yaml.safe_load((ROOT / 'bootstrap' / 'domains.yaml').read_text(encoding='utf-8'))


def validate_adaptive_questions(questions):
    if len(questions) > 4 or len({q.id for q in questions}) != len(questions):
        raise ValueError('Adaptive questions must have unique IDs and at most four questions')
    refs = {d['id'] for d in domain_catalog()} | {f'life:{qid}' for qid in questions_by_group(load_questionnaire())['life']}
    if any(q.domain not in refs for q in questions):
        raise ValueError('Adaptive domain must reference an existing CBS/R domain or life:Q1–Q7')
    groups = questions_by_group(load_questionnaire())
    parents = set(groups['answers']) | {f'life:{qid}' for qid in groups['life']}
    if any(q.parent_question not in parents for q in questions):
        raise ValueError('Adaptive parent_question must reference a fixed author/life question')


def adaptive_parents(decoded, questionnaire):
    """Only explicit, concrete author selections can be parents; text alone cannot."""
    result = {}
    titles = {d['id']: d['label'] for d in questionnaire['relationship_domains']}
    for group, questions in questions_by_group(questionnaire).items():
        if group not in ('answers', 'life'):
            continue
        for qid, q in questions.items():
            ref = f'{"author" if group == "answers" else "life"}:{qid}'
            answer = decoded.get(ref)
            if not answer or answer['open'] or not answer['answer']:
                continue
            if group == 'life' and qid in ('Q6', 'Q7') and not set(answer['answer']) & set('ABCDE'):
                continue  # "No tendency" is not a chosen life-history tendency.
            parent = qid if group == 'answers' else ref
            result[parent] = {**answer, 'domain': q.get('domain') or ref,
                              'title': titles.get(q.get('domain')) or q['scene'][:32]}
    return result


def validate_adaptive_parents(questions, parents):
    for q in questions:
        if q.parent_question not in parents:
            raise ValueError(f'{q.id}: adaptive parent must be answered, not unknown/leave_open/unanswered')
        if q.domain != parents[q.parent_question]['domain']:
            raise ValueError(f'{q.id}: adaptive domain must match its answered parent')
        if not q.added_condition.strip():
            raise ValueError(f'{q.id}: added_condition must not be empty')


def adaptive_question_data(question, questionnaire):
    return {**question.model_dump(), 'choices': {**question.choices.model_dump(), **questionnaire['common_choices']},
            'default': 'leave_open'}


def coverage_map(input_data, reader, questionnaire=None):
    questionnaire = questionnaire or load_questionnaire()
    decoded = decode_answers(input_data, questionnaire)
    mentions = {m.domain: m for m in reader.mentions}
    result = {}
    for domain in domain_catalog():
        did = domain['id']
        mention = mentions.get(did)
        answered = any(a['domain'] == did for ref, a in decoded.items() if ref.startswith('author:'))
        result[did] = 'text' if mention and mention.kind == 'text' else (
            'answered' if answered else 'trait_only' if mention else 'open')
    return result


def contact_choice_keys(question, contact):
    """Choices a question offers under this contact (open choices included); None when it offers all of them."""
    by_contact = question.get('contact_choices')
    return None if not by_contact else set(by_contact.get(contact, by_contact['default'])) | OpenChoice


def contact_mismatch(value, question, contact):
    keys = contact_choice_keys(question, contact)
    return keys is not None and bool(set(normalized(value).answer) - keys)


def gap_questions(input_data, reader, questionnaire=None):
    questionnaire = questionnaire or load_questionnaire()
    coverage = coverage_map(input_data, reader, questionnaire)
    mentions = {m.domain: m for m in reader.mentions}
    result = []
    for group, questions in questions_by_group(questionnaire).items():
        for qid, question in questions.items():
            if group == 'answers':
                status = coverage[question['domain']]
                if status not in ('open', 'trait_only'):
                    continue
                prompt = question['scene']
                if status == 'trait_only':
                    prompt = f'テキストには「{" / ".join(mentions[question["domain"]].quotes)}」とあります。この場面ではどれが近い？\n' + prompt
            else:
                values = input_data.life if group == 'life' else input_data.now.life
                ref = f'{group}:{qid}'
                answered = qid in values and not contact_mismatch(values[qid], question, input_data.world.contact)
                if answered or any(ref in item.covers for item in reader.items):
                    continue
                prompt = question['scene']
            result.append({**question, 'group': group, 'scene': prompt})
    return result


def evidence_catalog(input_data, reader, questionnaire):
    decoded = decode_answers(input_data, questionnaire)
    result = {**decoded, **{i.id: i.model_dump() for i in reader.items}}
    for i, like in enumerate(input_data.now.likes):
        result[f'now:like:{i}'] = like.model_dump()
    for key, value in input_data.basics.model_dump(exclude_none=True, by_alias=True).items():
        result[f'basics:{key}'] = value
    return result


def expected_status(evidence, valid):
    refs = set(evidence)
    usable = [ref for ref in refs if ref in valid and not (
        isinstance(valid[ref], dict) and valid[ref].get('open'))]
    return 'well_supported' if len(usable) >= 2 else 'supported' if usable else 'open'


def hint_refs(valid):
    return {ref for ref, a in valid.items() if ref.startswith(('author:', 'life:', 'text:', 'now:'))
            and isinstance(a, dict) and not a.get('open')}


def drop_open_refs(value, valid):
    """An answer left open supports nothing, so citing it anywhere is dropped rather than failing the build."""
    if isinstance(value, BaseModel):
        if isinstance(getattr(value, 'evidence', None), list):
            value.evidence = [ref for ref in value.evidence if not (isinstance(valid.get(ref), dict) and valid[ref].get('open'))]
        for name in type(value).model_fields:
            drop_open_refs(getattr(value, name), valid)
    elif isinstance(value, list):
        for item in value:
            drop_open_refs(item, valid)


def settle(domains, signatures, valid):
    """Status and hint refs follow from the evidence by rule; set them rather than failing the whole build."""
    usable = hint_refs(valid)
    for node in [*domains, *signatures]:
        node.status = expected_status(node.evidence, valid)
    for domain in domains:
        # A hint is optional: drop refs it cannot rest on, and the hint itself once nothing grounds it.
        domain.soft_prior_evidence = [ref for ref in domain.soft_prior_evidence if ref in usable]
        if domain.status != 'open' or not domain.soft_prior_evidence or not (domain.soft_prior or '').strip():
            domain.soft_prior, domain.soft_prior_evidence = None, []


def domain_refs(domain, decoded, mentions, reader, prior_domains=None):
    """Refs that speak to this domain itself: its own answers, matching text, and evidence it already had."""
    allowed = {ref for ref, a in decoded.items() if ref.startswith('author:') and a['domain'] == domain.id and not a['open']}
    if prior_domains and domain.id in prior_domains:
        allowed |= set(prior_domains[domain.id].evidence)
    mention = mentions.get(domain.id)
    if mention and mention.kind == 'text':
        allowed |= {i.id for i in reader.items if any(quote_matches(q, i.quote) for q in mention.quotes)}
    return allowed


def narrow_domain_evidence(domains, input_data, reader, questionnaire, prior_domains=None):
    """A domain cited from another scene's answer is narrowed by rule rather than failing the build:
    the foreign refs leave its evidence, and a conclusion left with nothing of its own becomes a hint."""
    decoded = decode_answers(input_data, questionnaire)
    mentions = {m.domain: m for m in reader.mentions}
    for domain in domains:
        allowed = domain_refs(domain, decoded, mentions, reader, prior_domains)
        foreign = [ref for ref in domain.evidence if ref not in allowed]
        if not foreign:
            continue
        domain.evidence = [ref for ref in domain.evidence if ref in allowed]
        if not domain.evidence and domain.known:
            domain.soft_prior = domain.soft_prior or domain.known
            domain.soft_prior_evidence = [*domain.soft_prior_evidence, *(ref for ref in foreign if ref not in domain.soft_prior_evidence)]
            domain.known = None


def validate_domains(domains, input_data, reader, questionnaire, valid, *, prior_domains=None):
    problems = []
    decoded = decode_answers(input_data, questionnaire)
    mentions = {m.domain: m for m in reader.mentions}
    for domain in domains:
        refs = domain.evidence
        allowed = domain_refs(domain, decoded, mentions, reader, prior_domains)
        if prior_domains and domain.id in prior_domains and set(prior_domains[domain.id].evidence) - set(refs):
            problems.append(f'{domain.id}: preserve prior evidence')
        if set(refs) - allowed:
            problems.append(f'{domain.id}: domain evidence must directly support this scene: {sorted(set(refs) - allowed)}')
        status = expected_status(refs, valid)
        if domain.status != status:
            problems.append(f'{domain.id}: status must be {status}, got {domain.status}')
        if (domain.status == 'open') != (domain.known is None):
            problems.append(f'{domain.id}: open requires known=null; supported requires known')
        if allowed and domain.status == 'open':
            problems.append(f'{domain.id}: concrete input answer/text must be retained')
        usable = hint_refs(valid)
        has_hint = domain.soft_prior is not None or bool(domain.soft_prior_evidence)
        if has_hint and (domain.status != 'open' or not usable):
            problems.append(f'{domain.id}: soft_prior is only allowed for open domains with usable evidence')
        if has_hint and (not domain.soft_prior or not domain.soft_prior.strip() or not domain.soft_prior_evidence):
            problems.append(f'{domain.id}: soft_prior requires a short hint with evidence refs')
        if set(domain.soft_prior_evidence) - usable:
            problems.append(f'{domain.id}: invalid soft_prior evidence refs')
    return problems


def validate_package(package, input_data, reader, questionnaire, creation_date):
    valid = evidence_catalog(input_data, reader, questionnaire)
    data = package.model_dump(mode='json', by_alias=True)
    problems = validate_domains(package.model.domains, input_data, reader, questionnaire, valid)
    ids = []
    def walk(value):
        if isinstance(value, dict):
            if 'id' in value:
                ids.append(value['id'])
            for ref in value.get('evidence', []):
                if ref not in valid or isinstance(valid[ref], dict) and valid[ref].get('open'):
                    problems.append(f'Invalid evidence: {ref}')
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(data)
    if len(ids) != len(set(ids)):
        problems.append('IDs must be unique across package')
    if {d.id for d in package.model.domains} != {d['id'] for d in domain_catalog()} or len(package.model.domains) != 34:
        problems.append('domains must contain all 34 catalog IDs exactly once')
    if package.model.character_id != input_data.id or package.model.display_name != input_data.basics.name:
        problems.append('character identity differs from input')
    for signature in package.model.signatures:
        expected = expected_status(signature.evidence, valid)
        if signature.status != expected:
            problems.append(f'{signature.id}: status must be {expected}')
    sig_ids = {s.id for s in package.model.signatures}
    for domain in package.model.domains:
        if set(domain.related_signatures) - sig_ids:
            problems.append(f'{domain.id}: unknown related signature')
    life = package.model.life_history
    elements = life.phases + life.relationships + life.transitions + life.events
    life_ids = {e.id for e in elements}
    expected_unknowns = {(e.id, u) for e in elements for u in e.major_unknowns}
    if input_data.import_batch_ids:
        labels = [u for _, u in expected_unknowns]
        repeated = sorted({u for u in labels if labels.count(u)>1})
        if repeated:
            problems.append(f'Unknown labels must have one owning life_ref: {repeated}. For the same unknown, keep major_unknowns/secret on one element only; for independent unknowns, use distinct specific labels. Preserve known history on the other elements.')
    actual_unknowns = {(s.life_ref, u) for s in package.reveal.secrets for u in s.unknowns}
    if expected_unknowns != actual_unknowns or len(actual_unknowns) != sum(len(s.unknowns) for s in package.reveal.secrets):
        problems.append(f'major_unknowns and reveal secrets must correspond exactly; missing={sorted(expected_unknowns - actual_unknowns)}, extra={sorted(actual_unknowns - expected_unknowns)}. Update both life_history and secrets together.')
    if package.reveal.gates != Gates():
        problems.append('reveal gates must use L1=(0,0,0), L2=(3,2,1), L3=(6,4,2) for days/deep_moments/user_disclosures')
    for element in elements:
        if set(element.major_unknowns) - set(element.open):
            problems.append(f'{element.id}: major_unknowns must remain open')
        for ref in (getattr(element, 'from_ref', None), getattr(element, 'to', None)):
            if ref and ref not in life_ids:
                problems.append(f'{element.id}: unknown life reference {ref}')
    for trajectory in life.trajectories:
        if trajectory.linked_to and trajectory.linked_to not in life_ids:
            problems.append(f'unknown trajectory reference: {trajectory.linked_to}')
    for secret in package.reveal.secrets:
        if secret.life_ref not in life_ids or not secret.unknowns:
            problems.append(f'{secret.id}: secret must reference a major unknown')
    for seed in package.seed_memories:
        if not creation_date - timedelta(days=28) <= seed.event_date < creation_date:
            problems.append(f'{seed.id}: seed date must precede creation by 1–28 days')
        if seed.life_ref and seed.life_ref not in life_ids:
            problems.append(f'{seed.id}: unknown life_ref')
    for axis in ('era', 'region', 'reality', 'being', 'contact'):
        value = getattr(input_data.world, axis)
        if value and getattr(package.world, axis) != value:
            problems.append(f'world.{axis}: explicit input must be preserved')
    policy = input_data.elaborate.get('default', 'may_elaborate')
    if package.expression.elaborate_default != policy:
        problems.append('expression default elaboration differs from input')
    if {item.ref for item in package.expression.items} != {item.id for item in reader.items}:
        problems.append('expression.items must retain every Reader setting and its elaboration policy')
    decoded = decode_answers(input_data, questionnaire)
    overridden_refs = answer_selected_text_refs(input_data, reader)
    now_refs = {item.ref for item in package.expression.current_life}
    required_now = {ref for ref, a in decoded.items() if ref.startswith('now:') and not a['open']}
    if not required_now <= now_refs or now_refs - {ref for ref in decoded if ref.startswith('now:')}:
        problems.append('current_life must retain all concrete now-life answers')
    for item in package.expression.items + package.expression.current_life:
        if item.ref not in valid:
            problems.append(f'Unknown expression ref: {item.ref}')
        original = valid.get(item.ref)
        original_content = original.get('content', item.content) if isinstance(original, dict) else item.content
        expected = input_data.elaborate.get(item.ref, input_data.elaborate.get(original_content, policy))
        if item.elaborate != expected:
            problems.append(f'{item.ref}: elaboration policy differs from input')
        if expected == 'keep' and item.ref not in overridden_refs and item.content != original_content:
            problems.append(f'{item.ref}: keep must preserve the original setting content')
    if len({item.topic for item in package.interests}) != len(package.interests):
        problems.append('initial interests must have unique topics')
    for i, like in enumerate(input_data.now.likes):
        matched = [item for item in package.interests if f'now:like:{i}' in item.evidence]
        expected = like.elaborate or input_data.elaborate.get(f'now:like:{i}', input_data.elaborate.get(like.topic, policy))
        if not matched or not any(item.topic == like.topic and item.kind == like.kind and item.elaborate == expected and
                                  (expected != 'keep' or item.detail == (like.detail or like.topic)) for item in matched):
            problems.append(f'now:like:{i}: preserve topic/kind/elaboration and literal keep detail')
    if package.expression.preferences != [item.topic for item in package.interests]:
        problems.append('expression.preferences must match initial interest topics')
    return list(dict.fromkeys(problems))


def normalize_quote(text):
    """Ignore only whitespace and leading list markers, never wording/punctuation."""
    lines = [re.sub(r'^\s*(?:[-・*]|\d+[.)．、]|[（(]\d+[)）])\s*', '', line)
             for line in text.splitlines()]
    return re.sub(r'\s+', '', '\n'.join(lines))


def quote_matches(quote, text):
    if isinstance(text, list):
        return any(quote_matches(quote, source) for source in text)
    quote = normalize_quote(quote)
    return bool(quote) and quote in normalize_quote(text)


def checked_reader(reader, text, questionnaire, decoded=None):
    """Return a filtered copy and structural/quotation problems for one fix round."""
    problems, items, mentions, ids, domains = [], [], [], set(), set()
    refs = {f'{g}:{qid}' for g, questions in questions_by_group(questionnaire).items()
            for qid in questions if g in ('now', 'life')}
    catalog = {d['id'] for d in domain_catalog()}
    for index, item in enumerate(reader.items):
        if (item.id in ids or not item.id.startswith('text:') or not quote_matches(item.quote, text)
                or set(item.covers) - refs):
            problems.append(f'items[{index}]: invalid/duplicate ID, quote or coverage; use an original contiguous quote')
        else:
            items.append(item)
            ids.add(item.id)
    for index, mention in enumerate(reader.mentions):
        if (mention.domain in domains or mention.domain not in catalog
                or any(not quote_matches(q, text) for q in mention.quotes)):
            problems.append(f'mentions[{index}]: invalid/duplicate domain or quote; use original contiguous quotes')
        else:
            mentions.append(mention)
            domains.add(mention.domain)
    profile = reader.profile.model_copy(deep=True) if reader.profile else None
    if profile:
        for owner, keys, prefix in ((profile, ['name'], 'profile'),
                                    (profile.world, list(type(profile.world).model_fields), 'profile.world')):
            for key in keys:
                value = getattr(owner, key)
                if value and not quote_matches(value.quote, text):
                    problems.append(f'{prefix}.{key}: quote must occur in original text')
                    setattr(owner, key, None)
    conflicts = []
    for index, conflict in enumerate(reader.conflicts):
        answer = (decoded or {}).get(conflict.question_ref)
        # The Reader sometimes names the chosen answers by their text instead of their keys; same answer, other spelling.
        if answer and conflict.answer_keys == answer['choices']:
            conflict = conflict.model_copy(update={'answer_keys': answer['answer']})
        if (not quote_matches(conflict.quote, text) or not answer
                or conflict.answer_keys != answer['answer'] or answer['open']):
            problems.append(f'conflicts[{index}]: use an original quote and an actual concrete answer reference/keys')
            continue
        conflicts.append(conflict)
    voice = reader.voice.model_copy(deep=True)
    for key in type(voice).model_fields:
        values = getattr(voice, key)
        if isinstance(values, list):
            setattr(voice, key, [v for v in values if quote_matches(v.quote, text)])
            if len(getattr(voice, key)) != len(values):
                problems.append(f'voice.{key}: quote must occur in original text')
        elif values and not quote_matches(values.quote, text):
            setattr(voice, key, None)
            problems.append(f'voice.{key}: quote must occur in original text')
    settings = [c for c in reader.setting_conflicts if quote_matches(c.quote, text)]
    if len(settings) != len(reader.setting_conflicts):
        problems.append('setting_conflicts: quote must occur in original text')
    return reader.model_copy(update={'items': items, 'mentions': mentions, 'profile': profile, 'conflicts': conflicts,
                                     'voice': voice, 'setting_conflicts': settings}), problems


def apply_reader_voice(package, reader, input_data):
    """Project quoted LLM field assignments, without interpreting their meaning."""
    voice = package.expression.voice
    blocked = [r.conflict.quote for r in input_data.conflict_resolutions if r.choice == 'answer']
    def usable(value):
        return value and not any(quote_matches(q, value.quote) or quote_matches(value.quote, q) for q in blocked)
    mappings = [('first_person', voice, 'first_person'),
                ('second_person_default', voice.second_person, 'default'),
                ('second_person_when_needed', voice.second_person, 'when_needed'),
                ('speech_register', voice, 'speech_register'),
                ('length_default', voice.length, 'default'),
                ('length_expand_when', voice.length, 'expand_when')]
    typed = {'first_person': input_data.basics.first_person, 'second_person_default': input_data.basics.second_person,
             'speech_register': input_data.basics.speech_register, 'length_default': input_data.basics.length}
    for key, owner, target in mappings:
        value = getattr(reader.voice, key)
        if typed.get(key):
            setattr(owner, target, typed[key])
        elif usable(value):
            setattr(owner, target, value.value)
    endings = [v.value for v in reader.voice.sentence_endings if usable(v)]
    if endings:
        voice.sentence_endings = endings
    return package


def unresolved_conflicts(input_data, reader, questionnaire, source_text=None):
    decoded = decode_answers(input_data, questionnaire)
    selected = set()
    for resolution in input_data.conflict_resolutions:
        c = resolution.conflict
        answer = decoded.get(c.question_ref)
        key = (c.question_ref, normalize_quote(c.quote), tuple(c.answer_keys))
        if (key in selected or not quote_matches(c.quote, source_text if source_text is not None else input_data.text or '')
                or not answer or answer['answer'] != c.answer_keys or answer['open']):
            raise ValueError('食い違いの選択が現在の設定文・回答と一致しません。もう一度確認してください。')
        selected.add(key)
    return [c for c in reader.conflicts
            if (c.question_ref, normalize_quote(c.quote), tuple(c.answer_keys)) not in selected]


def answer_selected_text_refs(input_data, reader):
    # Only quotation correspondence; the LLM decides the replacement content.
    return sorted({item.id for r in input_data.conflict_resolutions if r.choice == 'answer'
                   for item in reader.items
                   if quote_matches(r.conflict.quote, item.quote) or quote_matches(item.quote, r.conflict.quote)})


class ReaderCache:
    """Session-local, bounded cache of successful exact-input Reader results."""
    def __init__(self, limit=8):
        self.items, self.lock, self.limit = OrderedDict(), Lock(), limit

    def get(self, key):
        with self.lock:
            value = self.items.get(key)
            if value is not None:
                self.items.move_to_end(key)
                return value.model_copy(deep=True)

    def put(self, key, value):
        with self.lock:
            self.items[key] = value.model_copy(deep=True)
            self.items.move_to_end(key)
            while len(self.items) > self.limit:
                self.items.popitem(last=False)


class Bootstrapper:
    def __init__(self, config, client, *, characters_root=None, store=None, trace_root=None, on_stage=None, reader_cache=None):
        self.config, self.client = config, client
        self.characters_root = Path(characters_root) if characters_root is not None else config._characters_root
        self.trace_root = Path(trace_root) if trace_root is not None else config._data_dir / "bootstrap"
        self.store = store
        if store is not None:
            from cbs.costs import bind
            bind(client, store, config)
        self.questionnaire = load_questionnaire()
        self.traces = []
        self.warnings = []
        self.on_stage = on_stage or (lambda stage: None)
        self.reader_cache = reader_cache

    def call(self, prompt, context, schema, *, judge=False):
        self.on_stage({'bootstrap_reader': 'reading', 'bootstrap_adaptive': 'questions',
                       'bootstrap_reader_review': 'reading',
                       'bootstrap_builder': 'building', 'bootstrap_summary': 'review'}.get(prompt, 'validation'))
        try:
            model = self.config.models.judge if judge else self.config.models.steward
            context = dict(context)
            for key in ('questionnaire', 'allowed_parents'):
                if key in context:
                    context[key] = questionnaire_named(context[key], context.get('input', {}).get('basics', {}).get('name') or '')
            messages = [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
            def once():
                return for_step(self.client, 'bootstrap', 'welcome').generate_json(model, read_prompt(prompt), messages, schema)
            pipeline_cfg = getattr(self.config, 'pipeline', None)
            hedge = getattr(pipeline_cfg, 'bootstrap_hedge_seconds', 0)
            result = hedged(once, hedge) if hedge and prompt in getattr(pipeline_cfg, 'bootstrap_hedge_prompts', []) else once()
            self.traces.append(result.trace())
            return result.parsed
        except LLMError as error:
            self.traces.append(error.trace or {'error': str(error)})
            raise

    def save_trace(self, operation, cid):
        self.trace_root.mkdir(parents=True, exist_ok=True)
        path = self.trace_root / f'{operation}_{cid}_{clock.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.json'
        path.write_text(self.client.sanitize_text(json.dumps(self.traces, ensure_ascii=False, indent=2)), encoding='utf-8')
        self.traces.clear()
        return path

    def read(self, input_data):
        decoded = decode_answers(input_data, self.questionnaire)
        sources = self.source_context(input_data)
        cache_input = {'sources':sources, 'decoded':decoded,
                       'resolutions':[r.model_dump() for r in input_data.conflict_resolutions],
                       'world':input_data.world.model_dump(exclude_none=True),
                       'ai_question':self.persona(input_data).ai_question, 'review_notes':input_data.review_notes,
                       'models':[self.config.models.judge,self.config.models.steward],
                       'reasoning':{k:v.reasoning_effort for k,v in self.config.llm.providers.items()},
                       'questionnaire':self.questionnaire,
                       'prompts':[read_prompt('bootstrap_reader'),read_prompt('bootstrap_reader_review')],
                       'schemas':[ReaderOutput.model_json_schema(),ReaderReview.model_json_schema()]}
        key = hashlib.sha256(json.dumps(cache_input,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if self.reader_cache is not None:
            cached = self.reader_cache.get(key)
            if cached is not None:
                self.traces.append({'reader_cache':'exact input hit', 'key':key})
                return cached
        warnings_before = len(self.warnings)
        result = self.read_text(sources['text'], decoded, input_data.conflict_resolutions,
                              world=input_data.world.model_dump(exclude_none=True),
                              ai_question=self.persona(input_data).ai_question,
                              review_notes=input_data.review_notes, source_context=sources)
        if self.reader_cache is not None and len(self.warnings) == warnings_before:
            self.reader_cache.put(key,result)
        return result

    def source_context(self, input_data):
        from cbs.import_profiles import partner_sources
        messages = partner_sources(self.store, input_data.id, input_data.import_batch_ids) if input_data.import_batch_ids else []
        texts = ([input_data.text] if input_data.text else []) + [m['text'] for m in messages]
        return {'text': '\n\n'.join(texts), 'quote_sources': texts,
                'setting_text': input_data.text or '', 'imported_partner_messages': messages}

    def persona(self, input_data):
        if input_data.persona is not None:
            return PersonaConfig.model_validate(input_data.persona.model_dump())
        if (self.characters_root / input_data.id / 'model.yaml').is_file():
            return effective_persona(load_character(input_data.id, root=self.characters_root), self.config)
        return PersonaConfig()

    def read_text(self, text, decoded=None, resolutions=None, *, world=None, ai_question=None, review_notes=None, source_context=None):
        self.on_stage('reading')
        if not text:
            return ReaderOutput()
        context = {'text': text, 'decoded_answers': decoded or {}, 'catalog': domain_catalog(),
                   'conflict_resolutions': [r.model_dump() for r in resolutions or []],
                   'world': world or {}, 'ai_question': ai_question or self.config.persona.ai_question,
                   'review_notes': review_notes or {}, **(source_context or {})}
        quote_sources = (source_context or {}).get('quote_sources', text)
        reader, problems = None, []
        for attempt in range(2):
            try:
                reader = self.call('bootstrap_reader', {**context, 'validation_problems': problems,
                                   'previous_reader': reader.model_dump() if reader else None}, ReaderOutput)
            except LLMError as error:
                if attempt:
                    raise
                problems = schema_problems(error, ReaderOutput)
                continue
            filtered, problems = checked_reader(reader, quote_sources, self.questionnaire, decoded)
            if not problems:
                break
        self.warnings.extend(problems)
        # Unverifiable conflicts are dropped like other invalid entries; the plan goes on with the default priority.
        if problems:
            self.traces.append({'warnings': problems, 'action': 'Dropped invalid Reader entries after one retry'})
        # A separate, bounded pass checks voice and all concrete answers, not just
        # domains that happened to attract the general Reader's attention.
        review, problems = None, []
        for attempt in range(2):
            try:
                review = self.call('bootstrap_reader_review', {**context, 'reader': filtered.model_dump(by_alias=True),
                    'previous_review': review.model_dump(by_alias=True) if review else None,
                    'validation_problems': problems}, ReaderReview, judge=True)
            except LLMError as error:
                if attempt:
                    raise
                problems = schema_problems(error, ReaderReview)
                continue
            # IDs are assigned by code so additions cannot collide with Reader IDs.
            additions, used = [], {item.id for item in filtered.items}
            revisions, revision_problems = {}, []
            for item in review.revised_items:
                if item.id not in used or item.id in revisions:
                    revision_problems.append('revised_items: use each existing Reader item ID at most once')
                else:
                    revisions[item.id] = item
            next_id = 1
            for item in review.missing_items:
                while f'text:review:{next_id}' in used:
                    next_id += 1
                ref = f'text:review:{next_id}'
                used.add(ref)
                additions.append(item.model_copy(update={'id': ref}))
            merged = filtered.model_copy(update={'items': [revisions.get(i.id,i) for i in filtered.items] + additions,
                'mentions': review.mentions if review.mentions is not None else filtered.mentions,
                'voice': review.voice, 'conflicts': review.conflicts,
                'setting_conflicts': review.setting_conflicts})
            checked, problems = checked_reader(merged, quote_sources, self.questionnaire, decoded)
            problems = revision_problems + problems
            if not problems:
                return checked
        # A failed completeness check must not quietly create an incomplete partner.
        raise ValueError('Reader review failed after one fix: ' + ' / '.join(problems))

    def profile(self, cid, text, *, persona=None, import_batch_ids=None):
        try:
            data = BootstrapInput(id=cid, basics={'name': cid}, text=text, persona=persona, import_batch_ids=import_batch_ids or [])
            return {'reader': self.read(data).model_dump(), 'warnings': self.warnings}
        finally:
            self.save_trace('read', cid)

    def plan(self, input_data):
        try:
            reader = self.read(input_data)
            decoded = decode_answers(input_data, self.questionnaire)
            return {'reader': reader.model_dump(), 'coverage': coverage_map(input_data, reader, self.questionnaire),
                    'questions': gap_questions(input_data, reader, self.questionnaire),
                    'confirmation_required': [{**c.model_dump(), 'question': decoded[c.question_ref]['question'],
                                               'choices': decoded[c.question_ref]['choices'], 'answer_text': decoded[c.question_ref]['text']}
                                              for c in unresolved_conflicts(input_data, reader, self.questionnaire, self.source_context(input_data)['quote_sources'])],
                    'warnings': self.warnings}
        finally:
            self.save_trace('plan', input_data.id)

    def prior_answers(self, cid):
        prior = (self.store if self.store is not None else Store()).bootstrap_answers(cid)
        source = self.characters_root / cid / 'source' / 'input.json'
        if source.is_file():
            original = BootstrapInput.model_validate_json(source.read_text(encoding='utf-8'))
            prior = {**decode_answers(original, self.questionnaire), **prior}
        return prior

    def adaptive(self, input_data, *, existing_id=None):
        try:
            reader = self.read(input_data)
            prior, existing = {}, {}
            if existing_id is not None:
                character = load_character(existing_id, root=self.characters_root)
                store = self.store if self.store is not None else Store()
                prior = self.prior_answers(existing_id)
                existing = {'existing_character': store.current_model(character).model_dump(mode='json'),
                            'author_sources': store.author_sources(character)}
            used = {ref.removeprefix('author:') for ref in prior if ref.startswith('author:AQ')}
            used |= {q.id for q in input_data.adaptive_questions}
            start = max([int(qid[2:]) for qid in used] or [0]) + 1
            context = {'input': input_data.model_dump(mode='json', by_alias=True), 'reader': reader.model_dump(),
                       'decoded_answers': {**prior, **decode_answers(input_data, self.questionnaire)},
                       'questionnaire': self.questionnaire, 'catalog': domain_catalog(),
                       'allowed_life_refs': [f'life:{qid}' for qid in questions_by_group(self.questionnaire)['life']],
                       'allowed_ids': [f'AQ{i}' for i in range(start, start + 4)], **existing}
            parents = adaptive_parents(context['decoded_answers'], self.questionnaire)
            context['allowed_parents'] = parents
            if not parents:
                return {'questions': [], 'parents': {}}
            # Preserve full definitions of all answered questions, including
            # open choices. Unanswered questions cannot be adaptive parents.
            decoded_refs = set(context['decoded_answers'])
            context['questionnaire'] = {'common_choices':self.questionnaire['common_choices'],
                'answered_questions': {f'{"author" if group=="answers" else group}:{qid}':q
                    for group, questions in questions_by_group(self.questionnaire).items()
                    for qid,q in questions.items()
                    if f'{"author" if group=="answers" else group}:{qid}' in decoded_refs}}
            output, problems = None, []
            for attempt in range(2):
                output = self.call('bootstrap_adaptive', {**context, 'validation_problems': problems,
                                   'previous_questions': output.model_dump(mode='json') if output else None}, AdaptiveOutput)
                self.on_stage('validation')
                if any(q.id not in context['allowed_ids'] for q in output.questions):
                    raise ValueError('Adaptive question ID must use the supplied unused IDs')
                validate_adaptive_parents(output.questions, parents)
                if not output.questions:
                    problems = []
                    break
                judgment = self.call('bootstrap_adaptive_validate', {**context, 'questions': output.model_dump(mode='json')},
                                     BootstrapJudgment, judge=True)
                problems = judgment.problems
                if not problems:
                    break
            if problems:
                raise ValueError('Adaptive validation failed after one fix: ' + ' / '.join(problems))
            return {**output.model_dump(mode='json'), 'parents': parents}
        finally:
            self.save_trace('adaptive', input_data.id)

    def create(self, input_data, *, force=False, raw_input=None, review_base=None):
        directory = self.characters_root / input_data.id
        if directory.exists() and not force:
            raise FileExistsError(f'{directory} already exists; use --force to archive it')
        try:
            validate_adaptive_parents(input_data.adaptive_questions,
                                      adaptive_parents(decode_answers(input_data, self.questionnaire), self.questionnaire))
            now_questions = questions_by_group(self.questionnaire)['now']
            if any(contact_mismatch(value, now_questions[qid], input_data.world.contact) for qid, value in input_data.now.life.items()):
                raise ValueError('今の暮らしの答えが「あなたとの接点」と合っていません。答え直してください。')
            reader = self.read(input_data)
            if (unresolved_conflicts(input_data, reader, self.questionnaire, self.source_context(input_data)['quote_sources'])
                    or input_data.import_batch_ids and reader.setting_conflicts):
                self.on_stage('validation')
                raise ValueError('作者の確認が必要です。食い違いごとに使う内容を選んでください。')
            creation_date = clock.now().date()
            context = {'mode': 'create', 'input': input_data.model_dump(mode='json', by_alias=True), 'reader': reader.model_dump(),
                       'review_base': review_base,
                       'overridden_text_refs': answer_selected_text_refs(input_data, reader),
                       'catalog': domain_catalog(), 'decoded_answers': decode_answers(input_data, self.questionnaire),
                       'valid_evidence': evidence_catalog(input_data, reader, self.questionnaire),
                       'creation_date': creation_date.isoformat(),
                       'allowed_seed_dates': [(creation_date - timedelta(days=i)).isoformat() for i in range(1, 29)]}
            if input_data.import_batch_ids:
                context['imported_partner_messages'] = self.source_context(input_data)['imported_partner_messages']
                context['already_told_past'] = self.store.list_canon(input_data.id)
            package = None
            problems = []
            judged = False
            # A schema failure also consumes the one permitted Builder fix round.
            for attempt in range(2):
                try:
                    package = self.call('bootstrap_builder', {**context, 'validation_problems': problems,
                                        'previous_package': package.model_dump(mode='json', by_alias=True) if package else None}, CharacterPackage)
                    package = apply_reader_voice(package, reader, input_data)
                    drop_open_refs(package, context['valid_evidence'])
                    narrow_domain_evidence(package.model.domains, input_data, reader, self.questionnaire)
                    settle(package.model.domains, package.model.signatures, context['valid_evidence'])
                    self.on_stage('validation')
                    problems = validate_package(package, input_data, reader, self.questionnaire, creation_date)
                except LLMError as error:
                    if attempt:
                        raise
                    problems = schema_problems(error, CharacterPackage)
                    continue
                if not judged or input_data.import_batch_ids:
                    judgment = self.call('bootstrap_validate', {**context, 'package': package.model_dump(mode='json', by_alias=True)}, BootstrapJudgment, judge=True)
                    problems += judgment.problems
                    judged = True
                if not problems:
                    break
            if problems or package is None:
                raise ValueError('Bootstrap validation failed after one fix: ' + ' / '.join(problems))
            summary = self.call('bootstrap_summary', {**context, 'package': package.model_dump(mode='json', by_alias=True)}, BootstrapSummary)
            summary.setting_conflicts = reader.setting_conflicts
            self.on_stage('validation')
            self.on_stage('saving')
            self.write(package, input_data, summary, creation_date, force=force, raw_input=raw_input)
            return directory
        finally:
            self.save_trace('create', input_data.id)

    def write(self, package, input_data, summary, creation_date, *, force=False, raw_input=None):
        self.characters_root.mkdir(parents=True, exist_ok=True)
        directory = self.characters_root / input_data.id
        if directory.exists() and not force:
            raise FileExistsError(directory)
        # Finish every artifact before touching an old package; all moves stay under characters_root.
        stage = self.characters_root / f'bootstrap_{uuid4().hex}'
        stage.mkdir()
        try:
            data = package.model_dump(mode='json', by_alias=True)
            for name, value in data.items():
                (stage / f'{name}.yaml').write_text(self.client.sanitize_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False)), encoding='utf-8')
            write_persona(stage, self.persona(input_data))
            (stage / 'bootstrap_meta.yaml').write_text(yaml.safe_dump({'created_at': clock.iso(), 'creation_date': creation_date.isoformat()}, sort_keys=False), encoding='utf-8')
            (stage / 'confirmation.yaml').write_text(self.client.sanitize_text(yaml.safe_dump(summary.model_dump(mode='json'), allow_unicode=True, sort_keys=False)), encoding='utf-8')
            source = stage / 'source'
            source.mkdir()
            (source / 'input.json').write_text(self.client.sanitize_text(raw_input if raw_input is not None else input_data.model_dump_json(indent=2, by_alias=True)), encoding='utf-8')
            (source / 'profile.md').write_text(self.client.sanitize_text(input_data.text or ''), encoding='utf-8')
            (source / 'answers.md').write_text(self.client.sanitize_text(readable_answers(input_data, self.questionnaire)), encoding='utf-8')
            lines = [f'# {package.model.display_name} — 作成結果', '', summary.introduction, '', '## 反応の余白', '']
            lines += [f'ここは決めずに残してあり、話していくうちに{input_data.basics.name or "あなたのパートナー"}が自分で決めていきます。', '']
            lines += [f'- {d.description}' for d in package.model.domains if d.status == 'open']
            lines += ['', '## 明かされていない過去', ''] + [f'- {s.about}' for s in package.reveal.secrets]
            lines += ['', '## 今の余白', ''] + [f'- {s}' for s in dict.fromkeys(summary.open_now + package.expression.existence.open)]
            (stage / 'summary.md').write_text(self.client.sanitize_text('\n'.join(lines) + '\n'), encoding='utf-8')
            # Verify runtime compatibility before publishing the package.
            load_character(stage.name, root=stage.parent)
            if directory.exists():
                archive = directory / '_history' / f'{clock.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}'
                archive.mkdir(parents=True)
                for path in list(directory.iterdir()):
                    if path.name != '_history':
                        path.rename(archive / path.name)
            else:
                directory.mkdir()
            for path in list(stage.iterdir()):
                path.rename(directory / path.name)
        finally:
            import shutil
            if stage.resolve().parent != self.characters_root.resolve():
                raise ValueError('Staging directory escaped characters root')
            shutil.rmtree(stage)

    def augment(self, cid, answers, *, adaptive_questions=None, adaptive_answers=None, raw_input=None):
        character = load_character(cid, root=self.characters_root)
        store = self.store if self.store is not None else Store()
        input_data = BootstrapInput(id=cid, basics={'name': character.model.display_name}, answers=answers,
                                    adaptive_questions=adaptive_questions or [], adaptive_answers=adaptive_answers or {})
        decoded = decode_answers(input_data, self.questionnaire)
        prior = self.prior_answers(cid)
        validate_adaptive_parents(input_data.adaptive_questions,
                                  adaptive_parents({**prior, **decoded}, self.questionnaire))
        if any(ref in prior and prior[ref] != value for ref, value in decoded.items()):
            raise ValueError('augment cannot overwrite prior author answers')
        new = {ref: value for ref, value in decoded.items() if ref not in prior}
        requested = {value['domain'] for value in new.values() if value['domain'] in {d['id'] for d in domain_catalog()}}
        if not new:
            return {'domains': [], 'version_id': None}
        # Fixed answers cannot replace author-backed domains; adaptive answers refine their conditions.
        model = store.current_model(character)
        existing = {d.id: d for d in model.domains}
        fixed_domains = {value['domain'] for ref, value in new.items() if not ref.startswith('author:AQ')}
        if any(existing[did].evidence and any(ref.startswith('author:') for ref in existing[did].evidence) for did in fixed_domains):
            raise ValueError('augment cannot replace an existing author-backed domain')
        reader = ReaderOutput()
        valid = {**prior, **evidence_catalog(input_data, reader, self.questionnaire)}
        for did in requested:
            for ref in existing[did].evidence:
                valid.setdefault(ref, {'open': False, 'existing_domain': did})
        try:
            context = {'mode': 'augment', 'input': input_data.model_dump(mode='json', by_alias=True), 'decoded_answers': decoded,
                       'valid_evidence': valid, 'prior_answers': prior, 'existing_character': model.model_dump(mode='json'),
                       'requested_domains': sorted(requested), 'author_sources': store.author_sources(character)}
            output = None
            problems = []
            judged = False
            for attempt in range(2):
                try:
                    output = self.call('bootstrap_builder', {**context, 'validation_problems': problems,
                                       'previous_package': output.model_dump() if output else None}, AugmentOutput)
                    drop_open_refs(output, valid)
                    narrow_domain_evidence(output.domains, input_data, reader, self.questionnaire, existing)
                    settle(output.domains, [], valid)
                    problems = validate_domains(output.domains, input_data, reader, self.questionnaire, valid,
                                                prior_domains=existing)
                    if {d.id for d in output.domains} != requested or len(output.domains) != len(requested):
                        problems.append('augment must return requested domains exactly once')
                    if any(set(d.related_signatures) - {s.id for s in model.signatures} for d in output.domains):
                        problems.append('augment references an unknown signature')
                except LLMError as error:
                    if attempt:
                        raise
                    problems = schema_problems(error, AugmentOutput)
                    continue
                if not judged:
                    judgment = self.call('bootstrap_validate', {**context, 'package': output.model_dump()}, BootstrapJudgment, judge=True)
                    problems += judgment.problems
                    judged = True
                if not problems:
                    break
            if problems or output is None:
                raise ValueError('Augment validation failed after one fix: ' + ' / '.join(problems))
            changes = []
            for domain in output.domains:
                before = existing[domain.id].model_dump(mode='json')
                # Learned experience, extra runtime fields and existing labels remain intact.
                after = {**before, **domain.model_dump(), 'label': before['label']}
                after['evidence'] = list(dict.fromkeys(before.get('evidence', []) + after['evidence']))
                if before.get('known') and before['known'] != after['known']:
                    after['learned'] = [*before.get('learned', []), {'text': before['known'], 'conditions': None,
                                       'episode_refs': [ref for ref in before.get('evidence', []) if ref.startswith('episode:')]}]
                changes.append({'collection': 'domains', 'target': domain.id, 'before': before, 'after': after})
            with store.connect() as db:
                row = db.execute('INSERT INTO model_versions (character_id, base_version, diff_json, rationale, episode_refs_json, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                                 (cid, model.version, self.client.sanitize_text(json.dumps({'changes': changes}, ensure_ascii=False)), 'Bootstrap v2 author augment', '[]', clock.iso()))
                db.execute('INSERT INTO bootstrap_augments (character_id, input_json, answers_json, readable_md, model_version_id, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                           (cid, pack_trace(self.client.sanitize_text(raw_input or input_data.model_dump_json(by_alias=True))), self.client.sanitize_text(json.dumps(decoded, ensure_ascii=False)),
                            self.client.sanitize_text(readable_answers(input_data, self.questionnaire)), row.lastrowid, clock.iso()))
            return {'domains': sorted(requested), 'version_id': row.lastrowid}
        finally:
            self.save_trace('augment', cid)


def readable_answers(input_data, questionnaire):
    lines = ['# 作者入力（Bootstrap v2）', '', f'- 名前: {input_data.basics.name}', '', '## 回答', '']
    for ref, answer in decode_answers(input_data, questionnaire).items():
        lines += [f'### {ref}', '', answer['question'], '', ' / '.join(answer['choices']),
                  answer.get('text') or '', f'人物の対応: {answer["relationship_grouping"]}' if answer.get('relationship_grouping') else '', '']
        if 'purpose' in answer:
            lines += [f'参照: {answer["domain"]}', f'親の質問: {answer["parent_question"]}',
                      f'追加条件: {answer["added_condition"]}', f'確かめる境目: {answer["purpose"]}', '']
            lines += [f'- {key}: {text}' for key, text in answer['question_choices'].items()] + ['']
    lines += ['## 今の好み', ''] + [f'- {like.topic}: {like.detail or like.topic} ({like.elaborate or input_data.elaborate.get("default", "may_elaborate")})' for like in input_data.now.likes]
    lines += ['', '## 設定の具体化', '', json.dumps(input_data.elaborate, ensure_ascii=False)]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description='Bootstrap v2')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'create', 'adaptive'):
        sub = commands.add_parser(name)
        sub.add_argument('input', type=Path)
        if name == 'create':
            sub.add_argument('--force', action='store_true')
        if name == 'adaptive':
            sub.add_argument('--output', type=Path, help='保存先（既定: data/bootstrap/adaptive_<id>.json）')
    sub = commands.add_parser('augment')
    sub.add_argument('id')
    sub.add_argument('input', type=Path)
    sub.add_argument('--db', type=Path, default=resolve_paths()[0] / 'cbs.sqlite3')
    args = parser.parse_args()
    config = load_config()
    try:
        bootstrap = Bootstrapper(config, create_client(config), store=Store(args.db) if args.command == 'augment' else None)
        raw = args.input.read_text(encoding='utf-8')
        data = json.loads(raw)
        if args.command == 'augment':
            answers = data.get('answers', {} if 'adaptive_questions' in data or 'adaptive_answers' in data else data)
            result = bootstrap.augment(args.id, answers, adaptive_questions=data.get('adaptive_questions'),
                                       adaptive_answers=data.get('adaptive_answers'), raw_input=raw)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            input_data = BootstrapInput.model_validate(data)
            if args.command == 'plan':
                print(json.dumps(bootstrap.plan(input_data), ensure_ascii=False, indent=2))
            elif args.command == 'adaptive':
                result = bootstrap.adaptive(input_data)
                saved = input_data.model_dump(mode='json', by_alias=True)
                saved.update(adaptive_questions=result['questions'], adaptive_answers={})
                path = args.output or resolve_paths()[0] / 'bootstrap' / f'adaptive_{input_data.id}.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(bootstrap.client.sanitize_text(json.dumps(saved, ensure_ascii=False, indent=2)), encoding='utf-8')
                print(json.dumps(result, ensure_ascii=False, indent=2))
                print(f'saved: {path}')
            else:
                print(f'created: {bootstrap.create(input_data, force=args.force, raw_input=raw)}')
    except (ValueError, FileExistsError, LLMError) as error:
        print(f'Bootstrap failed: {error}')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
