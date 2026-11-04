"""Copy tracked, inspected public files into a separate local Git clone (stdlib only)."""
import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    "characters", "input", "data", "docs", "reports", "bench", "probes",
    "scenarios", ".claude", ".tmp", "agents.md", ".git",
}
SECRET_PATTERNS = (
    ("Google API key", re.compile(r"AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("secret token", re.compile(r"(?:sk-ant-|sk-proj-|ghp_|github_pat_|phx_)[A-Za-z0-9_-]+")),
    ("private key header", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("Tauri secret key", re.compile(r"rsign\s+encrypted\s+secret\s+key")),
    ("Windows user path", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'<>\u2026]+", re.I)),
    ("private character name", re.compile("\u30cf\u30eb")),
)
DEV_TEST = re.compile(
    r"\b(?:from|import)\s+(?:bench|probes|reports)\b|"
    r"\b(?:bench|probes|reports)[/\\]|"
    r"\bPath\(\s*[\"'](?:bench|probes|reports)[\"']"
)


class PublishError(ValueError):
    """Only fixed reasons and file locations, never source text or Git diagnostics."""


def git(root, *arguments):
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False,
    )
    if result.returncode:
        raise PublishError("Git inspection failed (diagnostics withheld)")
    return result.stdout


def text_content(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Older Windows text files still need the ASCII token/path checks.
        return data.decode("cp1252", errors="replace")


def check_path(name, location, line=1):
    path = PurePosixPath(name)
    forbidden_parts = [part.casefold() for part in path.parts]
    # These synthetic character fixtures are required by the public test suite.
    if path.parts[:3] == ("tests", "fixtures", "characters"):
        forbidden_parts[2] = "fixture-characters"
    if (not path.parts or path.is_absolute() or ".." in path.parts
            or "\\" in name or ":" in name
            or any(part.rstrip(" .") != part for part in path.parts)
            or any(part in FORBIDDEN for part in forbidden_parts)):
        raise PublishError(f"{location}:{line}: forbidden public path")
    return path


def is_link(path):
    return path.is_symlink() or path.is_junction()


def validate_target(root, destination):
    # Check the resolved absolute target before any deletion or file replacement.
    if is_link(destination):
        raise PublishError("destination:1: linked target")
    target = destination.resolve()
    scratch = root / ".tmp"
    if (target == root or root.is_relative_to(target)
            or (target.is_relative_to(root) and
                (is_link(scratch) or not target.is_relative_to(scratch.resolve()) or target == scratch.resolve()))):
        raise PublishError("destination:1: repository root/descendant is forbidden except below .tmp")
    if not target.is_dir() or not (target / ".git").exists():
        raise PublishError("destination:1: expected a folder with .git")
    return target


def inspect_files(root):
    manifest = root / "release/public.txt"
    entries = []
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        entry = line.split("#", 1)[0].strip().rstrip("/")
        if entry:
            check_path(entry, "release/public.txt", number)
            entries.append(entry)
    if not entries:
        raise PublishError("release/public.txt:1: empty public list")
    tracked = git(root, "ls-files", "-z").decode("utf-8").split("\0")
    email_result = subprocess.run(
        ["git", "-C", str(root), "config", "user.email"], capture_output=True, check=False,
    )
    if email_result.returncode not in (0, 1):
        raise PublishError("Git email inspection failed (diagnostics withheld)")
    email = email_result.stdout.decode("utf-8").strip()
    files, excluded, matched = {}, [], set()
    for name in sorted(set(tracked) - {""}):
        selected = [entry for entry in entries if name == entry or name.startswith(entry + "/")]
        if not selected:
            continue
        matched.update(selected)
        path = check_path(name, name)
        if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        source = root / path
        if any(is_link(parent) for parent in [source, *source.parents] if parent.is_relative_to(root)):
            raise PublishError(f"{name}:1: linked source")
        if not source.is_file():
            raise PublishError(f"{name}:1: tracked source missing")
        data = source.read_bytes()
        content = text_content(data)
        if path.parts[0] == "tests" and content is not None and DEV_TEST.search(content):
            excluded.append(name)
            continue
        if content is not None:
            for number, line in enumerate(content.splitlines(), 1):
                for reason, pattern in SECRET_PATTERNS:
                    if pattern.search(line):
                        raise PublishError(f"{name}:{number}: {reason}")
                if email and email.casefold() in line.casefold():
                    raise PublishError(f"{name}:{number}: developer email")
        public_prefix = "release/public/"
        output = name[len(public_prefix):] if name.startswith(public_prefix) else name
        check_path(output, name)
        key = output.casefold()
        if key in files:
            raise PublishError(f"{name}:1: duplicate public destination")
        files[key] = (output, data)
    missing = set(entries) - matched
    if missing:
        raise PublishError("release/public.txt:1: no tracked files for " + ", ".join(sorted(missing)))
    outputs = {name: data for name, data in files.values()}
    for name in outputs:
        if any(parent.as_posix().casefold() in files for parent in PurePosixPath(name).parents if parent.parts):
            raise PublishError(f"{name}:1: public file/directory conflict")
    return outputs, excluded


def destination_files(target):
    files, directories = [], []
    def visit(directory):
        for path in sorted(directory.iterdir()):
            if path.parent == target and path.name.casefold() == ".git":
                continue
            relative = path.relative_to(target).as_posix()
            if is_link(path):
                raise PublishError(f"destination/{relative}:1: linked destination entry")
            if path.is_dir():
                directories.append(path)
                visit(path)
            elif path.is_file():
                files.append(path)
            else:
                raise PublishError(f"destination/{relative}:1: unsupported destination entry")
    visit(target)
    return files, directories


def publish(destination, root=ROOT):
    root = root.resolve()
    target = validate_target(root, Path(destination))
    outputs, excluded = inspect_files(root)
    existing, directories = destination_files(target)
    # Complete all inspection and comparison before changing the target.
    existing_by_name = {p.relative_to(target).as_posix(): p for p in existing}
    added = [name for name in outputs if name not in existing_by_name]
    changed = [name for name in outputs if name in existing_by_name
               and existing_by_name[name].read_bytes() != outputs[name]]
    deleted = [name for name in existing_by_name if name not in outputs]
    for name in deleted:
        existing_by_name[name].unlink()
    # Remove empty old directories, including directory -> file transitions.
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    for name in added + changed:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(outputs[name])
    for name in excluded:
        print(f"Excluded development test: {name}")
    counts = {"added": len(added), "changed": len(changed), "deleted": len(deleted)}
    print(f"Public copy complete: added={counts['added']} changed={counts['changed']} deleted={counts['deleted']}")
    print("Next, in the public clone (review before committing):")
    print('  git config --local user.email "<GitHub noreply email>"')
    print("  git status --short")
    print("  git diff --stat; git diff --no-ext-diff")
    print("  git add --all")
    print("  git diff --cached --no-ext-diff")
    print('  git commit -m "Update public source"')
    print("  git push origin HEAD")
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="local public Git clone to synchronize")
    args = parser.parse_args(argv)
    try:
        publish(args.destination)
    except PublishError as error:
        parser.exit(1, f"Public copy stopped: {error}\n")
    except (OSError, UnicodeError):
        parser.exit(1, "Public copy stopped: file/Git operation failed (details withheld)\n")


if __name__ == "__main__":
    main()
