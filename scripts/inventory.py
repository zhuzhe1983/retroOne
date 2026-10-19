#!/usr/bin/env python3
"""Read-only, streaming ROM inventory; compatible with the NAS's Python 3.8.

Run on the storage host and redirect stdout on the caller to retain a manifest.
No file contents are read, no symlinks are followed, and no files are modified.
"""
import argparse
import collections
import datetime
import json
import os
import stat
import sys
import time


def emit(record):
    print(json.dumps(record, ensure_ascii=True, separators=(",", ":")), flush=True)


def inventory(root, sources):
    started = time.monotonic()
    groups = {}
    errors = []
    excluded = collections.Counter()
    emit({"type": "header", "schema": 1, "root": root, "sources": sources,
          "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "content_hashed": False, "excluded_directory_names": ["@eaDir"]})
    for source in sources:
        base = os.path.join(root, source)
        def onerror(exc):
            item = {"type": "error", "path": exc.filename, "error": str(exc)}
            errors.append(item)
            emit(item)
        for directory, dirs, names in os.walk(base, followlinks=False, onerror=onerror):
            kept = []
            for name in sorted(dirs):
                path = os.path.join(directory, name)
                if name == "@eaDir":
                    excluded[name] += 1
                    emit({"type": "excluded_directory", "path": os.path.relpath(path, root)})
                elif os.path.islink(path):
                    names.append(name)
                else:
                    kept.append(name)
            dirs[:] = kept
            rel_dir = os.path.relpath(directory, root)
            emit({"type": "directory", "path": rel_dir})
            for name in sorted(names):
                path = os.path.join(directory, name)
                relative = os.path.relpath(path, root)
                try:
                    s = os.lstat(path)
                    if stat.S_ISLNK(s.st_mode):
                        emit({"type": "symlink", "path": relative, "target": os.readlink(path)})
                        continue
                    if not stat.S_ISREG(s.st_mode):
                        emit({"type": "special", "path": relative, "mode": s.st_mode})
                        continue
                    extension = os.path.splitext(name)[1].lower() or "[none]"
                    emit({"type": "file", "path": relative, "size": s.st_size,
                          "mtime_ns": s.st_mtime_ns, "extension": extension})
                    parts = relative.split(os.sep)
                    group_name = "/".join(parts[:3] if source == "Pegasus G" and len(parts) > 3 and parts[1] == "Roms" else parts[:2])
                    group = groups.setdefault(group_name, {"files": 0, "bytes": 0,
                        "extensions": collections.Counter(), "samples": []})
                    group["files"] += 1
                    group["bytes"] += s.st_size
                    group["extensions"][extension] += 1
                    if len(group["samples"]) < 4:
                        group["samples"].append(relative)
                except OSError as exc:
                    onerror(exc)
    emit({"type": "summary", "groups": groups, "errors": len(errors),
          "excluded_directories": dict(excluded), "seconds": round(time.monotonic() - started, 2),
          "files": sum(g["files"] for g in groups.values()),
          "bytes": sum(g["bytes"] for g in groups.values()),
          "complete": not errors})
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("sources", nargs="+")
    args = parser.parse_args()
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        parser.error("root must be an existing directory")
    for source in args.sources:
        if source in (".", "..") or os.sep in source or not os.path.isdir(os.path.join(root, source)):
            parser.error("each source must name an existing direct child directory")
        if os.path.islink(os.path.join(root, source)):
            parser.error("source directories must not be symlinks")
    return inventory(root, args.sources)


if __name__ == "__main__":
    sys.exit(main())
