"""Split existing M2–M6 fixture content into the new Check/Extract contracts."""
def problems(post):
    if isinstance(post, str):
        return post
    return {"problems": [c for c in post.get("claims", [])
                         if c["verdict"] in ("contradicted", "forbidden")]}


def extraction(post):
    if isinstance(post, str):
        return post
    return {key: post.get(key, []) for key in
            ("ledger_updates", "new_canon", "disclosures", "used_memories", "new_threads", "used_threads", "understanding_questions")}
