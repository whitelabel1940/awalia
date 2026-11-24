"""Finite Decision answers; request bodies and provider errors never enter traces."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from contextvars import ContextVar
import json
import math
import ssl
from time import perf_counter

import httpx

from cbs.llm.cost import report_usage
from cbs.llm.errors import LLMError

# Shadow dispatches batches serially and accounts for each provider request.
request_guard = ContextVar('decision_request_guard', default=None)


class DecisionError(LLMError):
    def __init__(self, kind, *, trace=None, status=None):
        super().__init__('場面の読み取りの接続を確認できませんでした。',
                         trace={**(trace or {}), 'failure_kind': kind}, status_code=status)


def _probabilities(answer, labels):
    p = answer.get('probabilities')
    if not isinstance(p, dict) or set(p) != set(labels):
        raise DecisionError('answer_labels')
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in p.values()):
        raise DecisionError('answer_probability')
    if abs(sum(p.values()) - 1) > .03:
        raise DecisionError('answer_sum')
    return max(p, key=p.get)


def decode(question, answer):
    if not isinstance(answer, dict) or answer.get('type') != question['type']:
        raise DecisionError('answer_type')
    if question['type'] == 'noul':
        p = answer.get('noul')
        if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
            raise DecisionError('answer_noul')
        return p >= .5
    labels = question['criteria'] if question['type'] == 'choice' else [str(i) for i in range(len(question['criteria']))]
    winner = _probabilities(answer, labels)
    if question['type'] == 'choice' and answer.get('choice') != winner:
        selected = answer.get('choice')
        if selected not in labels or answer['probabilities'][selected] != answer['probabilities'][winner]:
            raise DecisionError('answer_choice')
        winner = selected
    return winner


@dataclass
class DecisionResult:
    model: str
    values: dict
    usage: dict
    batches: list[dict]
    latency_ms: float
    raw: dict = None

    def trace(self):
        return {'model': self.model, 'question_count': len(self.values), 'request_count': len(self.batches),
                'batches': self.batches, 'usage': self.usage, 'latency_ms': self.latency_ms,
                'answers': self.values}


def run_decision(model, questions, send):
    """The fake and HTTP adapters share batching, validation and numeric accounting."""
    started = perf_counter()
    google = model.startswith('gemini/')
    size = 1000 if google else 32
    items = list(questions.items())
    batches = [dict(items[i:i+size]) for i in range(0, len(items), size)]

    def one(batch):
        at = perf_counter()
        info = {'questions': list(batch)}
        values, data, answers = {}, {}, {}
        try:
            body = send(batch)
            raw_usage = body.get('usage', {})
            token_key = 'total_input_tokens' if google else 'input_tokens'
            data = {k: v for k, v in {'input_tokens': raw_usage.get(token_key),
                    'cost': raw_usage.get('cost') if not google else None}.items()
                    if type(v) in (int, float) and math.isfinite(v) and v >= 0}
            answers = body['answers']
            if not isinstance(answers, dict) or set(answers) != set(batch):
                raise DecisionError('answer_names')
            values = {name: decode(q, answers[name]) for name, q in batch.items()}
            info['answers'] = values
        except DecisionError as error:
            info['failure_kind'] = error.trace['failure_kind']
            if error.status_code:
                info['status_code'] = error.status_code
        except (httpx.TimeoutException, TimeoutError):
            info['failure_kind'] = 'timeout'
        except (httpx.TransportError, ConnectionError, OSError):
            info['failure_kind'] = 'transport'
        except Exception:
            info['failure_kind'] = 'answer_schema'
        info['latency_ms'] = round((perf_counter()-at)*1000, 2)
        return values, data, info, answers

    guard = request_guard.get()
    if guard is not None:
        results = []
        for batch in batches:
            guard(model)
            results.append(one(batch))
            if 'failure_kind' in results[-1][2]:
                break  # Shadow defers this record instead of sending more after a failed batch.
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(one, batches))
    usage = {key: sum(data[key] for _, data, _, _ in results if key in data)
             for key in ('input_tokens', 'cost') if any(key in data for _, data, _, _ in results)}
    # Incomplete cost metadata is unknown, rather than an understated total.
    if not google and any('cost' not in data for _, data, _, _ in results):
        usage.pop('cost', None)
    report_usage(usage)
    result = DecisionResult(model, {k: v for vals, _, _, _ in results for k, v in vals.items()}, usage,
                            [info for _, _, info, _ in results], round((perf_counter()-started)*1000, 2),
                            {k: v for _, _, _, answers in results if isinstance(answers, dict) for k, v in answers.items()})
    failed = next((info for info in result.batches if 'failure_kind' in info), None)
    if failed:
        error = DecisionError(failed['failure_kind'], trace={**result.trace(), 'question_count': len(questions)},
                              status=failed.get('status_code'))
        error.raw = result.raw
        raise error
    return result


class DecisionHTTPClient:
    def __init__(self, config, provider, *, api_key=None):
        import os
        from cbs.config import key_env
        self.provider = provider
        self.key = api_key if api_key is not None else os.environ.get(key_env(config, provider) or '', '')
        if not self.key:
            raise DecisionError('auth')
        if provider == 'openrouter':
            base = config.llm.providers[provider].base_url or 'https://openrouter.ai/api/v1'
            self.endpoint = base.rstrip('/') + '/systemone'
        else:
            self.endpoint = 'https://generativelanguage.googleapis.com/v1beta/decisions'
        context = ssl.create_default_context()
        if not config.llm.tls_strict:
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.http = httpx.Client(verify=context, timeout=120, follow_redirects=False)

    def sanitize_text(self, text):
        return text.replace(self.key, '[REDACTED]') if self.key else text

    def decide(self, model, input_text, questions):
        def send(batch):
            google = self.provider == 'gemini'
            payload = {'model': model.split('/', 1)[1], 'questions': batch,
                       **({'input': input_text} if google else {'state': json.loads(input_text)})}
            response = self.http.post(self.endpoint,
                headers={'x-goog-api-key': self.key} if google else {'Authorization': 'Bearer ' + self.key},
                json=payload)
            if response.status_code != 200:
                raise DecisionError('http', status=response.status_code)
            return response.json()
        return run_decision(model, questions, send)
