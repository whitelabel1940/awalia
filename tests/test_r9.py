"""R9 Authenticode: fake signer and fake Windows signature checks only (never SSL.com)."""
import base64
import hashlib
import json
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from release import build, sign, verify
from test_r4 import source, forbid_external_work, python_archive, installed_fixture  # noqa: F401

VERIFY_RELEASE = build.verify_release  # captured before the autouse fixture replaces it
SUBJECT = "CN=Fixture Signer"
MARK = b"<SIGNED>"
SECRETS = {"SSL_CS_CRED": "cred-fixture-1", "SSL_CS_USER": "user-fixture-2",
           "SSL_CS_PASS": "pass-fixture-3", "SSL_CS_TOTP": "totp-fixture-4"}


def fake_authenticode(path):
    data = open(path, "rb").read()
    if data.endswith(MARK):
        return {"status": "Valid", "subject": SUBJECT + ", O=Fixture", "thumbprint": "AB", "timestamped": True}
    if data.endswith(b"<OTHER>"):
        return {"status": "Valid", "subject": "CN=Python Software Foundation", "thumbprint": "CD", "timestamped": True}
    return {"status": "NotSigned", "subject": "", "thumbprint": "", "timestamped": False}


@pytest.fixture
def signer(tmp_path, monkeypatch):
    monkeypatch.setenv("AWALIA_SIGN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWALIA_SIGN_RUN", "run-1")
    monkeypatch.setenv("AWALIA_SIGN_VERSION", "0.1.0")
    monkeypatch.setenv("AWALIA_SIGN_SUBJECT", SUBJECT)
    monkeypatch.setenv("AWALIA_CODESIGNTOOL_DIR", str(tmp_path / "tool"))
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    calls = []
    def codesign(source_path, output_dir, log):
        calls.append(source_path)
        (output_dir / source_path.name).write_bytes(source_path.read_bytes() + MARK)
        return True

    monkeypatch.setattr(sign, "authenticode", fake_authenticode)
    monkeypatch.setattr(verify.sign, "authenticode", fake_authenticode)
    monkeypatch.setattr(sign, "codesign", codesign)
    def forbidden(*args, **kwargs):
        raise AssertionError("tests never run real tools")
    monkeypatch.setattr(sign.subprocess, "run", forbidden)
    return calls


def run(mode, path, monkeypatch, budget=10):
    monkeypatch.setenv("AWALIA_SIGN_MODE", mode)
    monkeypatch.setenv("AWALIA_SIGN_BUDGET", str(budget))
    return sign.main([str(path)])


def everything_written(tmp_path):
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in (tmp_path / "home").rglob("*") if p.is_file())


def test_roles_follow_tauri_paths(tmp_path):
    assert sign.role(tmp_path / "target/release/awalia.exe") == "main"
    assert sign.role(tmp_path / "target/release/bundle/nsis/Awalia_0.1.0_x64-setup.exe") == "installer"
    assert sign.role(tmp_path / "nsis/x64/NSIS/Plugins/x86-unicode/additional/nsis_tauri_utils.dll") == "nsis-plugin"
    assert sign.role(tmp_path / ".tmp/release/stage/runtime/Lib/x.dll") == "resource"
    assert sign.role(tmp_path / "AppData/Local/Temp/nsu1234.tmp") == "uninstaller"


def test_dry_run_records_and_never_signs(tmp_path, monkeypatch, signer, capsys):
    target = tmp_path / ".tmp/release/stage/runtime/openblas.dll"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unsigned")
    assert run("record", target, monkeypatch) == 0
    assert target.read_bytes() == b"unsigned" and signer == []
    entry, = sign.read_ledger("record")
    assert entry["role"] == "resource" and entry["would"] == "sign" and entry["status"] == "NotSigned"
    assert entry["input_sha256"] == hashlib.sha256(b"unsigned").hexdigest()
    assert sign.read_ledger("sign") == []


