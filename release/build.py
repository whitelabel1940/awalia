"""Assemble the Windows release using the development venv (stdlib only; the final
updater-signature check imports release/verify.py, which needs `cryptography`).

Network/build boundaries: download_python, install_dependencies, run_audits, audit_runtime,
cargo_metadata, build_tauri, verify_release, estimate_signatures, confirm.
Tests replace these functions, never their tools.

Authenticode (R9): --sign-dry-run records what Tauri asks to sign without contacting
SSL.com; --sign estimates the paid signatures, asks for confirmation and caps the build
at that number (release/sign.py).
"""
import argparse
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from datetime import datetime, timezone
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"characters", "input", "data", "tests", "docs", ".git"}
REQUIRED = {"cbs", "bootstrap", "onboarding", "config.toml", "pyproject.toml"}


def validate_versions(root):
    versions = {
        "pyproject.toml": tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "desktop/src-tauri/Cargo.toml": tomllib.loads((root / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8"))["package"]["version"],
    }
    for name in ("desktop/src-tauri/tauri.conf.json", "desktop/package.json"):
        versions[name] = json.loads((root / name).read_text(encoding="utf-8"))["version"]
    # A version bump also needs `cargo update -w`; otherwise `cargo metadata --locked` fails mid-build.
    lock = root / "desktop/src-tauri/Cargo.lock"
    if lock.is_file():
        package = tomllib.loads((root / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8"))["package"]["name"]
        versions["desktop/src-tauri/Cargo.lock"] = next(
            (p["version"] for p in tomllib.loads(lock.read_text(encoding="utf-8")).get("package", []) if p["name"] == package), None)
    if len(set(versions.values())) != 1:
        raise ValueError(f"Release versions disagree: {versions}")
    return next(iter(versions.values()))


def validate_python(root):
    spec = json.loads((root / "release/python.json").read_text(encoding="utf-8"))
    version = spec["version"]
    if not re.fullmatch(r"3\.14\.\d+", version):
        raise ValueError("Python must be a pinned 3.14 release")
    expected = f"https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip"
    if spec["url"] != expected:
        raise ValueError("Python URL must identify the pinned python.org amd64 embeddable archive")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", spec["sha256"]) or spec["sha256"] == "0" * 64:
        raise ValueError("Fill release/python.json sha256 with the verified archive SHA-256 before building")
    return spec


def copy_app(root, app):
    entries = [line.split("#", 1)[0].strip().rstrip("/")
               for line in (root / "release/include.txt").read_text(encoding="utf-8").splitlines()]
    entries = [line for line in entries if line]
    if not REQUIRED.issubset(entries):
        raise ValueError(f"Missing required include entries: {sorted(REQUIRED - set(entries))}")
    files = []
    # Validate the entire selection before copying anything; links never follow out
    # of the repository or smuggle private trees into the application.
    for entry in entries:
        path = PurePosixPath(entry)
        if (path.is_absolute() or ".." in path.parts or "\\" in entry or ":" in entry
                or any(part.casefold() in FORBIDDEN for part in path.parts)):
            raise ValueError(f"Forbidden include path: {entry}")
        source = root / entry
        if not source.exists():
            raise ValueError(f"Missing include source: {entry}")
        for item in [source, *sorted(source.rglob("*"))] if source.is_dir() else [source]:
            relative = item.relative_to(root)
            if item.is_symlink() or item.is_junction() or not item.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Linked include source: {relative}")
            if any(part.casefold() in FORBIDDEN for part in relative.parts):
                raise ValueError(f"Forbidden include source: {relative}")
            if "__pycache__" in relative.parts or item.suffix.lower() in {".pyc", ".pyo"}:
                continue
            if item.is_file():
                files.append((item, relative))
    for source, relative in files:
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def download_python(spec, destination):
    with urllib.request.urlopen(spec["url"], timeout=120) as response, destination.open("wb") as stream:
        shutil.copyfileobj(response, stream)


def unpack_python(archive, runtime, spec):
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest.lower() != spec["sha256"].lower():
        raise ValueError("Python archive SHA-256 mismatch")
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            name = member.filename
            if ("\\" in name or ":" in name or PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts):
                raise ValueError(f"Unsafe Python archive path: {name}")
        package.extractall(runtime)
    pth = runtime / "python314._pth"
    lines = [line for line in pth.read_text(encoding="utf-8").splitlines()
             if line.strip() not in {"#import site", "# import site", "import site", "Lib\\site-packages", "..\\app"}]
    pth.write_text("\n".join([*lines, "Lib\\site-packages", "..\\app", "import site", ""]), encoding="utf-8")
    for name in ("python.exe", "pythonw.exe", "LICENSE.txt"):
        if not (runtime / name).is_file():
            raise ValueError(f"Python archive missing {name}")


def install_dependencies(root, site_packages):
    subprocess.run([sys.executable, "-m", "pip", "install", "--require-hashes",
                    "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.14",
                    "--implementation", "cp", "--abi", "cp314", "--no-compile",
                    "--target", str(site_packages), "-r", str(root / "requirements-local-embedding.lock")],
                   cwd=root, check=True)


# Development-only packages in the shared lock (pytest and what only it needs).
DEV_PACKAGES = {"pytest", "pluggy", "iniconfig", "pygments"}


def _name(text):
    return re.sub(r"[-_.]+", "-", re.match(r"\s*([A-Za-z0-9._-]+)", text).group(1)).lower()


def prune_runtime(site_packages):
    """Drop pip's console-script launchers (unsigned .exe nobody runs) and development packages."""
    site_packages = Path(site_packages)
    launchers = site_packages / "bin"
    if launchers.exists():
        if launchers.is_symlink() or launchers.is_junction() or not launchers.resolve().is_relative_to(site_packages.resolve()):
            raise ValueError("Unsafe site-packages/bin")
        shutil.rmtree(launchers)
    importlib.invalidate_caches()  # importlib.metadata caches directory listings by mtime
    distributions = list(importlib.metadata.distributions(path=[str(site_packages)]))
    for dist in distributions:
        if _name(dist.metadata["Name"]) not in DEV_PACKAGES:
            continue
        paths = [Path(dist.locate_file(file)).resolve() for file in dist.files or []]  # RECORD goes too
        for path in paths:
            if path.is_relative_to(site_packages.resolve()) and path.is_file():
                path.unlink()
        for folder in sorted({path.parent for path in paths}, key=lambda p: len(p.parts), reverse=True):
            if folder != site_packages.resolve() and folder.is_relative_to(site_packages.resolve()) and folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    importlib.invalidate_caches()
    for dist in importlib.metadata.distributions(path=[str(site_packages)]):
        name = _name(dist.metadata["Name"])
        if name in DEV_PACKAGES:
            raise ValueError(f"Development package {name} is still installed")
        for requirement in dist.requires or []:
            if "extra" not in requirement and _name(requirement) in DEV_PACKAGES:
                raise ValueError(f"{name} needs development package {_name(requirement)}")


# pip-audit runs from uv's isolated tool environment, never from the build venv.
PIP_AUDIT = ["uvx", "--system-certs", "pip-audit==2.10.1", "--progress-spinner", "off"]


def audit_env(root):
    # pip-audit (requests/certifi) must trust a local HTTPS-scanning root such as Norton's.
    bundle = root / ".tmp/release/ca.pem"
    if not os.environ.get("REQUESTS_CA_BUNDLE"):
        bundle.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(root / "release/tools/ca_bundle.py"), str(bundle)],
                       cwd=root, check=True, capture_output=True)
        return {**os.environ, "REQUESTS_CA_BUNDLE": str(bundle)}
    return dict(os.environ)


def run_audits(root):
    subprocess.run([*PIP_AUDIT, "--require-hashes", "-r", str(root / "requirements-local-embedding.lock")],
                   cwd=root, env=audit_env(root), check=True)
    subprocess.run(["cargo", "audit"], cwd=root / "desktop/src-tauri", check=True)


def audit_runtime(root, site_packages):
    """Audit exactly what ships: the pruned runtime site-packages."""
    subprocess.run([*PIP_AUDIT, "--path", str(site_packages)], cwd=root, env=audit_env(root), check=True)


def cargo_metadata(root):
    result = subprocess.run(["cargo", "metadata", "--locked", "--format-version", "1"],
                            cwd=root / "desktop/src-tauri", check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout)


def write_notices(app, runtime, spec, metadata, *, license_root=None):
    chunks = [f"THIRD PARTY NOTICES\n\nPython {spec['version']} (PSF License Agreement)\nSource: {spec['url']}\n",
              (runtime / "LICENSE.txt").read_text(encoding="utf-8")]
    site = runtime / "Lib/site-packages"
    distributions = sorted(importlib.metadata.distributions(path=[str(site)]),
                           key=lambda dist: dist.metadata["Name"].lower())
    for dist in distributions:
        license_name = dist.metadata.get("License-Expression") or dist.metadata.get("License")
        if not license_name:
            license_name = "; ".join(c for c in dist.metadata.get_all("Classifier", []) if c.startswith("License ::")) or "Not declared"
        chunks.append(f"\nPython package: {dist.metadata['Name']} {dist.version}\nLicense: {license_name}\n")
        texts = []
        for file in sorted(dist.files or [], key=str):
            path = Path(dist.locate_file(file))
            if (path.is_file() and path.resolve().is_relative_to(site.resolve())
                    and (any(part.lower() == "licenses" for part in file.parts)
                         or file.name.lower().startswith(("license", "copying", "notice", "copyright")))):
                texts.append(f"--- {file} ---\n{path.read_text(encoding='utf-8', errors='replace')}")
        # Some wheels omit upstream license files. Allow reviewed, version-specific
        # copies from the upstream source package; never invent a license text.
        if not texts and license_root is not None:
            name = re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()
            supplement = license_root / f"{name}-{dist.version}"
            files = sorted(supplement.glob("*"))
            if files:
                license_name_file = supplement / "SPDX.txt"
                if license_name_file.is_file():
                    chunks.append("Supplement license: " + license_name_file.read_text(encoding="utf-8").strip())
                for file in files:
                    if file.is_file() and file.name != "SPDX.txt":
                        texts.append(f"--- reviewed upstream license: {file.name} ---\n{file.read_text(encoding='utf-8')}")
        if not texts:
            raise ValueError(f"No bundled license text found for {dist.metadata['Name']} {dist.version}; "
                             "add reviewed upstream files under release/licenses/<name>-<version>/ before release")
        chunks.extend(texts)
    chunks.append("\nRust packages (cargo metadata):\n")
    for package in sorted(metadata["packages"], key=lambda p: (p["name"], p["version"])):
        license_name = package.get("license")
        if not license_name and package.get("license_file"):
            license_name = "See license file: " + package["license_file"]
        chunks.append(f"{package['name']} {package['version']} — {license_name or 'Not declared'}\n")
    (app / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(chunks), encoding="utf-8")


RELEASE_TARGET = ".tmp/release/target"


def sign_command_config(root):
    # Arguments end up in the generated installer.nsi (!uninstfinalize): never put secrets here.
    return json.dumps({"bundle": {"windows": {"signCommand": {
        "cmd": sys.executable, "args": [str(root / "release/sign.py"), "%1"]}}}})


def build_tauri(root, *, no_updater=False, signing=None):
    cli = root / "desktop/node_modules/.bin/tauri.cmd"
    # Keep artifact discovery independent of the invoking shell's Cargo settings, and out of the
    # development target: app/ and runtime/ beside target/release/awalia.exe would make the dev
    # shell believe it is installed and switch to the installed data directory.
    build_env = {**os.environ, "CARGO_TARGET_DIR": str(root / RELEASE_TARGET)}
    command = [str(cli), "build", "--config", str(root / "release/tauri.release.json")]
    if no_updater:
        command += ["--config", '{"bundle":{"createUpdaterArtifacts":false}}']
        for key in ("TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD"):
            build_env.pop(key, None)
    if signing:
        command += ["--config", sign_command_config(root)]
        build_env.update({"AWALIA_SIGN_MODE": signing["mode"], "AWALIA_SIGN_RUN": signing["run"],
                          "AWALIA_SIGN_BUDGET": str(signing.get("budget", 0)),
                          "AWALIA_SIGN_VERSION": signing["version"]})
    # Tauri's signing diagnostics may contain secrets. Never forward or persist them.
    try:
        subprocess.run(command, cwd=root / "desktop", env=build_env, check=True,
                       capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Tauri build/signing failed (tool output withheld to protect signing credentials)") from None


def validate_signing(no_updater=False):
    if no_updater:
        print("WARNING: --no-updater: updater artifacts are DISABLED", flush=True)
        return
    if (not os.environ.get("TAURI_SIGNING_PRIVATE_KEY", "").strip()
            or "TAURI_SIGNING_PRIVATE_KEY_PASSWORD" not in os.environ):
        raise ValueError("Set TAURI_SIGNING_PRIVATE_KEY (key file path) and "
                         "TAURI_SIGNING_PRIVATE_KEY_PASSWORD before building, or explicitly use --no-updater")


def _release_module(name):
    spec = importlib.util.spec_from_file_location(f"awalia_release_{name}", Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SIGNING_SETTINGS = ("SSL_CS_CRED", "SSL_CS_USER", "SSL_CS_PASS", "SSL_CS_TOTP",
                    "AWALIA_CODESIGNTOOL_DIR", "AWALIA_SIGN_SUBJECT")
MONTHLY_SIGNATURES = 20


def validate_authenticode(mode):
    if mode != "sign":
        return
    missing = [name for name in SIGNING_SETTINGS if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError("Set " + ", ".join(missing) + " before --sign")
    if any('"' in os.environ[name] for name in SIGNING_SETTINGS[:4]):
        # sign.py quotes each value for cmd.exe; a double quote would break the command.
        raise ValueError("SSL.com credentials must not contain a double quote")
    if not (Path(os.environ["AWALIA_CODESIGNTOOL_DIR"]) / "CodeSignTool.bat").is_file():
        raise ValueError("AWALIA_CODESIGNTOOL_DIR has no CodeSignTool.bat")


def signtool():
    configured = os.environ.get("TAURI_WINDOWS_SIGNTOOL_PATH")
    if configured:
        return Path(configured)
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits/10/bin"
    found = sorted(kits.glob("*/x64/signtool.exe"))
    if not found:
        raise ValueError("signtool.exe (Windows SDK) not found; Tauri needs it to skip already-signed files")
    return found[-1]


def estimate_signatures(root, stage):
    """Paid signatures Tauri will request, by role, following tauri-bundler 2.10.3:
    awalia.exe, 5 NSIS plugins (always), unsigned .exe/.dll resources, uninstaller, installer.
    Files whose unsigned bytes are already in the signing cache cost nothing."""
    signer = _release_module("sign")
    subject = os.environ.get("AWALIA_SIGN_SUBJECT", "").strip()  # a stray space would miss every cached copy
    tool = signtool()
    def needs(path):
        return not signer.cached(signer.sha256(path), subject)
    plugins = Path(os.environ.get("LOCALAPPDATA", "")) / "tauri/NSIS/Plugins/x86-unicode"
    plugin_files = [plugins / ("additional/" if name == "nsis_tauri_utils.dll" else "") / name for name in signer.NSIS_PLUGINS]
    resources = []
    for path in sorted(p for p in stage.rglob("*") if p.is_file() and p.suffix.lower() in {".exe", ".dll"}):
        signed = subprocess.run([str(tool), "verify", "/pa", str(path)], capture_output=True).returncode == 0
        if not signed:
            resources.append(path)
    return {"main": 1,
            "nsis-plugin": sum(1 for p in plugin_files if not p.is_file() or needs(p)),
            "resource": sum(1 for p in resources if needs(p)),
            "uninstaller": 1, "installer": 1,
            "resource_files": [str(p.relative_to(stage)) for p in resources]}


def confirm(message):
    print(message, flush=True)
    return input("Type yes to sign: ").strip() == "yes"


def approve_signing(root, stage, version):
    estimate = estimate_signatures(root, stage)
    total = sum(v for k, v in estimate.items() if k != "resource_files")
    month = datetime.now().astimezone().strftime("%Y-%m")
    used = _release_module("sign").attempts(month=month)
    lines = [f"Authenticode for Awalia {version}: up to {total} paid signatures",
             *(f"  {k}: {v}" for k, v in estimate.items() if k != "resource_files"),
             *(f"    {name}" for name in estimate["resource_files"]),
             f"This month ({month}) so far: {used}; after this build: up to {used + total} of {MONTHLY_SIGNATURES}"]
    if used + total > MONTHLY_SIGNATURES:
        lines.append(f"WARNING: exceeds the {MONTHLY_SIGNATURES} included signatures; extra ones are charged")
    if not confirm("\n".join(lines)):
        raise ValueError("Signing not confirmed; nothing was signed")
    return total


def check_signing_run(mode, run):
    entries = [e for e in _release_module("sign").read_ledger(mode) if e.get("run") == run]
    final = {}
    for entry in entries:
        if entry.get("result") != "attempt":
            final.setdefault(entry["role"], []).append(entry)
    summary = ", ".join(f"{role} {len(items)}" for role, items in sorted(final.items()))
    print(f"Signing {'dry run' if mode == 'record' else 'run'} {run}: {summary or 'no calls'}", flush=True)
    for role in ("main", "uninstaller", "installer"):
        if len(final.get(role, [])) != 1:
            # NSIS ignores the uninstaller command's exit code; this is where a failure shows.
            raise ValueError(f"Expected exactly one {role} signing call, found {len(final.get(role, []))}")
    if mode == "sign":
        bad = [f"{e['role']} {e['name']}: {e['result']}" for items in final.values() for e in items
               if e["result"] not in {"signed", "cached", "already"}]
        if bad:
            raise ValueError("Signing incomplete: " + "; ".join(bad))
    if mode == "record":
        for role, items in sorted(final.items()):
            for entry in items:
                print(f"  {role:12} {entry['name']}  now {entry.get('status')}, would {entry.get('would')}")
    return final


# The site links https://github.com/whitelabel1940/awalia/releases/latest/download/<this name>.
# GitHub resolves "latest/download/<name>" to the asset of that name in the newest release, so
# every release carries this byte-identical copy next to the versioned installer. The updater's
# latest.json keeps pointing to the versioned file.
FIXED_INSTALLER = "Awalia-setup-x64.exe"


def verify_release(root, installer, *, signed):
    """Updater .sig against the final (Authenticode-signed) bytes; installer signature when signing."""
    verifier = _release_module("verify")
    signature = installer.with_suffix(installer.suffix + ".sig").read_text(encoding="utf-8")
    trusted = verifier.verify_updater_signature(verifier.pubkey(root), installer, signature)
    print(f"Updater signature matches the final installer ({trusted})", flush=True)
    if signed:
        signer = verifier.sign
        if not signer.is_ours(signer.authenticode(installer), os.environ["AWALIA_SIGN_SUBJECT"]):
            raise ValueError("Installer is not signed by us with a timestamp")


def write_update_manifest(root, out, version, installer):
    signature = installer.with_suffix(installer.suffix + ".sig")
    if not signature.is_file() or not signature.read_text(encoding="utf-8").strip():
        raise ValueError("Tauri produced no updater signature")
    notes = ""
    news = root / "news/news.json"
    if news.is_file():
        latest = json.loads(news.read_text(encoding="utf-8")).get("latest", {})
        if latest.get("version") == version and isinstance(latest.get("notes"), str):
            notes = latest["notes"]
    shutil.copy2(signature, out / signature.name)
    manifest = {"version": version, "notes": notes,
                "pub_date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "platforms": {"windows-x86_64": {
                    "signature": signature.read_text(encoding="utf-8").strip(),
                    "url": f"https://github.com/whitelabel1940/awalia/releases/download/v{version}/{quote(installer.name)}"}}}
    (out / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reset_directory(path, root):
    # Resolve and check the final target before any recursive deletion.
    expected = (root / ".tmp/release").resolve()
    if (not expected.is_relative_to(root.resolve()) or path.is_symlink() or path.is_junction()
            or not path.resolve().is_relative_to(expected) or path.resolve() == expected):
        raise ValueError(f"Unsafe release directory: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def build(root=ROOT, *, skip_audit=False, no_updater=False, authenticode=None):
    """authenticode: None (unsigned), "record" (dry run, no SSL.com) or "sign" (paid)."""
    validate_signing(no_updater)
    validate_authenticode(authenticode)
    root = root.resolve()
    version = validate_versions(root)
    spec = validate_python(root)  # Placeholder must stop before network and staging.
    if skip_audit:
        print("WARNING: --skip-audit: pip-audit and cargo audit were SKIPPED", flush=True)
    else:
        run_audits(root)
    stage = root / ".tmp/release/stage"
    out = root / ".tmp/release/out"
    reset_directory(stage, root)
    reset_directory(out, root)
    app, runtime = stage / "app", stage / "runtime"
    copy_app(root, app)
    archive = stage / "python.zip"
    download_python(spec, archive)
    unpack_python(archive, runtime, spec)
    archive.unlink()
    install_dependencies(root, runtime / "Lib/site-packages")
    prune_runtime(runtime / "Lib/site-packages")
    if not skip_audit:
        audit_runtime(root, runtime / "Lib/site-packages")
    write_notices(app, runtime, spec, cargo_metadata(root), license_root=root / "release/licenses")
    # Remove previous installers so a failed/new build cannot report an old one.
    bundle = root / RELEASE_TARGET / "release/bundle/nsis"
    if bundle.exists():
        for old in [*bundle.glob("*.exe"), *bundle.glob("*.exe.sig")]:
            old.unlink()
    signing = None
    if authenticode:
        # Everything that can fail cheaply (audits, staging, pip) is done before any signature.
        run = datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()
        budget = approve_signing(root, stage, version) if authenticode == "sign" else 0
        signing = {"mode": authenticode, "run": run, "budget": budget, "version": version}
    kwargs = {"signing": signing} if signing else {}
    if no_updater:
        build_tauri(root, no_updater=True, **kwargs)
    else:
        build_tauri(root, **kwargs)
    if signing:
        check_signing_run(authenticode, signing["run"])
    installers = sorted(bundle.glob("*.exe"))
    if not installers:
        raise ValueError("Tauri produced no NSIS installer in " + RELEASE_TARGET)
    if len(installers) != 1:
        raise ValueError("Expected exactly one NSIS installer")
    for installer in installers:
        target = out / installer.name
        shutil.copy2(installer, target)
        shutil.copy2(installer, out / FIXED_INSTALLER)
        print(f"Awalia {version}: {target} ({target.stat().st_size:,} bytes)")
        print(f"Fixed-name copy for the site link: {out / FIXED_INSTALLER}")
        if not no_updater:
            verify_release(root, installer, signed=authenticode == "sign")
            write_update_manifest(root, out, version, installer)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-audit", action="store_true", help="explicitly skip BOTH dependency audits")
    parser.add_argument("--no-updater", action="store_true", help="explicitly build without signed updater artifacts")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sign", action="store_const", const="sign", dest="authenticode",
                       help="Authenticode-sign through SSL.com (paid; estimates and asks first)")
    group.add_argument("--sign-dry-run", action="store_const", const="record", dest="authenticode",
                       help="record what Tauri would sign, without contacting SSL.com")
    args = parser.parse_args()
    try:
        build(skip_audit=args.skip_audit, no_updater=args.no_updater, authenticode=args.authenticode)
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Release stopped: {error}\n")


if __name__ == "__main__":
    main()
