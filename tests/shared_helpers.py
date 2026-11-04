"""Helpers used by several test modules.

They live here rather than in the test that first needed them, because some of those
tests also drive development-only benchmark and probe folders and are left out of the
public repository (R8); the tests that borrow these helpers must still run there.
"""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cbs import clock
from cbs.llm import FakeLLMClient
from cbs.llm.openai_client import OpenAILLMClient
from cbs.steward import StewardOutput, apply_output
from cbs.web import create_app
from test_steward import episode, update


def api(pipeline, tmp_path):
    root = tmp_path / "characters"
    (root / "fixture").mkdir(parents=True)
    return TestClient(create_app(pipeline, characters_root=root, settings_dir=tmp_path / "settings"),
                      base_url="http://127.0.0.1:9987")


class ScriptedTransport(FakeLLMClient):
    """In-memory stand-in for SDK response objects; provider constructors stay forbidden."""
    def __init__(self):
        super().__init__()
        self.options = []
        self.chat = SimpleNamespace(completions=self)
        self.embeddings = SimpleNamespace(create=lambda **kw: SimpleNamespace(data=[
            SimpleNamespace(index=1, embedding=[0., 1.]), SimpleNamespace(index=0, embedding=[1., 0.])]))
        self.content = 'answer synthetic-secret'
        self.annotations = [{"type": "url_citation", "url_citation": {"url": "https://example.org/article", "title": "Article"}}]

    def create(self, **kwargs):
        self.options.append(kwargs)
        if kwargs.get('stream'):
            class Stream(list):
                def close(self):
                    self.closed = True
            return Stream([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='hello'))], usage=None)])
        message = SimpleNamespace(content=self.content, model_dump=lambda **kw: {'annotations': self.annotations})
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)


def adapter():
    client = OpenAILLMClient.__new__(OpenAILLMClient)
    client._zdr = False
    client.name, client._api, client._key = 'openrouter', 'chat', 'synthetic-secret'
    client._reasoning = {'judge': 'low', 'actor': 'low', 'steward': 'low'}
    client._client = ScriptedTransport()
    return client


def record(p, day='2026-10-19', domain='CBS10', mode='enacted', updates=()):
    with clock.at(day + 'T23:59:00+09:00'):
        sid = p.store.create_session(p.config.character.id)['id']
        tid = p.store.add_turn(sid, '好き', 'どうして？', {})
        apply_output(p.store, p.character, tid, StewardOutput(episodes=[episode(mode, domain)], updates=list(updates)))
    return tid


def trait_version(p, refs, *, target='new_signature', day='2026-10-21'):
    with clock.at(day + 'T12:00:00+09:00'):
        tid = p.store.add_turn(p.store.create_session(p.config.character.id)['id'], '話してくれてありがとう', 'うん', {})
        signature = {'id': 'sig_learned', 'when': '親しい相手の気持ち', 'tends_to': '理由を聞く'} if target == 'new_signature' else None
        status = 'tentative' if target == 'new_signature' else next(s.status for s in p.store.current_model(p.character).signatures if s.id == target)
        result = apply_output(p.store, p.character, tid, StewardOutput(episodes=[], updates=[update(
            status, refs, target, interpretation='differentiation', signature=signature)]))
        assert result['applied']
    return tid


def payload(ident='test', at='2026-04-01T20:00:00+09:00', text='昔の話', assistant='ボクの言葉'):
    return {'id': ident, 'metadata': {'title': ident, 'dates': {'created': at}}, 'messages': [
        {'role': 'Prompt', 'say': text, 'time': at}, {'role': 'Response', 'say': assistant, 'time': at}]}


def preview(jobs, pipeline, entries):
    p = jobs.preview(pipeline, 'export.json', json.dumps(entries).encode())
    q = jobs.quote(pipeline, p['preview_id'], [c['key'] for c in p['conversations']])
    return p, q


def run(jobs, pipeline, q):
    jobs.start(pipeline, q['token'], True)
    jobs.futures[q['token']].result(timeout=20)
    return pipeline.store.import_status('fixture', q['token'])
