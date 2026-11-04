"""R5 uses only temporary layouts and stubbed build/network boundaries."""
import json
from datetime import datetime

import pytest

from release import build as r5
from test_r4 import source, forbid_external_work, python_archive, installed_fixture


@pytest.mark.parametrize("version,notes", [("0.1.0", "変更の説明"), ("0.2.0", "")])
def test_manifest(source, version, notes):
    out = source / "out"
    out.mkdir()
    installer = source / "Awalia_0.1.0_x64-setup.exe"
    installer.write_bytes(b"not-executable")
    installer.with_suffix(".exe.sig").write_text("public-signature-fixture\n", encoding="utf-8")
    (source / "news").mkdir()
    (source / "news/news.json").write_text(json.dumps({"latest": {"version": version, "notes": "変更の説明"}}), encoding="utf-8")
    try:
        r5.write_update_manifest(source, out, "0.1.0", installer)
        data = json.loads((out / "latest.json").read_text(encoding="utf-8"))
        assert set(data) == {"version", "notes", "pub_date", "platforms"}
        assert data["version"] == "0.1.0" and data["notes"] == notes
        assert datetime.fromisoformat(data["pub_date"]).utcoffset().total_seconds() == 0
        assert data["platforms"] == {"windows-x86_64": {
            "signature": "public-signature-fixture",
            "url": "https://github.com/whitelabel1940/awalia/releases/download/v0.1.0/Awalia_0.1.0_x64-setup.exe"}}
        assert (out / (installer.name + ".sig")).is_file()
    finally:
        installer.unlink()


@pytest.mark.parametrize("missing", ["TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD"])
def test_missing_signing_stops_before_any_build_work(source, monkeypatch, capsys, missing):
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValueError, match="TAURI_SIGNING_PRIVATE_KEY"):
        r5.build(source)
    assert not (source / ".tmp").exists()
    assert not capsys.readouterr().out


def test_missing_signature_fails(source):
    with pytest.raises(ValueError, match="no updater signature"):
        r5.write_update_manifest(source, source, "0.1.0", source / "missing.exe")


def test_no_updater_is_explicit_and_never_signs(source, monkeypatch, capsys):
    for key in ("TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    archive = source / "fixture.zip"
    spec = python_archive(archive)
    (source / "release/python.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(r5, "download_python", lambda spec, target: target.write_bytes(archive.read_bytes()))
    monkeypatch.setattr(r5, "install_dependencies", lambda root, site: installed_fixture(site))
    monkeypatch.setattr(r5, "cargo_metadata", lambda root: {"packages": []})
    def fake_tauri(root, *, no_updater):
        assert root == source and no_updater
        target = root / ".tmp/release/target/release/bundle/nsis/fixture.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not-executable")
    monkeypatch.setattr(r5, "build_tauri", fake_tauri)
    try:
        out = r5.build(source, skip_audit=True, no_updater=True)
        assert (out / "fixture.exe").exists()
        assert not (out / "latest.json").exists()
        assert "--no-updater: updater artifacts are DISABLED" in capsys.readouterr().out
    finally:
        for target in source.rglob("*.exe"):
            target.unlink()


def test_tool_output_and_signing_credentials_are_never_exposed(source, monkeypatch, capsys):
    from test_r4 import EXTERNAL_STEPS
    def fail(command, **kwargs):
        assert kwargs["capture_output"] and kwargs["check"]
        # Represent a tool echoing inherited credentials. Only fixed diagnostics escape.
        raise r5.subprocess.CalledProcessError(1, command, output=b"sensitive fixture", stderr=b"sensitive fixture")
    monkeypatch.setattr(r5.subprocess, "run", fail)
    with pytest.raises(ValueError) as caught:
        EXTERNAL_STEPS["build_tauri"](source)
    assert "sensitive fixture" not in str(caught.value)
    assert capsys.readouterr() == ("", "")
    def record(command, **kwargs):
        assert command[-1] == '{"bundle":{"createUpdaterArtifacts":false}}'
        assert "TAURI_SIGNING_PRIVATE_KEY" not in kwargs["env"]
        assert "TAURI_SIGNING_PRIVATE_KEY_PASSWORD" not in kwargs["env"]
    monkeypatch.setattr(r5.subprocess, "run", record)
    EXTERNAL_STEPS["build_tauri"](source, no_updater=True)


def test_updater_config_uses_secure_defaults():
    config = json.loads((r5.ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    updater = config["plugins"]["updater"]
    # Opus can later fill the public key without breaking this configuration test.
    # Empty/whitespace keys disabling the updater are covered by Rust's gate test.
    assert isinstance(updater["pubkey"], str)
    assert updater["windows"]["installMode"] == "passive"
    assert updater["endpoints"] == ["https://github.com/whitelabel1940/awalia/releases/latest/download/latest.json"]
    assert "dangerousInsecureTransportProtocol" not in updater
    release = json.loads((r5.ROOT / "release/tauri.release.json").read_text(encoding="utf-8"))
    assert release["bundle"]["createUpdaterArtifacts"] is True
