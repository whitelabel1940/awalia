"""Public synchronization uses only local disposable repositories; no remote operations."""
from pathlib import Path
import subprocess

import pytest

from release import publish as r8


PUBLIC_ENTRIES = (
    "cbs", "bootstrap", "onboarding", "desktop", "release", "news", "tests",
    "config.toml", "pyproject.toml", "requirements.lock", "requirements-local-embedding.lock",
)
DEV_NAMES = ("ben" + "ch", "pro" + "bes", "re" + "ports")


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    assert result.returncode == 0, "Local fixture Git command failed"
    return result.stdout


def write(root, name, content="public fixture\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def track(root, *names):
    git(root, "add", "--", *names)


@pytest.fixture
def repositories(tmp_path):
    source, target = tmp_path / "development", tmp_path / "public"
    for root in (source, target):
        root.mkdir()
        git(root, "init", "-q")
    git(source, "config", "--local", "user.email", "private-developer@example.invalid")
    write(source, "release/public.txt", "\n".join(PUBLIC_ENTRIES))
    for entry in PUBLIC_ENTRIES:
        if "." in Path(entry).name:
            write(source, entry)
        else:
            write(source, entry + "/fixture.txt")
    track(source, ".")
    return source, target


def assert_untouched(target):
    assert (target / "sentinel.txt").read_text() == "keep me"
    assert sorted(p.name for p in target.iterdir()) == [".git", "sentinel.txt"]


def test_selection_overlay_and_tracked_worktree_contents(repositories, capsys):
    source, target = repositories
    write(target, "sentinel.txt", "keep me")
    write(source, "cbs/untracked.txt", "never publish")
    write(source, "release/public/README.md", "public readme")
    write(source, "release/public/LICENSE", "")
    write(source, "release/public/PRIVACY.md", "")
    write(source, "release/public/.gitignore", ".cache/\n")
    write(source, "release/public/.github/workflows/ci.yml", "")
    fixture = "/".join(("tests", "fixtures", "characters", "fixture", "model.yaml"))
    write(source, fixture)
    write(source, "cbs/__pycache__/fixture.pyc", "never publish")
    write(source, "cbs/fixture.pyc", "never publish")
    track(source, "release/public", fixture)
    git(source, "add", "-f", "cbs/__pycache__/fixture.pyc", "cbs/fixture.pyc")
    write(source, "cbs/fixture.txt", "tracked working-tree change")
    metadata_before = {p.relative_to(target / ".git"): p.read_bytes()
                       for p in (target / ".git").rglob("*") if p.is_file()}
    counts = r8.publish(target, source)
    assert counts == {"added": 18, "changed": 0, "deleted": 1}
    assert (target / "README.md").read_text() == "public readme"
    assert (target / ".github/workflows/ci.yml").is_file()
    assert (target / ".gitignore").read_text() == ".cache/\n"
    assert (target / "cbs/fixture.txt").read_text() == "tracked working-tree change"
    assert (target / fixture).is_file()
    assert not (target / "release/public").exists()
    assert not (target / "cbs/untracked.txt").exists()
    assert not list(target.rglob("*.pyc"))
    assert metadata_before == {p.relative_to(target / ".git"): p.read_bytes()
                               for p in (target / ".git").rglob("*") if p.is_file()}
    assert "added=18 changed=0 deleted=1" in capsys.readouterr().out
    assert r8.publish(target, source) == {"added": 0, "changed": 0, "deleted": 0}
    write(source, "cbs/fixture.txt", "new content")
    write(target, "extra/nested/file.txt")
    assert r8.publish(target, source) == {"added": 0, "changed": 1, "deleted": 1}
    assert not (target / "extra").exists()


@pytest.mark.parametrize("directory", DEV_NAMES)
@pytest.mark.parametrize("reference", ["from {d}.tool import run", "import {d}", "p = '{d}/fixture'", "p = '{d}\\\\fixture'", "Path('{d}')"])
def test_development_test_excluded_by_contents(repositories, capsys, directory, reference):
    source, target = repositories
    name = "tests/test_development.py"
    write(source, name, reference.format(d=directory))
    track(source, name)
    r8.publish(target, source)
    assert not (target / name).exists()
    assert name in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(r8.FORBIDDEN - {".git"}))
def test_forbidden_manifest_entry_stops_before_changes(repositories, name):
    source, target = repositories
    write(target, "sentinel.txt", "keep me")
    manifest = source / "release/public.txt"
    manifest.write_text(manifest.read_text() + "\n" + name + "/fixture\n")
    with pytest.raises(r8.PublishError, match=r"release/public.txt:12: forbidden"):
        r8.publish(target, source)
    assert_untouched(target)


@pytest.mark.parametrize("name", ["release/data/leak.txt", "release/public/AGENTS.md", "release/public/docs/file.txt"])
def test_nested_and_overlay_forbidden_paths(repositories, name):
    source, target = repositories
    write(target, "sentinel.txt", "keep me")
    write(source, name)
    track(source, name)
    with pytest.raises(r8.PublishError, match="forbidden public path"):
        r8.publish(target, source)
    assert_untouched(target)


