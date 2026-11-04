from cbs.config import load_config
from cbs.llm import LLMError, create_client
from cbs.llm.cost import for_step
from cbs.pipeline import PreJudge
from cbs.runtime import read_prompt


def main():
    config = load_config()
    try:
        client = create_client(config)
        actor = for_step(client, 'actor', 'other').generate_text(config.models.actor, read_prompt("ping_actor"), [{"role": "user", "content": "こんにちは"}])
        print(f"actor: {actor.model} | {actor.latency_ms} ms | {actor.text}")
        judge = for_step(client, 'judge', 'other').generate_json(config.models.judge, read_prompt("ping_judge"), [{"role": "user", "content": "こんにちは"}], PreJudge)
        print(f"judge: {judge.model} | {judge.latency_ms} ms | {judge.parsed.model_dump_json()}")
    except LLMError as error:
        print(error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
