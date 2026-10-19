#!/usr/bin/env python3
"""Find exact small-game duplicates without modifying or deleting ROMs.

Single-cartridge ZIPs are compared by decompressed SHA-256, so different archive
compression, Chinese names and container timestamps do not hide identical games.
Multi-file archives are compared as complete packages. Variants with different
payloads remain distinct. A resumable SQLite cache avoids repeated reads.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retroone.platforms import PLATFORMS

LIMIT = 20 * 1024 * 1024
CARTRIDGES = {"nes", "fds", "snes", "gb", "gbc", "gba", "nds", "n64", "megadrive",
              "mastersystem", "gamegear", "sega32x", "sg1000", "pico", "atari2600",
              "atari5200", "atari7800", "jaguar", "lynx", "wonderswan", "wonderswancolor",
              "ngp", "ngpc", "pce", "virtualboy", "pokemini", "colecovision", "channelf",
              "vectrex", "odyssey2", "msx", "msx2", "supervision", "gamewatch"}


def sha(stream):
    result, size = hashlib.sha256(), 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return result.hexdigest(), size
        size += len(chunk)
        if size > LIMIT:
            raise ValueError("content exceeds the small-game scan limit")
        result.update(chunk)


def fingerprint(path, platform):
    if path.suffix.lower() == ".zip" and platform in CARTRIDGES:
        with zipfile.ZipFile(str(path)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            allowed = set(PLATFORMS[platform]["extensions"]) - {".zip", ".7z"}
            games = [item for item in entries if Path(item.filename).suffix.lower() in allowed]
            if len(games) == 1 and len(entries) < 20000 and sum(i.file_size for i in entries) <= LIMIT:
                item = games[0]
                if item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("encrypted or linked member")
                with archive.open(item) as stream:
                    value, size = sha(stream)
                return "payload", value, size
    with path.open("rb") as stream:
        value, size = sha(stream)
    kind = "payload" if platform in CARTRIDGES and path.suffix.lower() not in {".zip", ".7z", ".rar"} else "package"
    return kind, value, size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="the organized one directory")
    args = parser.parse_args()
    root = args.root.resolve()
    audit = root / ".retroone"
    audit.mkdir(exist_ok=True)
    db = sqlite3.connect(str(audit / "content-hashes.sqlite"))
    db.execute("CREATE TABLE IF NOT EXISTS fingerprints(path TEXT PRIMARY KEY,platform TEXT,size INTEGER,mtime_ns INTEGER,kind TEXT,sha256 TEXT,payload_size INTEGER,error TEXT)")
    count = 0
    for platform_dir in sorted((root / "library").iterdir()):
        platform = platform_dir.name
        if platform not in CARTRIDGES or platform_dir.is_symlink():
            continue
        extensions = set(PLATFORMS[platform]["extensions"])
        for path in sorted(platform_dir.glob("*/*/*")):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in extensions:
                continue
            st = path.stat()
            if st.st_size > LIMIT:
                continue
            rel = path.relative_to(root).as_posix()
            cached = db.execute("SELECT size,mtime_ns,error,kind FROM fingerprints WHERE path=?", (rel,)).fetchone()
            if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns and cached[2] is None:
                # Older reports called uncompressed cartridges packages. Their
                # bytes already have the right hash; compare them to ZIP payloads.
                if cached[3] == "package" and path.suffix.lower() not in {".zip", ".7z", ".rar"}:
                    db.execute("UPDATE fingerprints SET kind='payload' WHERE path=?", (rel,))
                count += 1
                continue
            try:
                kind, digest, size = fingerprint(path, platform)
                error = None
            except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                kind, digest, size, error = None, None, None, str(exc)
            db.execute("INSERT OR REPLACE INTO fingerprints VALUES(?,?,?,?,?,?,?,?)", (rel, platform, st.st_size, st.st_mtime_ns, kind, digest, size, error))
            count += 1
            if count % 250 == 0:
                db.commit()
                print(json.dumps(dict(checked=count, current=rel), ensure_ascii=False), flush=True)
    db.commit()
    groups = []
    for platform, kind, digest, number in db.execute("SELECT platform,kind,sha256,count(*) FROM fingerprints WHERE error IS NULL GROUP BY platform,kind,sha256 HAVING count(*)>1"):
        paths = [r[0] for r in db.execute("SELECT path FROM fingerprints WHERE platform=? AND kind=? AND sha256=?", (platform,kind,digest))]
        paths.sort(key=lambda p: (0 if "/new/" in p else 1 if "/pegasus/" in p else 2, p))
        groups.append(dict(platform=platform, kind=kind, sha256=digest, canonical=paths[0], copies=paths))
    report = dict(completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  scope="direct small-cartridge entries <=20MiB only; no files removed",
                  checked=count, duplicate_groups=len(groups), duplicate_copies=sum(len(g["copies"])-1 for g in groups),
                  errors=db.execute("SELECT count(*) FROM fingerprints WHERE error IS NOT NULL").fetchone()[0], groups=groups)
    temp = audit / "duplicates.json.tmp"
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(audit / "duplicates.json")
    print(json.dumps({k:v for k,v in report.items() if k!="groups"}, ensure_ascii=False), flush=True)
    db.close()


if __name__ == "__main__":
    main()
