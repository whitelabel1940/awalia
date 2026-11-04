"""The judge selects an image slot; failures never fail a turn."""
from cbs.llm.cost import for_step
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextvars import copy_context
from typing import Literal

from pydantic import BaseModel, ConfigDict
from cbs.runtime import read_prompt
from cbs.timing import span

EXPRESSIONS = ("neutral", "smile", "shy", "surprised", "sad", "troubled", "thinking", "pout")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="expression")


class Expression(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: Literal["neutral", "smile", "shy", "surprised", "sad", "troubled", "thinking", "pout"]


def judge_expression(pipeline, user_text, reply):
    context = {"user_text": user_text, "assistant_text": reply,
               "demeanor": pipeline.store.current_character(pipeline.character).expression.model_dump(mode="json").get("demeanor", {})}
    try:
        context['feeling'] = pipeline.store.current_feeling(pipeline.config.character.id)
        with span("expression_llm"):
            result = for_step(pipeline.client, 'expression', 'chat').generate_json(pipeline.config.models.judge, read_prompt("expression"),
                [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}], Expression)
        value = Expression.model_validate(result.parsed).expression
        return value, pipeline._safe_trace(result.trace())
    except Exception as error:
        return "neutral", {**(error.metadata() if hasattr(error, "metadata") else {}),
                           "error": "表情の判断に失敗したため neutral で表示します。"}


def start_expression(pipeline, user_text, reply):
    return _pool.submit(copy_context().run, judge_expression, pipeline, user_text, reply)


def finish_expression(future, trace, step="Expression", *, timeout=10):
    try:
        value, details = future.result(timeout=timeout)
    except TimeoutError:
        value, details = "neutral", {"error": "表情の判断がタイムアウトしました。"}
    trace[step] = {**details, "output": {"expression": value}}
    return value
