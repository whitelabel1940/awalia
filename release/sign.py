"""Authenticode hook for Tauri's `bundle.windows.signCommand` (Windows, stdlib only).

Tauri runs `<python> release/sign.py <file>` for every file it signs. `release/build.py`
sets the mode and limits through the environment:

  AWALIA_SIGN_MODE    record (dry run: list what Tauri asks for, never contacts SSL.com)
                      or sign (SSL.com CodeSignTool)
  AWALIA_SIGN_RUN     id of this build; AWALIA_SIGN_BUDGET caps paid attempts in it
  AWALIA_SIGN_VERSION release version, for the ledger

Signing needs SSL_CS_CRED / SSL_CS_USER / SSL_CS_PASS / SSL_CS_TOTP, AWALIA_CODESIGNTOOL_DIR
(the folder with CodeSignTool.bat) and AWALIA_SIGN_SUBJECT (text expected in the signer's
subject). The credentials are expanded by cmd.exe from the environment; they never appear
in this process's arguments, stdout, the ledger, the cache or the logs.

Ledger, cache and logs live in AWALIA_SIGN_HOME (default ~/.awalia/sign), outside the repo.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SECRET_VARIABLES = ("SSL_CS_CRED", "SSL_CS_USER", "SSL_CS_PASS", "SSL_CS_TOTP")
NSIS_PLUGINS = ("NSISdl.dll", "StartMenu.dll", "System.dll", "nsDialogs.dll", "nsis_tauri_utils.dll")
PAID = "attempt"  # one ledger line per request sent to SSL.com, written before sending


def sign_home():
    return Path(os.environ.get("AWALIA_SIGN_HOME") or Path.home() / ".awalia" / "sign")


def sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def role(path):
    path = Path(path)
    parts = [part.lower() for part in path.parts]
    if path.name.lower().endswith("-setup.exe") and "nsis" in parts:
        return "installer"
    if path.name.lower() == "awalia.exe":
        return "main"
    if path.name.lower() in {name.lower() for name in NSIS_PLUGINS} and "x86-unicode" in parts:
        return "nsis-plugin"
    if "stage" in parts and ("app" in parts or "runtime" in parts):
        return "resource"
    # NSIS !uninstfinalize passes the uninstaller as a temporary file.
    return "uninstaller"


def authenticode(path):
    """Signature facts from Windows (status, signer subject, thumbprint, timestamp)."""
    script = ("$s=Get-AuthenticodeSignature -LiteralPath $env:AWALIA_VERIFY_PATH;"
              "$c=$s.SignerCertificate;"
              "[pscustomobject]@{status=[string]$s.Status;"
              "subject=$(if($c){$c.Subject}else{''});thumbprint=$(if($c){$c.Thumbprint}else{''});"
              "timestamped=[bool]$s.TimeStamperCertificate}|ConvertTo-Json -Compress")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            env={**os.environ, "AWALIA_VERIFY_PATH": str(path)},
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def is_ours(info, subject):
    return (bool(subject) and info.get("status") == "Valid" and subject in (info.get("subject") or "")
            and bool(info.get("timestamped")))


def ledger_path(mode):
    return sign_home() / ("ledger.jsonl" if mode == "sign" else "dry-run.jsonl")


def read_ledger(mode="sign"):
    path = ledger_path(mode)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_ledger(mode, **entry):
    path = ledger_path(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
             "run": os.environ.get("AWALIA_SIGN_RUN", ""), "version": os.environ.get("AWALIA_SIGN_VERSION", ""),
             **entry}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def attempts(*, run=None, month=None):
    """Requests sent to SSL.com (failed ones included: we cannot know if they were charged)."""
    return sum(1 for entry in read_ledger("sign") if entry.get("result") == PAID
               and (run is None or entry.get("run") == run)
               and (month is None or entry.get("time", "").startswith(month)))


def cached(input_hash, subject):
    """A previously signed copy of exactly this unsigned file, re-verified before use."""
    folder = sign_home() / "cache"
    meta = folder / f"{input_hash}.json"
    if not meta.is_file():
        return None
    record = json.loads(meta.read_text(encoding="utf-8"))
    signed = folder / record["file"]
    if not signed.is_file() or sha256(signed) != record["output_sha256"]:
        return None
    if not is_ours(authenticode(signed), subject):
        return None
    return signed, record["output_sha256"]


def store_cache(input_hash, signed, output_hash, kind, name):
    folder = sign_home() / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{input_hash}{signed.suffix}"
    shutil.copyfile(signed, target)
    if sha256(target) != output_hash:
        target.unlink()
        raise ValueError("cache copy changed")
    (folder / f"{input_hash}.json").write_text(json.dumps(
        {"file": target.name, "output_sha256": output_hash, "role": kind, "name": name}), encoding="utf-8")


def codesign_command(tool_dir, source, output_dir):
    for path in (tool_dir, source, output_dir):
        if any(character in str(path) for character in '"%^&|<>'):
            raise ValueError("signing paths must not contain shell metacharacters")
    # cmd.exe expands %VAR% itself, so the secrets stay out of this command line.
    line = (f'"{Path(tool_dir) / "CodeSignTool.bat"}" sign -credential_id="%SSL_CS_CRED%" '
            f'-username="%SSL_CS_USER%" -password="%SSL_CS_PASS%" -totp_secret="%SSL_CS_TOTP%" '
            f'-input_file_path="{source}" -output_dir_path="{output_dir}"')
    return f'cmd /d /s /c "{line}"'


def redact(text):
    from urllib.parse import quote, quote_plus
    for name in SECRET_VARIABLES:
        value = os.environ.get(name)
        if value:
            # Also the forms a tool may print: JSON-escaped and URL-encoded.
            for form in sorted({value, json.dumps(value)[1:-1], quote(value, safe=""), quote_plus(value)}, key=len, reverse=True):
                text = text.replace(form, "***")
    return text


def codesign(source, output_dir, log):
    """Send one file to SSL.com. Returns True when CodeSignTool reports success."""
    tool_dir = Path(os.environ["AWALIA_CODESIGNTOOL_DIR"])
    result = subprocess.run(codesign_command(tool_dir, source, output_dir), cwd=tool_dir,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(redact(f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"), encoding="utf-8")
    return result.returncode == 0


def work_copy(target, folder):
    """Copy to a work folder under a name CodeSignTool accepts (the uninstaller arrives as a temp file)."""
    folder.mkdir(parents=True)
    suffix = target.suffix.lower() if target.suffix.lower() in {".exe", ".dll"} else ".exe"
    copy = folder / (target.stem + suffix)
    shutil.copyfile(target, copy)
    return copy


def replace_verified(target, signed, output_hash):
    shutil.copyfile(signed, target)
    if sha256(target) != output_hash:
        raise ValueError(f"{target.name}: file differs after replacing it with the signed copy")


def record(target):
    input_hash = sha256(target)
    info = authenticode(target)
    subject = os.environ.get("AWALIA_SIGN_SUBJECT", "").strip()  # a stray space would miss every cached copy
    if is_ours(info, subject):
        would = "already"
    elif subject and cached(input_hash, subject):
        would = "cached"
    else:
        would = "sign"
    append_ledger("record", result="recorded", role=role(target), name=target.name, path=str(target),
                  input_sha256=input_hash, status=info.get("status"), would=would)
    print(f"awalia-sign: recorded {role(target)} {target.name} ({would})")
    return 0


def sign(target):
    subject = os.environ.get("AWALIA_SIGN_SUBJECT", "").strip()  # a stray space would miss every cached copy
    missing = [name for name in (*SECRET_VARIABLES, "AWALIA_CODESIGNTOOL_DIR", "AWALIA_SIGN_RUN") if not os.environ.get(name)]
    if missing or not subject:
        print("awalia-sign: signing environment is incomplete")
        return 1
    run, kind = os.environ["AWALIA_SIGN_RUN"], role(target)
    input_hash = sha256(target)
    base = dict(role=kind, name=target.name, input_sha256=input_hash)
    if is_ours(authenticode(target), subject):
        append_ledger("sign", result="already", output_sha256=input_hash, **base)
        print(f"awalia-sign: already signed {kind} {target.name}")
        return 0
    hit = cached(input_hash, subject)
    if hit:
        signed, output_hash = hit
        replace_verified(target, signed, output_hash)
        append_ledger("sign", result="cached", output_sha256=output_hash, **base)
        print(f"awalia-sign: reused signed copy for {kind} {target.name}")
        return 0
    budget = int(os.environ.get("AWALIA_SIGN_BUDGET", "0"))
    if attempts(run=run) >= budget:
        append_ledger("sign", result="refused", **base)
        print(f"awalia-sign: budget of {budget} signatures for this build is used up; refusing {target.name}")
        return 1
    count = attempts() + 1
    folder = sign_home() / "work" / f"{run}-{count}"
    if folder.exists():  # left by an interrupted attempt; nothing in it is trusted
        shutil.rmtree(folder)
    copy = work_copy(target, folder / "in")
    output_dir = folder / "out"
    output_dir.mkdir()
    append_ledger("sign", result=PAID, **base)
    ok = codesign(copy, output_dir, sign_home() / "logs" / f"{run}-{count}.log")
    signed = output_dir / copy.name
    if not ok or not signed.is_file() or not is_ours(authenticode(signed), subject):
        append_ledger("sign", result="failed", **base)
        print(f"awalia-sign: signing failed for {kind} {target.name} (log: {run}-{count}.log)")
        return 1
    output_hash = sha256(signed)
    replace_verified(target, signed, output_hash)
    store_cache(input_hash, signed, output_hash, kind, target.name)
    append_ledger("sign", result="signed", output_sha256=output_hash, **base)
    print(f"awalia-sign: signed {kind} {target.name}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: sign.py <file>")
        return 2
    target = Path(argv[0])
    if not target.is_file():
        print("awalia-sign: no such file")
        return 1
    mode = os.environ.get("AWALIA_SIGN_MODE")
    if mode == "record":
        return record(target)
    if mode == "sign":
        try:
            return sign(target)
        except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"awalia-sign: stopped: {type(error).__name__}")
            return 1
    print("awalia-sign: AWALIA_SIGN_MODE must be record or sign")
    return 2


if __name__ == "__main__":
    sys.exit(main())
