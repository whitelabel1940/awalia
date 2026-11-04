"""Assemble the Windows release using the development venv (stdlib only).

Network/build boundaries: download_python, install_dependencies, run_audits,
cargo_metadata, build_tauri. Tests replace these functions, never their tools.
"""
import argparse
import hashlib
import importlib.metadata
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


def run_audits(root):
    subprocess.run([sys.executable, "-m", "pip_audit", "--require-hashes", "-r",
                    str(root / "requirements-local-embedding.lock")], cwd=root, check=True)
    subprocess.run(["cargo", "audit"], cwd=root / "desktop/src-tauri", check=True)


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


def build_tauri(root, *, no_updater=False):
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


def build(root=ROOT, *, skip_audit=False, no_updater=False):
    validate_signing(no_updater)
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
    write_notices(app, runtime, spec, cargo_metadata(root), license_root=root / "release/licenses")
    # Remove previous installers so a failed/new build cannot report an old one.
    bundle = root / RELEASE_TARGET / "release/bundle/nsis"
    if bundle.exists():
        for old in [*bundle.glob("*.exe"), *bundle.glob("*.exe.sig")]:
            old.unlink()
    if no_updater:
        build_tauri(root, no_updater=True)
    else:
        build_tauri(root)
    installers = sorted(bundle.glob("*.exe"))
    if not installers:
        raise ValueError("Tauri produced no NSIS installer in " + RELEASE_TARGET)
    if len(installers) != 1:
        raise ValueError("Expected exactly one NSIS installer")
    for installer in installers:
        target = out / installer.name
        shutil.copy2(installer, target)
        print(f"Awalia {version}: {target} ({target.stat().st_size:,} bytes)")
        if not no_updater:
            write_update_manifest(root, out, version, installer)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-audit", action="store_true", help="explicitly skip BOTH dependency audits")
    parser.add_argument("--no-updater", action="store_true", help="explicitly build without signed updater artifacts")
    args = parser.parse_args()
    try:
        build(skip_audit=args.skip_audit, no_updater=args.no_updater)
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Release stopped: {error}\n")


if __name__ == "__main__":
    main()
