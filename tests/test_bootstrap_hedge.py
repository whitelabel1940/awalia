"""The welcome flow's long Builder call is re-sent once if the provider stalls."""
import threading
import time

import pytest

from cbs.bootstrap import hedged
from cbs.config import load_config


def test_fast_call_is_sent_once():
    calls = []
    def call():
        calls.append(1)
        return 'ok'
    assert hedged(call, 0.5) == 'ok'
    assert len(calls) == 1


def test_stalled_call_is_hedged_and_first_answer_wins():
    calls, release = [], threading.Event()
    def call():
        calls.append(1)
        if len(calls) == 1:
            release.wait(5)  # the stalled first attempt
            return 'late'
        return 'fast'
    started = time.monotonic()
    assert hedged(call, 0.1) == 'fast'
    assert time.monotonic() - started < 2
    assert len(calls) == 2
    release.set()


def test_one_failure_waits_for_the_other_attempt():
    calls = []
    def call():
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.3)
            raise RuntimeError('stalled then failed')
        time.sleep(0.5)
        return 'second'
    assert hedged(call, 0.1) == 'second'


def test_both_failures_raise():
    def call():
        time.sleep(0.2)
        raise RuntimeError('provider error')
    with pytest.raises(RuntimeError):
        hedged(call, 0.05)


def test_builder_is_hedged_by_default(tmp_path):
    cfg = load_config(data_dir=tmp_path)
    assert cfg.pipeline.bootstrap_hedge_seconds == 90
    assert cfg.pipeline.bootstrap_hedge_prompts == ['bootstrap_builder']