def test_sign_replaces_in_place_caches_and_reuses(tmp_path, monkeypatch, signer, capsys):
    uninstaller = tmp_path / "Temp/nsu1234.tmp"
    uninstaller.parent.mkdir()
    uninstaller.write_bytes(b"uninstaller")
    assert run("sign", uninstaller, monkeypatch) == 0
    assert uninstaller.read_bytes() == b"uninstaller" + MARK
    assert signer[0].suffix == ".exe"  # CodeSignTool gets a proper extension, not the temp name
    results = [e["result"] for e in sign.read_ledger()]
    assert results == ["attempt", "signed"]
    signed = sign.read_ledger()[-1]
    assert signed["output_sha256"] == hashlib.sha256(b"uninstaller" + MARK).hexdigest()
    # Same unsigned bytes in a later build: copied from the cache, no paid request.
    again = tmp_path / "Temp/nsu9999.tmp"
    again.write_bytes(b"uninstaller")
    assert run("sign", again, monkeypatch) == 0
    assert again.read_bytes() == b"uninstaller" + MARK and len(signer) == 1
    assert sign.read_ledger()[-1]["result"] == "cached" and sign.attempts() == 1
    # A stray space around the signer name (typed in a shell) still finds the copy.
    monkeypatch.setenv("AWALIA_SIGN_SUBJECT", f" {SUBJECT} ")
    spaced = tmp_path / "Temp/nsu7777.tmp"
    spaced.write_bytes(b"uninstaller")
    assert run("sign", spaced, monkeypatch) == 0
    assert sign.read_ledger()[-1]["result"] == "cached" and sign.attempts() == 1
    monkeypatch.setenv("AWALIA_SIGN_SUBJECT", SUBJECT)
    # A changed cache copy is never trusted.
    cache = next((tmp_path / "home/cache").glob("*.exe"))
    cache.write_bytes(b"tampered" + MARK)
    third = tmp_path / "Temp/nsu7777.tmp"
    third.write_bytes(b"uninstaller")
    assert run("sign", third, monkeypatch) == 0
    assert len(signer) == 2 and third.read_bytes() == b"uninstaller" + MARK
    # Already ours: nothing to do. Someone else's signature does not count as ours.
    assert run("sign", third, monkeypatch) == 0
    assert sign.read_ledger()[-1]["result"] == "already" and len(signer) == 2
    other = tmp_path / ".tmp/release/stage/runtime/python.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"psf<OTHER>")
    assert run("sign", other, monkeypatch) == 0 and len(signer) == 3
    output = capsys.readouterr().out + everything_written(tmp_path)
    assert not any(value in output for value in SECRETS.values())


def test_budget_failure_and_missing_settings(tmp_path, monkeypatch, signer):
    target = tmp_path / "awalia.exe"
    target.write_bytes(b"main")
    assert run("sign", target, monkeypatch, budget=0) == 1
    assert target.read_bytes() == b"main" and signer == []
    assert [e["result"] for e in sign.read_ledger()] == ["refused"] and sign.attempts() == 0
    monkeypatch.setattr(sign, "codesign", lambda *args: signer.append(args) or False)
    assert run("sign", target, monkeypatch) == 1
    assert target.read_bytes() == b"main"
    assert [e["result"] for e in sign.read_ledger()][-2:] == ["attempt", "failed"]
    assert sign.attempts() == 1 and sign.attempts(run="other") == 0  # a failure still counts as used
    monkeypatch.delenv("SSL_CS_TOTP")
    assert run("sign", target, monkeypatch) == 1 and len(signer) == 1


def test_codesign_keeps_secrets_out_of_arguments_and_logs(tmp_path, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWALIA_CODESIGNTOOL_DIR", str(tmp_path / "tool"))
    seen = {}
    def fake_run(command, **kwargs):
        seen["command"] = command
        return sign.subprocess.CompletedProcess(command, 0, stdout="echo " + SECRETS["SSL_CS_PASS"], stderr="")
    monkeypatch.setattr(sign.subprocess, "run", fake_run)
    log = tmp_path / "logs/one.log"
    assert sign.codesign(tmp_path / "in/awalia.exe", tmp_path / "out", log)
    assert "%SSL_CS_PASS%" in seen["command"] and "-output_dir_path=" in seen["command"]
    assert not any(value in seen["command"] for value in SECRETS.values())
    assert SECRETS["SSL_CS_PASS"] not in log.read_text(encoding="utf-8") and "***" in log.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="metacharacters"):
        sign.codesign_command(tmp_path, tmp_path / "a&b.exe", tmp_path)
    # Escaped and encoded forms a tool might print are masked too.
    awkward = 'p"w\\3 &x'
    monkeypatch.setenv("SSL_CS_PASS", awkward)
    printed = f'{{"password": {json.dumps(awkward)}}} url=?p={__import__("urllib.parse").parse.quote(awkward, safe="")} raw={awkward}'
    masked = sign.redact(printed)
    assert "p\\\"w" not in masked and "p%22w" not in masked and awkward not in masked and masked.count("***") == 3


# --- updater signature (minisign format used by Tauri) ---

def minisign(data, key=None):
    key = key or Ed25519PrivateKey.generate()
    key_id = b"12345678"
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    pub_text = "untrusted comment: minisign public key\n" + base64.b64encode(b"Ed" + key_id + raw).decode() + "\n"
    sig = b"ED" + key_id + key.sign(hashlib.blake2b(data).digest())
    trusted = "timestamp:1\tfile:fixture-setup.exe\tversion:0.1.0"
    text = ("untrusted comment: signature\n" + base64.b64encode(sig).decode() + "\ntrusted comment: " + trusted
            + "\n" + base64.b64encode(key.sign(sig[10:] + trusted.encode())).decode() + "\n")
    return base64.b64encode(pub_text.encode()).decode(), base64.b64encode(text.encode()).decode()


