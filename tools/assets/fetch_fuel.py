#!/usr/bin/env python3
"""Fetch the Gazebo Fuel models pinned in assets/fuel.lock (task P1-T02).

Fuel models are not vendored: the lock file records, per model, the owner, the version, the
SHA-256 of the archive Fuel serves for that version and the licence, and this script downloads
the archives, verifies them and extracts each into a directory on the Gazebo resource path.
The result is reproducible without network access once the archives are cached, and a changed
upstream archive is refused rather than silently used.

Usage:
  fetch_fuel.py [fetch] [--lock FILE] [--dest DIR] [--quiet]
      Download the archives missing from <dest>/archives, verify every archive against the lock,
      extract the models whose directory is missing or stale into <dest>/models/<dir>. Idempotent
      and offline-safe when everything is already in place.
  fetch_fuel.py check [--lock FILE] [--dest DIR]
      Verify without network access: every archive present with the right digest and every model
      directory extracted from it. Exit 1 when anything is missing or differs.
  fetch_fuel.py add OWNER/NAME [--version N] [--dir NAME] [--used-by WORLD ...] [--lock FILE] [--dest DIR]
      Pin a model: query Fuel for its metadata (latest version unless --version), download the
      archive, record owner, version, directory, licence, size and SHA-256 in the lock, then fetch.

The layout under --dest (default assets/fuel, ignored by git):
  archives/<dir>-v<version>.zip     the archive exactly as served by Fuel
  models/<dir>/                     the extracted model, with a .fieldproof-fuel.json stamp
The directory name <dir> is the lower-case, underscored model name by default, which is what the
`model://` URIs inside OpenRobotics models use (the model "Euro pallet" refers to
model://euro_pallet/meshes/pallet.dae), so a world includes it as `model://<dir>`.

Needs only the Python standard library and PyYAML (both on the host and in the dev container).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = REPO / "assets" / "fuel.lock"
DEFAULT_DEST = REPO / "assets" / "fuel"
STAMP = ".fieldproof-fuel.json"
API_VERSION = "fieldproof.dev/v0alpha1"
KIND = "FuelLock"
LOCK_FIELDS = ("name", "owner", "version", "dir", "licence", "licence_url", "size_bytes", "sha256")
RETRIES = 3
TIMEOUT_S = 120
# Fuel licence names as the API reports them, mapped to SPDX identifiers.
SPDX = {
    "Creative Commons Zero v1.0 Universal": "CC0-1.0",
    "Creative Commons Attribution 4.0 International": "CC-BY-4.0",
    "Creative Commons Attribution Share Alike 4.0 International": "CC-BY-SA-4.0",
    "Creative Commons Attribution Non Commercial 4.0 International": "CC-BY-NC-4.0",
    "Creative Commons Attribution Non Commercial No Derivatives 4.0 International": "CC-BY-NC-ND-4.0",
    "Creative Commons Attribution Non Commercial Share Alike 4.0 International": "CC-BY-NC-SA-4.0",
    "Creative Commons Attribution No Derivatives 4.0 International": "CC-BY-ND-4.0",
}

Entry = dict[str, Any]


class FetchError(Exception):
    """A lock entry could not be satisfied (network, digest or archive problem)."""


# ------------------------------------------------------------------------------------- lock file
def read_lock(path: Path) -> tuple[list[str], dict[str, Any]]:
    """Return the lock's leading comment lines and its parsed content."""
    text = path.read_text()
    header = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            header.append(line)
        else:
            break
    data = yaml.safe_load(text) or {}
    if data.get("apiVersion") != API_VERSION or data.get("kind") != KIND:
        raise FetchError(f"{path}: expected apiVersion {API_VERSION} and kind {KIND}")
    data.setdefault("models", [])
    for entry in data["models"]:
        missing = [field for field in LOCK_FIELDS if field not in entry]
        if missing:
            raise FetchError(f"{path}: model {entry.get('name')!r} lacks {', '.join(missing)}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])):
            raise FetchError(f"{path}: model {entry['name']!r} has no SHA-256 digest")
    return header, data


class _IndentedDumper(yaml.SafeDumper):  # type: ignore[misc]  # PyYAML ships no types
    """Indent sequences under their key, as the repository's YAML style does."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:  # noqa: ARG002
        super().increase_indent(flow, False)


def write_lock(path: Path, header: list[str], data: dict[str, Any]) -> None:
    data["models"] = sorted(data["models"], key=lambda e: str(e["dir"]))
    body = yaml.dump(data, Dumper=_IndentedDumper, sort_keys=False, allow_unicode=True, width=100)
    path.write_text("\n".join([*header, body.rstrip("\n")]) + "\n")


def archive_url(server: str, entry: Entry) -> str:
    name = urllib.parse.quote(str(entry["name"]))
    return f"{server}/1.0/{entry['owner']}/models/{name}/{entry['version']}/{name}.zip"


def archive_path(dest: Path, entry: Entry) -> Path:
    return dest / "archives" / f"{entry['dir']}-v{entry['version']}.zip"


def model_dir(dest: Path, entry: Entry) -> Path:
    return dest / "models" / str(entry["dir"])


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stamp_of(entry: Entry) -> dict[str, Any]:
    return {key: entry[key] for key in ("name", "owner", "version", "sha256")}


# ------------------------------------------------------------------------------------- network
def download(url: str, target: Path, quiet: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            if not quiet:
                print(f"  downloading {url}" + (f" (attempt {attempt})" if attempt > 1 else ""))
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:  # noqa: S310
                with partial.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
            partial.replace(target)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            partial.unlink(missing_ok=True)
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise FetchError(f"could not download {url}: {last_error}")


def fuel_metadata(server: str, owner: str, name: str) -> dict[str, Any]:
    url = f"{server}/1.0/{owner}/models/{urllib.parse.quote(name)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:  # noqa: S310
            data: dict[str, Any] = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise FetchError(f"could not read the metadata of {owner}/{name} from {url}: {error}") from error
    return data


# ------------------------------------------------------------------------------------- extraction
def extract(archive: Path, target: Path) -> None:
    """Extract the archive into a fresh directory, refusing entries that escape it."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            destination = (target / member.filename).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise FetchError(f"{archive.name}: entry {member.filename!r} escapes the model directory")
        bundle.extractall(target)
    if not (target / "model.config").is_file():
        raise FetchError(f"{archive.name}: no model.config at the archive root")


def archive_ok(dest: Path, entry: Entry) -> bool:
    path = archive_path(dest, entry)
    return path.is_file() and sha256_of(path) == entry["sha256"]


def model_ok(dest: Path, entry: Entry) -> bool:
    stamp = model_dir(dest, entry) / STAMP
    if not stamp.is_file():
        return False
    try:
        recorded = json.loads(stamp.read_text())
    except ValueError:
        return False
    return bool(recorded == stamp_of(entry)) and (model_dir(dest, entry) / "model.config").is_file()


def fetch_entry(server: str, dest: Path, entry: Entry, quiet: bool) -> str:
    """Bring one model up to date; return what was done."""
    if model_ok(dest, entry) and archive_ok(dest, entry):
        return "up to date"
    archive = archive_path(dest, entry)
    action = "extracted"
    if not archive_ok(dest, entry):
        download(archive_url(server, entry), archive, quiet)
        digest = sha256_of(archive)
        if digest != entry["sha256"]:
            archive.unlink()
            raise FetchError(
                f"{entry['owner']}/{entry['name']} v{entry['version']}: the archive's SHA-256 is "
                f"{digest}, the lock says {entry['sha256']}; refusing it (update the lock on purpose)"
            )
        action = "downloaded and extracted"
    extract(archive, model_dir(dest, entry))
    (model_dir(dest, entry) / STAMP).write_text(json.dumps(stamp_of(entry), indent=2) + "\n")
    return action


# ------------------------------------------------------------------------------------- commands
def cmd_fetch(args: argparse.Namespace) -> int:
    _, lock = read_lock(args.lock)
    failures = 0
    for entry in lock["models"]:
        try:
            outcome = fetch_entry(lock["server"], args.dest, entry, args.quiet)
            if not args.quiet:
                print(f"{entry['dir']}: {outcome}")
        except FetchError as error:
            failures += 1
            print(f"{entry['dir']}: {error}", file=sys.stderr)
    if failures:
        print(f"fetch_fuel: {failures} model(s) could not be fetched", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{len(lock['models'])} Fuel model(s) ready under {args.dest / 'models'}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    _, lock = read_lock(args.lock)
    problems = []
    for entry in lock["models"]:
        if not archive_ok(args.dest, entry):
            problems.append(f"{entry['dir']}: archive missing or its digest differs from the lock")
        if not model_ok(args.dest, entry):
            problems.append(f"{entry['dir']}: model directory missing or extracted from another version")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"{len(lock['models'])} Fuel model(s) verified under {args.dest}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    header, lock = read_lock(args.lock)
    owner, _, name = args.model.partition("/")
    if not owner or not name:
        print("fetch_fuel add: expected OWNER/NAME", file=sys.stderr)
        return 2
    metadata = fuel_metadata(lock["server"], owner, name)
    version = args.version or int(metadata["version"])
    directory = args.dir or re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    entry: Entry = {
        "name": name,
        "owner": owner,
        "version": version,
        "dir": directory,
        "licence": SPDX.get(str(metadata.get("license_name")), str(metadata.get("license_name"))),
        "licence_url": str(metadata.get("license_url", "")),
        "size_bytes": 0,
        "sha256": "0" * 64,
        "used_by": list(args.used_by or []),
    }
    archive = archive_path(args.dest, entry)
    download(archive_url(lock["server"], entry), archive, args.quiet)
    entry["sha256"] = sha256_of(archive)
    entry["size_bytes"] = archive.stat().st_size
    lock["models"] = [e for e in lock["models"] if e["dir"] != directory] + [entry]
    write_lock(args.lock, header, lock)
    outcome = fetch_entry(lock["server"], args.dest, entry, args.quiet)
    print(f"{directory}: {owner}/{name} v{version} ({entry['licence']}) pinned, {outcome}")
    return 0


def common_options(parser: argparse.ArgumentParser, on_subcommand: bool) -> None:
    """--lock, --dest and --quiet, accepted before and after the subcommand.

    The copies on a subcommand carry no default so that they only override the main parser's
    values when given (argparse applies subparser defaults last).
    """
    suppress = argparse.SUPPRESS
    parser.add_argument(
        "--lock",
        type=Path,
        default=suppress if on_subcommand else DEFAULT_LOCK,
        help="lock file (assets/fuel.lock)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=suppress if on_subcommand else DEFAULT_DEST,
        help="cache directory (assets/fuel)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=suppress if on_subcommand else False,
        help="print only problems",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common_options(parser, on_subcommand=False)
    commands = parser.add_subparsers(dest="command")
    common_options(commands.add_parser("fetch", help="download, verify and extract (the default)"), True)
    common_options(commands.add_parser("check", help="verify offline"), True)
    add = commands.add_parser("add", help="pin a model in the lock")
    common_options(add, on_subcommand=True)
    add.add_argument("model", help="OWNER/NAME as on fuel.gazebosim.org")
    add.add_argument("--version", type=int, help="model version (default: the latest)")
    add.add_argument("--dir", help="directory name on the resource path (default: from the name)")
    add.add_argument("--used-by", nargs="*", help="worlds that include the model")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(args)
        if args.command == "add":
            return cmd_add(args)
        return cmd_fetch(args)
    except FetchError as error:
        print(f"fetch_fuel: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