def private_fixtures():
    return [
        "AI" + "za" + "a" * 35,
        *[prefix + "fixture" * 8 for prefix in ("sk-" + "ant-", "sk-" + "proj-", "gh" + "p_", "github_" + "pat_", "ph" + "x_")],
        "-----BEGIN " + "PRIVATE KEY-----",
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "rsign " + "encrypted " + "secret key",
        "private-developer@example.invalid",
        "C:" + "\\" + "Users" + "\\" + "private-fixture" + "\\file.txt",
        "C:" + "\\\\" + "Users" + "\\\\" + "private-fixture" + "\\\\file.txt",
        "\u30cf\u30eb",
    ]


@pytest.mark.parametrize("index", range(13))
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_private_content_stops_without_disclosing_values(repositories, capsys, index, encoding):
    source, target = repositories
    write(target, "sentinel.txt", "keep me")
    value = private_fixtures()[index]
    path = write(source, "cbs/private.txt")
    path.write_text("safe\n" + value + "\n", encoding=encoding)
    track(source, "cbs/private.txt")
    with pytest.raises(r8.PublishError) as caught:
        r8.publish(target, source)
    message = str(caught.value)
    assert "cbs/private.txt:2:" in message
    assert value not in message
    assert capsys.readouterr() == ("", "")
    assert_untouched(target)


def test_constructed_dummy_values_and_public_project_token_are_allowed(repositories):
    source, target = repositories
    content = "dummy = 'sk-" + "proj-' + 'a' * 40\n"
    content += "google = 'AI" + "za' + 'a' * 35\n"
    content += "key = 'phc_publicfixture'\n"
    content += "folder = 'C:" + "\\\\" + "Users" + "\\\\" + "\u2026" + "\\\\OneDrive'\n"
    write(source, "cbs/fixture.txt", content)
    r8.publish(target, source)
    assert (target / "cbs/fixture.txt").read_text(encoding="utf-8") == content


@pytest.mark.parametrize("path", ["release/public/.git /config", "release/public/.git./config"])
def test_windows_path_aliases_are_refused(path):
    with pytest.raises(r8.PublishError, match="forbidden public path"):
        r8.check_path(path, "release/public.txt", 1)


def test_worktree_git_file_is_preserved(repositories):
    source, target = repositories
    worktree = target.parent / "worktree"
    worktree.mkdir()
    write(worktree, ".git", "gitdir: ../fixture-metadata\n")
    r8.publish(worktree, source)
    assert (worktree / ".git").read_text() == "gitdir: ../fixture-metadata\n"


@pytest.mark.parametrize("relative", [".", "cbs", ".tmp"])
def test_source_root_and_non_scratch_descendant_refused(repositories, relative):
    source, _ = repositories
    target = source / relative
    target.mkdir(exist_ok=True)
    if relative != ".":
        git(target, "init", "-q")
    with pytest.raises(r8.PublishError, match="root/descendant"):
        r8.publish(target, source)
    assert (source / "cbs/fixture.txt").exists()


def test_scratch_descendant_allowed(repositories):
    source, _ = repositories
    write(source, ".gitignore", ".tmp/\n")
    target = source / ".tmp/r8-public"
    target.mkdir(parents=True)
    git(target, "init", "-q")
    r8.publish(target, source)
    assert (target / "cbs/fixture.txt").is_file()
    assert (target / ".git").is_dir()


def test_target_must_have_git_metadata(repositories):
    source, target = repositories
    empty = target.parent / "empty"
    empty.mkdir()
    with pytest.raises(r8.PublishError, match="folder with .git"):
        r8.publish(empty, source)
    assert not list(empty.iterdir())


@pytest.mark.parametrize("side", ["source", "target"])
def test_links_are_rejected_before_writes(repositories, side):
    source, target = repositories
    write(target, "sentinel.txt", "keep me")
    link = (source / "cbs/link.txt") if side == "source" else (target / "link.txt")
    try:
        link.symlink_to(source / "cbs/fixture.txt")
    except OSError:
        pytest.skip("Creating symbolic links requires Windows developer mode")
    if side == "source":
        track(source, "cbs/link.txt")
    with pytest.raises(r8.PublishError, match="linked"):
        r8.publish(target, source)
    assert (target / "sentinel.txt").read_text() == "keep me"
    assert (source / "cbs/fixture.txt").is_file()


def test_file_directory_transitions(repositories):
    source, target = repositories
    write(target, "cbs", "old file")
    write(target, "config.toml/nested.txt", "old nested")
    r8.publish(target, source)
    assert (target / "cbs/fixture.txt").is_file()
    assert (target / "config.toml").is_file()


def test_cli_failure_uses_safe_location_only(repositories, monkeypatch, capsys):
    source, target = repositories
    value = private_fixtures()[0]
    write(source, "cbs/fixture.txt", "safe\n" + value)
    original = r8.publish
    monkeypatch.setattr(r8, "publish", lambda destination: original(destination, source))
    with pytest.raises(SystemExit) as caught:
        r8.main([str(target)])
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert "cbs/fixture.txt:2:" in output.err
    assert value not in output.err + output.out