def test_updater_signature_matches_final_bytes_only(tmp_path):
    installer = tmp_path / "fixture-setup.exe"
    installer.write_bytes(b"final installer")
    pubkey, signature = minisign(b"final installer")
    assert "file:fixture-setup.exe" in verify.verify_updater_signature(pubkey, installer, signature)
    installer.write_bytes(b"final installer, changed after signing")
    with pytest.raises(ValueError, match="does not match"):
        verify.verify_updater_signature(pubkey, installer, signature)
    other, _ = minisign(b"x")
    installer.write_bytes(b"final installer")
    with pytest.raises(ValueError):
        verify.verify_updater_signature(other, installer, signature)


def test_installed_files_must_be_ours_and_in_the_ledger(tmp_path, monkeypatch, signer):
    folder = tmp_path / "Awalia"
    (folder / "runtime").mkdir(parents=True)
    (folder / "awalia.exe").write_bytes(b"main" + MARK)
    (folder / "uninstall.exe").write_bytes(b"uninstaller" + MARK)
    (folder / "runtime/python.exe").write_bytes(b"psf<OTHER>")
    (folder / "runtime/openblas.dll").write_bytes(b"blas" + MARK)
    for data in (b"main", b"uninstaller"):
        sign.append_ledger("sign", result="signed", role="x", name="x", input_sha256=hashlib.sha256(data).hexdigest(),
                           output_sha256=hashlib.sha256(data + MARK).hexdigest())
    lines, problems = verify.check_installed(folder, SUBJECT)
    assert problems == [] and len(lines) == 4
    (folder / "uninstall.exe").write_bytes(b"uninstaller")  # NSIS left it unsigned
    (folder / "runtime/openblas.dll").write_bytes(b"blas")
    _, problems = verify.check_installed(folder, SUBJECT)
    assert any("uninstall.exe: not signed" in p for p in problems) and any("openblas.dll" in p for p in problems)
    (folder / "uninstall.exe").write_bytes(b"different" + MARK)
    _, problems = verify.check_installed(folder, SUBJECT)
    assert any("not a file the ledger recorded" in p for p in problems)
    (folder / "uninstall.exe").unlink()
    _, problems = verify.check_installed(folder, SUBJECT)
    assert "uninstall.exe is missing" in problems


# --- build.py ---

