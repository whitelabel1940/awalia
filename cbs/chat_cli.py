import argparse

from cbs.llm import LLMError
from cbs.runtime import create_pipeline


def main():
    parser = argparse.ArgumentParser(description="パートナーと端末で話す（/quit で終了）")
    parser.add_argument("--session", help="既存セッションID")
    args = parser.parse_args()
    try:
        pipeline = create_pipeline()
        session = pipeline.store.get_session(args.session) if args.session else pipeline.store.create_session(pipeline.config.character.id)
        print(f"session: {session['id']}（/quit で終了）")
        for turn in pipeline.store.history(session["id"]):
            print(f"あなた: {turn['user_text']}\nパートナー: {turn['assistant_text'] or '（失敗）'}")
        while True:
            try:
                message = input("あなた: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if message == "/quit":
                break
            if not message:
                continue
            try:
                turn = pipeline.run(session["id"], message)
                print(f"パートナー: {turn['assistant_text']}\n[turn {turn['turn_id']} / trace saved]")
            except LLMError as error:
                print(error)
    except (LLMError, KeyError) as error:
        print("セッションがありません。" if isinstance(error, KeyError) else error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
