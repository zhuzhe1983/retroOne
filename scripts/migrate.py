#!/usr/bin/env python3
"""Preserve and classify the NAS collections using independent Btrfs CoW copies.

Default prints a plan. --apply resumes only task-owned staging directories; never
overwrites a final collection or deletes source files. --verify walks every source
and checks that its mapped destination exists with matching type/size/mtime. Copy
success is a kernel-verified reflink, not a claim of an independent full hash audit.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retroone.platforms import resolve_platform

SOURCE_NAMES = ("new", "old", "Pegasus G")
PRIORITY = {"nes": 0, "snes": 1, "gb": 2, "gbc": 3, "gba": 4, "dos": 5,
            "psx": 6, "psp": 7, "ps2": 8, "megadrive": 9, "windows": 30,
            "teknoparrot": 90, "archive": 100}


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def plan(root):
    items = []
    for origin in SOURCE_NAMES:
        source = root / origin
        if not source.is_dir() or source.is_symlink():
            raise ValueError("Missing or symlinked source: " + str(source))
        label = "pegasus" if origin == "Pegasus G" else origin
        for child in sorted(source.iterdir()):
            if child.name == "@eaDir":
                # Synology generated thumbnails, not collection data.
                items.append(dict(source=str(child.relative_to(root)), destination="archive/" + label + "/@eaDir", platform="archive"))
                continue
            if origin == "Pegasus G" and child.name == "Roms" and child.is_dir():
                for collection in sorted(child.iterdir()):
                    slug = resolve_platform(collection.name)
                    destination = Path("library") / slug / label / collection.name
                    if not collection.is_dir() or collection.is_symlink():
                        destination = Path("archive") / label / "Roms" / collection.name
                    items.append(dict(source=str(collection.relative_to(root)), destination=str(destination), platform=slug))
            else:
                slug = resolve_platform(child.name) if origin != "Pegasus G" else "archive"
                destination = Path("library") / slug / label / child.name
                if origin == "Pegasus G" or not child.is_dir() or child.name.startswith((".", "#", "@")) or child.is_symlink():
                    destination = Path("archive") / label / child.name
                    slug = "archive"
                items.append(dict(source=str(child.relative_to(root)), destination=str(destination), platform=slug))
    return sorted(items, key=lambda p: (PRIORITY.get(p["platform"], 20), SOURCE_NAMES.index(p["source"].split("/")[0]), p["source"]))


class Log:
    def __init__(self, directory):
        self.directory = directory
        self.fp = (directory / "migration.jsonl").open("a", encoding="utf-8")

    def emit(self, kind, **fields):
        record = dict(type=kind, time=stamp(), **fields)
        line = json.dumps(record, ensure_ascii=False)
        self.fp.write(line + "\n")
        self.fp.flush()
        print(line, flush=True)

    def status(self, **fields):
        data = dict(updated_at=stamp(), **fields)
        temp = self.directory / "migration-status.json.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.directory / "migration-status.json")


def read_journal(directory):
    complete = set()
    p = directory / "migration.jsonl"
    if p.exists():
        for line in p.open(encoding="utf-8"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "copied":
                complete.add(row["destination"])
    return complete


def apply(root, output, items, log):
    complete = read_journal(log.directory)
    staging = output / ".retroone" / "staging"
    staging.mkdir(exist_ok=True)
    failures = []
    for index, item in enumerate(items):
        source, dest = root / item["source"], output / item["destination"]
        log.status(phase="copying", total_collections=len(items), completed_collections=index,
                   current=item["source"], errors=failures, verification="pending")
        if item["destination"] in complete and (dest.exists() or dest.is_symlink()):
            continue
        if dest.exists() or dest.is_symlink():
            failures.append(dict(source=item["source"], error="Destination exists without completion record; preserving it"))
            log.emit("error", **failures[-1])
            continue
        temp = staging / hashlib.sha256(item["source"].encode()).hexdigest()[:24]
        marker = staging / (temp.name + ".json")
        if temp.exists() and not marker.exists():
            raise RuntimeError("Refusing unknown staging directory " + str(temp))
        marker.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        started = time.monotonic()
        log.emit("copy_started", **item)
        if source.is_dir() and not source.is_symlink():
            temp.mkdir(exist_ok=True)
            args = ["cp", "-a", "--reflink=always", "--no-clobber", "--", str(source) + "/.", str(temp) + "/"]
        else:
            args = ["cp", "-a", "--reflink=always", "--no-clobber", "--", str(source), str(temp)]
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
        if p.returncode:
            failures.append(dict(source=item["source"], error=p.stderr[-4000:], exit=p.returncode))
            log.emit("error", **failures[-1])
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(temp), str(dest))
        log.emit("copied", **item, method="reflink", seconds=round(time.monotonic()-started, 2))
        complete.add(item["destination"])
    log.status(phase="copied" if not failures else "copy_errors", total_collections=len(items),
               completed_collections=len(complete), errors=failures, verification="pending")
    return failures


def walk_entries(path):
    yield path
    if path.is_dir() and not path.is_symlink():
        def fail(error):
            raise error
        for directory, dirs, names in os.walk(str(path), followlinks=False, onerror=fail):
            for name in dirs + names:
                yield Path(directory) / name


def verify(root, output, items, log):
    db = sqlite3.connect(str(log.directory / "migration-audit.sqlite"))
    db.execute("CREATE TABLE IF NOT EXISTS files(source TEXT PRIMARY KEY,destination TEXT,size INTEGER,mtime_ns INTEGER,kind TEXT,verified_at TEXT,error TEXT)")
    files = total_bytes = checked = 0
    errors = []
    symlinks = []
    try:
        for index, item in enumerate(items):
            source_base = root / item["source"]
            destination_base = output / item["destination"]
            log.status(phase="verifying", current=item["source"], total_collections=len(items),
                       completed_collections=index, verified_files=files, verified_bytes=total_bytes,
                       errors=errors[:50], verification="in_progress")
            try:
                for source in walk_entries(source_base):
                    destination = destination_base / source.relative_to(source_base)
                    relative = str(source.relative_to(root))
                    error, kind, size, mtime = None, "unknown", 0, 0
                    try:
                        src, dst = source.lstat(), destination.lstat()
                        size, mtime = src.st_size, src.st_mtime_ns
                        if stat.S_IFMT(src.st_mode) != stat.S_IFMT(dst.st_mode):
                            raise ValueError("file type differs")
                        if stat.S_ISREG(src.st_mode):
                            kind = "file"
                            if size != dst.st_size or mtime != dst.st_mtime_ns:
                                raise ValueError("size or mtime differs")
                            if src.st_dev == dst.st_dev and src.st_ino == dst.st_ino:
                                raise ValueError("source and destination are hard linked")
                            files += 1
                            total_bytes += size
                        elif stat.S_ISDIR(src.st_mode):
                            kind = "directory"
                        elif stat.S_ISLNK(src.st_mode):
                            kind = "symlink"
                            if os.readlink(str(source)) != os.readlink(str(destination)):
                                raise ValueError("symlink differs")
                            # All symlinks need explicit dependency review before deleting originals.
                            symlinks.append(dict(path=relative, target=os.readlink(str(source))))
                        else:
                            kind = "special"
                            raise ValueError("special filesystem entry needs manual review")
                    except (OSError, ValueError) as exc:
                        error = str(exc)
                        errors.append(dict(source=relative, error=error))
                    db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                               (relative, str(destination.relative_to(output)), size, mtime, kind, stamp(), error))
                    checked += 1
                    if checked % 1000 == 0:
                        db.commit()
                db.commit()
                log.emit("collection_verified", **item, checked=checked, errors=len(errors))
            except OSError as exc:
                errors.append(dict(source=item["source"], error=str(exc)))
        result = dict(phase="verification_complete", files=files, bytes=total_bytes, checked_entries=checked,
                      errors=errors, symlinks_needing_review=symlinks, full_content_hashes=False,
                      verification="reflink_copy_plus_complete_type_size_mtime_inventory",
                      originals_preserved=True, deletion_authorized=False)
        log.emit("verification_complete", **{k:v for k,v in result.items() if k != "phase"})
        log.status(**result)
        return result
    finally:
        db.commit()
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/volume2/rom"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    output = root / "one"
    if output.is_symlink():
        parser.error("one must not be a symlink")
    items = plan(root)
    if not args.apply and not args.verify:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    output.mkdir(exist_ok=True)
    state = output / ".retroone"
    state.mkdir(exist_ok=True)
    with (state / "migration.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log = Log(state)
        (state / "migration-plan.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        failures = apply(root, output, items, log) if args.apply else []
        if args.verify:
            result = verify(root, output, items, log)
            return 1 if failures or result["errors"] else 0
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
