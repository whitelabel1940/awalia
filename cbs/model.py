"""Append-only model diffs, shared by all readers of a database."""
from cbs.character import CharacterModel


def current_model(character, versions):
    data = character.model.model_dump(mode="json")
    for version in versions:
        if version["invalidated"] or "changes" not in version["diff"]:
            continue
        for change in version["diff"]["changes"]:
            items = data[change["collection"]]
            target = next((item for item in items if item["id"] == change["target"]), None)
            if target is None:
                items.append(change["after"])
            else:
                target.update(change["after"])
        data["version"] = version["id"] + character.model.version
    return CharacterModel.model_validate(data)


def model_changes(initial, current):
    result = []
    for collection in ("domains", "signatures"):
        before = {item.id: item.model_dump(mode="json") for item in getattr(initial, collection)}
        for item in getattr(current, collection):
            after = item.model_dump(mode="json")
            if before.get(item.id) != after:
                result.append({"collection": collection, "target": item.id, "before": before.get(item.id), "after": after})
    return result
