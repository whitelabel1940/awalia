"""Bridge the synchronous turn pipeline to an SSE response."""
import asyncio
import json
from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse
from cbs.llm import LLMError
from cbs.llm.errors import failure_notice
from cbs.llm.failures import FailureState


def install_stream(app, selected, message_input):
    @app.post("/api/sessions/{session_id}/messages/stream")
    async def stream(session_id: str, body: message_input, pipeline=Depends(selected)):
        try:
            if pipeline.store.get_session(session_id)["character_id"] != pipeline.config.character.id:
                raise KeyError(session_id)
        except KeyError:
            raise HTTPException(404, "セッションがありません。") from None

        async def events():
            loop = asyncio.get_running_loop()
            queue = asyncio.Queue()
            def emit(event, data):
                loop.call_soon_threadsafe(queue.put_nowait, (event, data))
            def run():
                try:
                    result = pipeline.run(session_id, body.content, event_sink=emit, photos=body.photos)
                    pipeline.wait_for_expression(result["turn_id"])
                    pipeline.wait_for_notice(result["turn_id"])
                    notice = pipeline.store.costs.notice(consume=True)
                    if notice:
                        emit('cost_notice', notice)
                except Exception as error:
                    from cbs.telemetry import record_error
                    record_error(pipeline.store, 'reply', error)
                    safe = error if isinstance(error, LLMError) else LLMError("返答に失敗しました。")
                    emit("error", failure_notice(safe, pipeline.character.model.display_name))
                finally:
                    emit("llm_failures", {"notices": FailureState(pipeline.store).notices(pipeline.character.model.display_name, pipeline.config)})
                    emit(None, None)
            # Disconnect cancels the consumer, not persistence/extraction of the turn.
            task = asyncio.create_task(asyncio.to_thread(run))
            app.state.bootstrap_tasks.add(task)
            task.add_done_callback(app.state.bootstrap_tasks.discard)
            try:
                while True:
                    event, data = await queue.get()
                    if event is None:
                        break
                    yield "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
            finally:
                # Background work is owned by the lifespan task set.
                pass
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