def test_prune_runtime_drops_launchers_and_dev_packages(tmp_path):
    site = tmp_path / "site"
    (site / "bin").mkdir(parents=True)
    (site / "bin/pytest.exe").write_bytes(b"launcher")
    installed_fixture(site)
    for name, files in (("pytest", ["pytest/__init__.py", "_pytest/main.py"]), ("pygments", ["pygments/__init__.py"])):
        info = site / f"{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\n", encoding="utf-8")
        for file in files:
            (site / file).parent.mkdir(parents=True, exist_ok=True)
            (site / file).write_text("", encoding="utf-8")
        (info / "RECORD").write_text("".join(f"{f},,\n" for f in [*files, f"{name}-1.0.dist-info/METADATA", f"{name}-1.0.dist-info/RECORD"]), encoding="utf-8")
    build.prune_runtime(site)
    assert sorted(p.name for p in site.iterdir()) == ["fixture-1.2.dist-info"]
    needs = site / "needs-1.0.dist-info"
    needs.mkdir()
    (needs / "METADATA").write_text("Metadata-Version: 2.4\nName: needs\nVersion: 1.0\nRequires-Dist: pytest>=8\n"
                                    "Requires-Dist: pluggy; extra == 'testing'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="needs development package pytest"):
        build.prune_runtime(site)


def test_tauri_gets_sign_command_without_secrets(source, monkeypatch):
    from test_r4 import EXTERNAL_STEPS
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    seen = {}
    monkeypatch.setattr(build.subprocess, "run", lambda command, **kwargs: seen.update(command=command, env=kwargs["env"]))
    EXTERNAL_STEPS["build_tauri"](source, signing={"mode": "record", "run": "r", "budget": 0, "version": "0.1.0"})
    config = json.loads(seen["command"][-1])["bundle"]["windows"]["signCommand"]
    assert config == {"cmd": sys.executable, "args": [str(source / "release/sign.py"), "%1"]}
    assert not any(value in " ".join(seen["command"]) for value in SECRETS.values())
    assert {k: seen["env"][k] for k in ("AWALIA_SIGN_MODE", "AWALIA_SIGN_RUN", "AWALIA_SIGN_BUDGET")} == {
        "AWALIA_SIGN_MODE": "record", "AWALIA_SIGN_RUN": "r", "AWALIA_SIGN_BUDGET": "0"}


def stage_build(source, monkeypatch, tauri):
    archive = source / "fixture.zip"
    spec = python_archive(archive)
    (source / "release/python.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(build, "download_python", lambda spec, target: target.write_bytes(archive.read_bytes()))
    monkeypatch.setattr(build, "install_dependencies", lambda root, site: installed_fixture(site))
    monkeypatch.setattr(build, "cargo_metadata", lambda root: {"packages": []})
    monkeypatch.setattr(build, "build_tauri", tauri)
    monkeypatch.setattr(build, "verify_release", lambda root, installer, *, signed: None)


def fake_tauri_run(roles):
    def tauri(root, *, signing=None, no_updater=False):
        for role in roles:
            sign.append_ledger(signing["mode"], run=signing["run"], role=role, name=role, input_sha256="0",
                               result="recorded" if signing["mode"] == "record" else "signed",
                               status="NotSigned", would="sign")
        installer = root / ".tmp/release/target/release/bundle/nsis/fixture-setup.exe"
        installer.parent.mkdir(parents=True, exist_ok=True)
        installer.write_bytes(b"fixture-installer")
        installer.with_suffix(".exe.sig").write_text("fixture-signature", encoding="utf-8")
        tauri.signing = signing
    return tauri


def test_dry_run_build_lists_calls_and_requires_the_uninstaller(source, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AWALIA_SIGN_HOME", str(tmp_path / "home"))
    tauri = fake_tauri_run(["main", *["nsis-plugin"] * 5, "resource", "uninstaller", "installer"])
    stage_build(source, monkeypatch, tauri)
    build.build(source, skip_audit=True, authenticode="record")
    assert tauri.signing["mode"] == "record" and tauri.signing["budget"] == 0
    assert "installer 1, main 1, nsis-plugin 5, resource 1, uninstaller 1" in capsys.readouterr().out
    # NSIS ignores a failed uninstaller command: a missing call stops the build.
    stage_build(source, monkeypatch, fake_tauri_run(["main", "installer"]))
    with pytest.raises(ValueError, match="one uninstaller"):
        build.build(source, skip_audit=True, authenticode="record")


def test_paid_build_needs_settings_and_confirmation_before_tauri(source, monkeypatch, tmp_path):
    monkeypatch.setenv("AWALIA_SIGN_HOME", str(tmp_path / "home"))
    with pytest.raises(ValueError, match="SSL_CS_CRED"):
        build.build(source, skip_audit=True, authenticode="sign")
    assert not (source / ".tmp").exists()
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWALIA_SIGN_SUBJECT", SUBJECT)
    (tmp_path / "tool").mkdir()
    (tmp_path / "tool/CodeSignTool.bat").write_text("", encoding="utf-8")
    monkeypatch.setenv("AWALIA_CODESIGNTOOL_DIR", str(tmp_path / "tool"))
    tauri = fake_tauri_run(["main", "uninstaller", "installer"])
    stage_build(source, monkeypatch, tauri)
    monkeypatch.setattr(build, "estimate_signatures", lambda root, stage: {
        "main": 1, "nsis-plugin": 5, "resource": 1, "uninstaller": 1, "installer": 1, "resource_files": ["x.dll"]})
    shown = []
    monkeypatch.setattr(build, "confirm", lambda message: shown.append(message) or False)
    with pytest.raises(ValueError, match="not confirmed"):
        build.build(source, skip_audit=True, authenticode="sign")
    assert not hasattr(tauri, "signing") and "up to 9 paid signatures" in shown[0]
    assert not any(value in shown[0] for value in SECRETS.values())
    monkeypatch.setattr(build, "confirm", lambda message: True)
    build.build(source, skip_audit=True, authenticode="sign")
    assert tauri.signing["budget"] == 9 and tauri.signing["mode"] == "sign"


def test_verify_release_uses_the_final_installer(source, tmp_path):
    installer = tmp_path / "fixture-setup.exe"
    installer.write_bytes(b"final")
    pubkey, signature = minisign(b"final")
    (source / "desktop/src-tauri/tauri.conf.json").write_text(json.dumps({"version": "0.1.0", "plugins": {"updater": {"pubkey": pubkey}}}), encoding="utf-8")
    installer.with_suffix(".exe.sig").write_text(signature, encoding="utf-8")
    VERIFY_RELEASE(source, installer, signed=False)
    installer.write_bytes(b"final, re-signed afterwards")
    with pytest.raises(ValueError, match="does not match"):
        VERIFY_RELEASE(source, installer, signed=False)