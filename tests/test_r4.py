"""Release tests use synthetic packages and stub every network/build boundary."""
import hashlib
import json
from pathlib import Path
import zipfile

import re
import sys

import pytest

from release import build as r4

EXTERNAL_STEPS = {name: getattr(r4, name) for name in
                  ("download_python", "install_dependencies", "run_audits", "audit_runtime", "cargo_metadata", "build_tauri")}


@pytest.fixture(autouse=True)
def forbid_external_work(monkeypatch):
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "fixture-key-path")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "")
    def forbidden(*args, **kwargs):
        raise AssertionError("Release tests must not use the network or real build tools")
    for name in ("download_python", "install_dependencies", "run_audits", "cargo_metadata", "build_tauri",
                 "verify_release", "estimate_signatures", "confirm", "audit_runtime"):
        monkeypatch.setattr(r4, name, forbidden)
    monkeypatch.setattr(r4.subprocess, "run", forbidden)
    monkeypatch.setattr(r4.urllib.request, "urlopen", forbidden)


@pytest.fixture
def source(tmp_path):
    for directory in ("release", "cbs", "bootstrap", "onboarding", "desktop/src-tauri"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "release/include.txt").write_text("\n".join(sorted(r4.REQUIRED)), encoding="utf-8")
    (tmp_path / "cbs/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "bootstrap/domains.yaml").write_text("fixture: true", encoding="utf-8")
    (tmp_path / "onboarding/00_consent.md").write_text("説明", encoding="utf-8")
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.1.0"', encoding="utf-8")
    (tmp_path / "desktop/src-tauri/Cargo.toml").write_text('[package]\nversion="0.1.0"', encoding="utf-8")
    for name in ("desktop/src-tauri/tauri.conf.json", "desktop/package.json"):
        (tmp_path / name).write_text('{"version":"0.1.0"}', encoding="utf-8")
    return tmp_path


def python_archive(path):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in {"python314._pth": "python314.zip\n.\n# import site\n",
                              "python.exe": "fixture", "pythonw.exe": "fixture",
                              "LICENSE.txt": "Python license fixture"}.items():
            archive.writestr(name, content)
    return {"version": "3.14.3",
            "url": "https://www.python.org/ftp/python/3.14.3/python-3.14.3-embed-amd64.zip",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_copy_runtime_resources_excludes_bytecode(source):
    (source / "cbs/__pycache__").mkdir()
    (source / "cbs/__pycache__/cache.pyc").write_bytes(b"cache")
    (source / "cbs/stray.pyc").write_bytes(b"cache")
    r4.copy_app(source, source / "stage/app")
    files = {p.relative_to(source / "stage/app").as_posix()
             for p in (source / "stage/app").rglob("*") if p.is_file()}
    assert files == {"cbs/__init__.py", "bootstrap/domains.yaml", "onboarding/00_consent.md",
                     "config.toml", "pyproject.toml"}


@pytest.mark.parametrize("entry", ["characters/", "input/", "data/", "tests/", "docs/", ".git/",
                                    "../outside", "C:/outside", "cbs/../../outside", "cbs\\data"])
def test_forbidden_include_stops(source, entry):
    with (source / "release/include.txt").open("a", encoding="utf-8") as stream:
        stream.write("\n" + entry)
    with pytest.raises(ValueError, match="Forbidden"):
        r4.copy_app(source, source / "stage/app")
    assert not (source / "stage/app").exists()


@pytest.mark.parametrize("name", sorted(r4.FORBIDDEN))
def test_nested_private_folder_stops(source, name):
    (source / "cbs" / name).mkdir()
    with pytest.raises(ValueError, match="Forbidden"):
        r4.copy_app(source, source / "stage/app")


def test_missing_include_and_missing_required_entry(source):
    (source / "config.toml").unlink()
    with pytest.raises(ValueError, match="Missing include source"):
        r4.copy_app(source, source / "stage/app")
    (source / "release/include.txt").write_text("cbs/", encoding="utf-8")
    with pytest.raises(ValueError, match="Missing required"):
        r4.copy_app(source, source / "stage/app")


@pytest.mark.parametrize("name", ["pyproject.toml", "desktop/src-tauri/Cargo.toml",
                                  "desktop/src-tauri/tauri.conf.json", "desktop/package.json"])
def test_version_mismatch_stops(source, name):
    assert r4.validate_versions(source) == "0.1.0"
    path = source / name
    path.write_text(path.read_text(encoding="utf-8").replace("0.1.0", "0.2.0"), encoding="utf-8")
    with pytest.raises(ValueError, match="versions disagree"):
        r4.build(source)


def test_archive_hash_and_pth(tmp_path):
    archive = tmp_path / "python.zip"
    spec = python_archive(archive)
    runtime = tmp_path / "runtime"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        r4.unpack_python(archive, runtime, {**spec, "sha256": "0" * 64})
    assert not runtime.exists()
    r4.unpack_python(archive, runtime, spec)
    lines = (runtime / "python314._pth").read_text(encoding="utf-8").splitlines()
    assert lines == ["python314.zip", ".", "Lib\\site-packages", "..\\app", "import site"]
    for name in ("python.exe", "pythonw.exe"):
        (runtime / name).unlink()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "..\\escape"])
