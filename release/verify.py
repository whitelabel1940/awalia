"""Release checks that cost nothing (run with the development venv; needs `cryptography`).

  python release/verify.py release            installer in .tmp/release/out: updater .sig against the
                                              final bytes and latest.json, plus its Authenticode signature
  python release/verify.py installed [DIR]    every .exe/.dll of an installed Awalia (default
                                              %LOCALAPPDATA%\\Programs\\Awalia)

AWALIA_SIGN_SUBJECT set: awalia.exe, uninstall.exe and the installer must carry our timestamped
signature and match a signed output in the ledger. Unset: signatures are only reported.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sign  # noqa: E402  (sibling module; works when run as a script and when imported)


def verify_updater_signature(pubkey, path, signature):
    """Check a Tauri (minisign) signature against the file's final bytes. Raises on mismatch."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    public = base64.b64decode(base64.b64decode(pubkey).decode().splitlines()[1])
    if public[:2] != b"Ed":
        raise ValueError("unexpected updater public key format")
    lines = base64.b64decode(signature.strip()).decode().splitlines()
    sig, trusted, global_sig = base64.b64decode(lines[1]), lines[2].split(": ", 1)[1], base64.b64decode(lines[3])
    if sig[2:10] != public[2:10]:
        raise ValueError("updater signature was made with a different key")
    data = Path(path).read_bytes()
    message = hashlib.blake2b(data).digest() if sig[:2] == b"ED" else data
    key = Ed25519PublicKey.from_public_bytes(public[10:42])
    try:
        key.verify(sig[10:74], message)
        key.verify(global_sig, sig[10:74] + trusted.encode())
    except InvalidSignature:
        raise ValueError(f"updater signature does not match {Path(path).name}") from None
    return trusted


def pubkey(root=ROOT):
    config = json.loads((root / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    return config["plugins"]["updater"]["pubkey"]


def signed_outputs():
    return {entry["output_sha256"] for entry in sign.read_ledger("sign")
            if entry.get("result") in {"signed", "cached", "already"} and entry.get("output_sha256")}


def check_ours(path, subject, problems, *, ledger=True):
    info = sign.authenticode(path)
    line = f"{info.get('status')}  {path.name}"
    if subject:
        if not sign.is_ours(info, subject):
            problems.append(f"{path.name}: not signed by us with a timestamp ({info.get('status')})")
        elif ledger and sign.sha256(path) not in signed_outputs():
            problems.append(f"{path.name}: signed, but not a file the ledger recorded as our signed output")
    return line


def check_release(root=ROOT, subject=None):
    out = root / ".tmp/release/out"
    installers = sorted(out.glob("*-setup.exe"))
    if len(installers) != 1:
        raise ValueError("expected exactly one installer in .tmp/release/out")
    installer, problems = installers[0], []
    signature = installer.with_name(installer.name + ".sig").read_text(encoding="utf-8")
    trusted = verify_updater_signature(pubkey(root), installer, signature)
    manifest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    if manifest["platforms"]["windows-x86_64"]["signature"] != signature.strip():
        problems.append("latest.json carries a different signature than the .sig file")
    if manifest["platforms"]["windows-x86_64"]["url"].rsplit("/", 1)[-1] != installer.name.replace(" ", "%20"):
        problems.append("latest.json points to a different installer name")
    fixed = out / "Awalia-setup-x64.exe"  # build.FIXED_INSTALLER, the site's download link
    if not fixed.is_file() or sign.sha256(fixed) != sign.sha256(installer):
        problems.append(f"{fixed.name} is missing or differs from {installer.name}")
    lines = [f"updater signature OK ({trusted})", check_ours(installer, subject, problems)]
    return lines, problems


def check_installed(directory, subject=None):
    directory = Path(directory)
    files = sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in {".exe", ".dll"})
    names = {p.name.lower() for p in files}
    problems = [f"{name} is missing" for name in ("awalia.exe", "uninstall.exe") if name not in names]
    lines = []
    for path in files:
        if path.parent == directory and path.name.lower() in {"awalia.exe", "uninstall.exe"}:
            lines.append(check_ours(path, subject, problems))
            continue
        info = sign.authenticode(path)
        lines.append(f"{info.get('status')}  {path.relative_to(directory)}")
        if info.get("status") != "Valid":
            problems.append(f"{path.relative_to(directory)}: {info.get('status')}")
    return lines, problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    subject = os.environ.get("AWALIA_SIGN_SUBJECT", "").strip() or None
    if argv[:1] == ["release"]:
        lines, problems = check_release(subject=subject)
    elif argv[:1] == ["installed"]:
        default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Awalia"
        lines, problems = check_installed(argv[1] if len(argv) > 1 else default, subject)
    else:
        print(__doc__)
        return 2
    print("\n".join(lines))
    if not subject:
        print("(AWALIA_SIGN_SUBJECT not set: signatures reported, not required)")
    for problem in problems:
        print("PROBLEM:", problem)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