def test_unsafe_archive_stops(tmp_path, name):
    path = tmp_path / "python.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, "bad")
    with pytest.raises(ValueError, match="Unsafe Python archive"):
        r4.unpack_python(path, tmp_path / "runtime", {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


def test_real_python_pin_is_filled_and_consistent():
    # Never run r4.build() on the real tree here: with a real pin it downloads and builds.
    spec = r4.validate_python(r4.ROOT)
    assert re.fullmatch(r"[0-9a-f]{64}", spec["sha256"])
    assert spec["url"] == f"https://www.python.org/ftp/python/{spec['version']}/python-{spec['version']}-embed-amd64.zip"


def installed_fixture(site):
    info = site / "fixture-1.2.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.4\nName: fixture\nVersion: 1.2\nLicense-Expression: MIT\n", encoding="utf-8")
    (info / "LICENSE").write_text("Permission fixture", encoding="utf-8")
    (info / "RECORD").write_text("fixture-1.2.dist-info/LICENSE,,\n", encoding="utf-8")


@pytest.mark.parametrize("skip_audit", [False, True])
def test_stubbed_complete_build(source, monkeypatch, capsys, skip_audit):
    archive = source / "fixture.zip"
    spec = python_archive(archive)
    (source / "release/python.json").write_text(json.dumps(spec), encoding="utf-8")
    calls = []
    def download(spec_arg, target):
        assert spec_arg == spec
        calls.append("download")
        target.write_bytes(archive.read_bytes())
    def install(root, site):
        calls.append("pip")
        assert root == source
        installed_fixture(site)
    def tauri(root):
        calls.append("tauri")
        assert "Permission fixture" in (root / ".tmp/release/stage/app/THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
        installer = root / ".tmp/release/target/release/bundle/nsis/fixture-setup.exe"
        installer.parent.mkdir(parents=True, exist_ok=True)
        installer.write_bytes(b"fixture-installer")
        installer.with_suffix(".exe.sig").write_text("fixture-signature", encoding="utf-8")
    monkeypatch.setattr(r4, "download_python", download)
    monkeypatch.setattr(r4, "install_dependencies", install)
    monkeypatch.setattr(r4, "run_audits", lambda root: calls.append("audits"))
    monkeypatch.setattr(r4, "cargo_metadata", lambda root: {"packages": [{"name": "fixture-rust", "version": "1.0", "license": "Apache-2.0"}]})
    monkeypatch.setattr(r4, "build_tauri", tauri)
    monkeypatch.setattr(r4, "verify_release", lambda root, installer, *, signed: calls.append(("verify", signed)))
    def runtime_audit(root, site):
        assert not (site / "bin").exists()
        calls.append("runtime audit")
    monkeypatch.setattr(r4, "audit_runtime", runtime_audit)
    # Rebuilding replaces stale stage/out content.
    for name in ("stage", "out"):
        directory = source / ".tmp/release" / name
        directory.mkdir(parents=True)
        (directory / "stale").write_text("old", encoding="utf-8")
    out = r4.build(source, skip_audit=skip_audit)
    assert calls == (([] if skip_audit else ["audits"]) + ["download", "pip"]
                     + ([] if skip_audit else ["runtime audit"]) + ["tauri", ("verify", False)])
    assert not (out / "stale").exists()
    assert (out / "fixture-setup.exe").read_bytes() == b"fixture-installer"
    # The site's fixed download name is the same bytes as the versioned installer.
    assert (out / r4.FIXED_INSTALLER).read_bytes() == b"fixture-installer"
    notice = (source / ".tmp/release/stage/app/THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    for value in ("Python 3.14.3", "Python license fixture", "fixture 1.2", "MIT", "Permission fixture", "fixture-rust 1.0", "Apache-2.0"):
        assert value in notice
    output = capsys.readouterr().out
    assert ("SKIPPED" in output) == skip_audit
    assert "17 bytes" in output
    # Synthetic executable fixtures are never left for someone to launch.
    (out / "fixture-setup.exe").unlink()
    (out / r4.FIXED_INSTALLER).unlink()
    (source / ".tmp/release/target/release/bundle/nsis/fixture-setup.exe").unlink()
    for name in ("python.exe", "pythonw.exe"):
        (source / ".tmp/release/stage/runtime" / name).unlink()


def test_release_config_and_actual_include():
    config = json.loads((r4.ROOT / "release/tauri.release.json").read_text(encoding="utf-8"))
    bundle = config["bundle"]
    assert bundle["active"] and bundle["targets"] == ["nsis"]
    nsis = bundle["windows"]["nsis"]
    assert nsis == {"installMode": "currentUser", "languages": ["Japanese"], "startMenuFolder": "Awalia",
                    "installerIcon": "icons/icon.ico", "uninstallerIcon": "icons/icon.ico",
                    "customLanguageFiles": {"Japanese": "../../release/nsis/Japanese.nsh"},
                    "installerHooks": "../../release/nsis-hooks.nsh"}
    # The installer (and its uninstaller) carry the app's own icon, not the NSIS default.
    assert (r4.ROOT / "desktop/src-tauri" / nsis["installerIcon"]).is_file()
    hook = (r4.ROOT / "desktop/src-tauri" / nsis["installerHooks"]).read_text(encoding="utf-8")
    assert 'StrCpy $INSTDIR "$LOCALAPPDATA\\Programs\\Awalia"' in hook
    assert "MUI_CUSTOMFUNCTION_GUIINIT" in hook and "NSIS_HOOK_PREINSTALL" in hook
    # R12a: only three fixed trees and the empty install root may be removed.
    assert re.findall(r'^\s*RMDir\s+(.+)$', hook, re.MULTILINE) == [
        r'/r "$INSTDIR\app"', r'/r "$INSTDIR\runtime"', r'"$INSTDIR"',
        r'/r "$LOCALAPPDATA\Awalia"',
    ]
    for source, dest in bundle["resources"].items():
        assert (r4.ROOT / "desktop/src-tauri" / source).resolve() == (r4.ROOT / ".tmp/release/stage" / dest).resolve()
    dev = json.loads((r4.ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert dev["bundle"]["active"] is False
    assert set((r4.ROOT / "release/include.txt").read_text(encoding="utf-8").splitlines()) == {"cbs/", "bootstrap/", "onboarding/", "config.toml", "pyproject.toml"}


def test_uninstall_cleanup_guards_and_install_directory_hook():
    hook = (r4.ROOT / "release/nsis-hooks.nsh").read_text(encoding="utf-8")
    # Check the complete executable statements: no alternate deletion, checkbox
    # initialization, silent fallback, or install-time cleanup may slip in.
    statements = [line.strip() for line in hook.splitlines()
                  if line.strip() and not line.lstrip().startswith(";")]
    assert statements == [
        "!define MUI_CUSTOMFUNCTION_GUIINIT AwaliaDefaultInstallDirectory",
        "Function AwaliaDefaultInstallDirectory",
        'StrCmp $INSTDIR "$LOCALAPPDATA\\Awalia" 0 done',
        'StrCpy $INSTDIR "$LOCALAPPDATA\\Programs\\Awalia"',
        "done:",
        "FunctionEnd",
        "!macro NSIS_HOOK_PREINSTALL",
        "Call AwaliaDefaultInstallDirectory",
        "SetOutPath $INSTDIR",
        "!macroend",
        "Var AwaliaOwnsApp",
        "Var AwaliaOwnsRuntime",
        "!macro NSIS_HOOK_PREUNINSTALL",
        "StrCpy $AwaliaOwnsApp 0",
        "StrCpy $AwaliaOwnsRuntime 0",
        'IfFileExists "$INSTDIR\\app\\cbs\\web\\__main__.py" 0 +2',
        "StrCpy $AwaliaOwnsApp 1",
        'IfFileExists "$INSTDIR\\runtime\\python314._pth" 0 +2',
        "StrCpy $AwaliaOwnsRuntime 1",
        "!macroend",
        "!macro NSIS_HOOK_POSTUNINSTALL",
        "${If} $UpdateMode <> 1",
        "${If} $AwaliaOwnsApp = 1",
        'RMDir /r "$INSTDIR\\app"',
        "${EndIf}",
        "${If} $AwaliaOwnsRuntime = 1",
        'RMDir /r "$INSTDIR\\runtime"',
        "${EndIf}",
        'RMDir "$INSTDIR"',
        "${If} $DeleteAppDataCheckboxState = 1",
        "SetShellVarContext current",
        'RMDir /r "$LOCALAPPDATA\\Awalia"',
        "${EndIf}",
        "${EndIf}",
        "!macroend",
    ]


def test_japanese_custom_language_is_complete_and_changes_delete_label():
    config = json.loads((r4.ROOT / "release/tauri.release.json").read_text(encoding="utf-8"))
    nsis = config["bundle"]["windows"]["nsis"]
    assert list(nsis["customLanguageFiles"]) == nsis["languages"] == ["Japanese"]
    language = (r4.ROOT / "desktop/src-tauri" / nsis["customLanguageFiles"]["Japanese"]).read_text(encoding="utf-8")
    lines = [line for line in language.splitlines() if line.strip() and not line.startswith(";")]
    matches = [re.fullmatch(r'LangString (\w+) \$\{LANG_JAPANESE\} "(.+)"', line) for line in lines]
    assert all(matches)
    strings = {match[1]: match[2] for match in matches}
    assert len(strings) == len(lines)  # No duplicate definitions.
    # Complete message set in tauri-bundler 2.10.3; custom files replace, not
    # overlay, the built-in language. Review this list when upgrading Tauri.
    assert set(strings) == {
        "addOrReinstall", "alreadyInstalled", "alreadyInstalledLong", "appRunning",
        "appRunningOkKill", "chooseMaintenanceOption", "choowHowToInstall", "createDesktop",
        "dontUninstall", "dontUninstallDowngrade", "failedToKillApp", "installingWebview2",
        "newerVersionInstalled", "older", "olderOrUnknownVersionInstalled", "silentDowngrades",
        "unableToUninstall", "uninstallApp", "uninstallBeforeInstalling", "unknown",
        "webview2AbortError", "webview2DownloadError", "webview2DownloadSuccess",
        "webview2Downloading", "webview2InstallError", "webview2InstallSuccess", "deleteAppData",
    }
    assert strings["deleteAppData"] == "会話の記録・設定・API キーもすべて削除する（元に戻せません）"
    # These tokens are replaced at runtime by utils.nsh, not Handlebars.
    for key in ("appRunning", "appRunningOkKill", "failedToKillApp"):
        assert "{{product_name}}" in strings[key]


def test_reset_refuses_outside_release(tmp_path):
    outside = tmp_path / "valuable"
    outside.mkdir()
    with pytest.raises(ValueError, match="Unsafe release"):
        r4.reset_directory(outside, tmp_path)
    assert outside.exists()


def test_external_tool_commands_are_pinned_and_fail_closed(source, monkeypatch):
    commands = []
    def record(command, **kwargs):
        assert kwargs["check"] is True
        commands.append((command, kwargs))
        return r4.subprocess.CompletedProcess(command, 0, stdout='{"packages": []}')
    monkeypatch.setattr(r4.subprocess, "run", record)
    EXTERNAL_STEPS["install_dependencies"](source, source / "site")
    pip = commands[-1][0]
    for flag in ("--require-hashes", "--only-binary=:all:", "--no-compile"):
        assert flag in pip
    for flag, value in (("--platform", "win_amd64"), ("--python-version", "3.14"), ("--abi", "cp314")):
        assert pip[pip.index(flag) + 1] == value
    assert pip[-1] == str(source / "requirements-local-embedding.lock")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(source / "ca.pem"))
    EXTERNAL_STEPS["run_audits"](source)
    # pip-audit is pinned and runs from uv's isolated environment, not the build venv.
    assert commands[-2][0][:3] == ["uvx", "--system-certs", "pip-audit==2.10.1"]
    assert "--require-hashes" in commands[-2][0] and sys.executable not in commands[-2][0]
    assert commands[-1][0] == ["cargo", "audit"]
    EXTERNAL_STEPS["audit_runtime"](source, source / "site")
    assert commands[-1][0][-2:] == ["--path", str(source / "site")]
    assert EXTERNAL_STEPS["cargo_metadata"](source) == {"packages": []}
    assert "--locked" in commands[-1][0]
    EXTERNAL_STEPS["build_tauri"](source)
    command, kwargs = commands[-1]
    assert command[1:] == ["build", "--config", str(source / "release/tauri.release.json")]
    assert kwargs["cwd"] == source / "desktop"
    assert kwargs["env"]["CARGO_TARGET_DIR"] == str(source / ".tmp/release/target")
    def vulnerable(command, **kwargs):
        raise r4.subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(r4.subprocess, "run", vulnerable)
    with pytest.raises(r4.subprocess.CalledProcessError):
        EXTERNAL_STEPS["run_audits"](source)


def test_audit_failure_stops_before_staging_or_build(source, monkeypatch):
    spec = python_archive(source / "fixture.zip")
    (source / "release/python.json").write_text(json.dumps(spec), encoding="utf-8")
    def fail(root):
        raise r4.subprocess.CalledProcessError(1, ["audit-fixture"])
    monkeypatch.setattr(r4, "run_audits", fail)
    with pytest.raises(r4.subprocess.CalledProcessError):
        r4.build(source)
    assert not (source / ".tmp/release/stage").exists()


def test_missing_python_package_license_stops(source):
    runtime = source / "runtime"
    runtime.mkdir()
    (runtime / "LICENSE.txt").write_text("Python license fixture", encoding="utf-8")
    installed_fixture(runtime / "Lib/site-packages")
    (runtime / "Lib/site-packages/fixture-1.2.dist-info/LICENSE").unlink()
    with pytest.raises(ValueError, match="No bundled license text"):
        r4.write_notices(source, runtime, {"version": "3.14.3", "url": "fixture"}, {"packages": []})


def test_reviewed_upstream_license_is_scoped_to_package_version(source):
    runtime = source / "runtime"
    runtime.mkdir()
    (runtime / "LICENSE.txt").write_text("Python fixture", encoding="utf-8")
    installed_fixture(runtime / "Lib/site-packages")
    (runtime / "Lib/site-packages/fixture-1.2.dist-info/LICENSE").unlink()
    licenses = source / "release/licenses"
    wrong = licenses / "fixture-1.1"
    wrong.mkdir(parents=True)
    (wrong / "LICENSE").write_text("Wrong version fixture", encoding="utf-8")
    spec = {"version": "3.14.3", "url": "fixture"}
    with pytest.raises(ValueError, match="No bundled license text"):
        r4.write_notices(source, runtime, spec, {"packages": []}, license_root=licenses)
    correct = licenses / "fixture-1.2"
    correct.mkdir()
    (correct / "SPDX.txt").write_text("MIT", encoding="utf-8")
    with pytest.raises(ValueError, match="No bundled license text"):
        r4.write_notices(source, runtime, spec, {"packages": []}, license_root=licenses)
    (correct / "LICENSE").write_text("Reviewed license fixture", encoding="utf-8")
    r4.write_notices(source, runtime, spec, {"packages": []}, license_root=licenses)
    text = (source / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    assert "Reviewed license fixture" in text and "Supplement license: MIT" in text
    assert "Wrong version fixture" not in text
