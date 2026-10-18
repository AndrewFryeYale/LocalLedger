"""Bundled demo dataset: a snapshot of a real run so a fresh clone is browsable in minutes, with no ingest and no key.

`ll demo-export` (maintainer) writes ./demo/:
  localledger.db.gz   the SQLite DB (companies, documents, signals, notes, models, war rooms, leads, LLM ledger)
  outputs/            the result files the pages read (backtest, tokenomics, red team, newsletter, company models)
  rawdocs/            stored source documents for DEMO_RAW_TICKERS, so the war-room replay runs offline
  llm_cache/          cached gpt-6-luna responses (outputs only); identical prompts replay at no cost
  manifest.json       what was exported, when, with counts
`ll demo-load` copies it into place. It never overwrites an existing database unless --force, and then it keeps a
.bak copy first. (Directory names avoid raw/ out/ .cache/, which .gitignore excludes everywhere.)
"""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from localledger.config import get_settings

DEMO_RAW_TICKERS = ["PCYO"]  # `make demo` replays the PCYO war room
OUTPUT_FILES = [
    "backtest/results.json",
    "backtest/summary.md",
    "tokenomics/tokenomics.json",
    "tokenomics/tokenomics.md",
    "safety/demo.json",
    "safety/demo.md",
    "bench/summary.md",
]
OUTPUT_GLOBS = ["newsletter/*", "models/*.yaml"]
DROP_TABLES = ["presence"]  # who was online when the snapshot was taken


def demo_dir() -> Path:
    return get_settings().root / "demo"


def db_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise RuntimeError("the demo dataset loads into SQLite only (DATABASE_URL must be sqlite:///...)")
    return Path(url.removeprefix("sqlite:///"))


def _copy(src: Path, dst: Path, overwrite: bool = True) -> bool:
    if not src.exists() or (dst.exists() and not overwrite):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def export(dest: Path | None = None) -> dict:
    s = get_settings()
    dest = dest or demo_dir()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    snap = dest / "snapshot.db"
    src, con = sqlite3.connect(db_path()), sqlite3.connect(snap)
    try:
        src.backup(con)
        con.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm beside it
        for t in DROP_TABLES:
            con.execute(f"DELETE FROM {t}")
        # Newsletter paths become relative to out/ so they resolve on any machine.
        for iid, html, md in con.execute("SELECT id, path_html, path_md FROM newsletter_issues").fetchall():
            con.execute(
                "UPDATE newsletter_issues SET path_html = ?, path_md = ? WHERE id = ?",
                (f"newsletter/{Path(html).name}", f"newsletter/{Path(md).name}", iid),
            )
        counts = {
            t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("companies", "documents", "signals", "notes", "company_models", "warroom_sessions", "leads")
        }
        docs = con.execute(
            f"SELECT path, text_path FROM documents WHERE ticker IN ({','.join('?' * len(DEMO_RAW_TICKERS))})",
            DEMO_RAW_TICKERS,
        ).fetchall()
        con.commit()
        con.execute("VACUUM")
    finally:
        src.close()
        con.close()
    with snap.open("rb") as f, gzip.open(dest / "localledger.db.gz", "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)
    snap.unlink()

    outputs = [f for f in OUTPUT_FILES if _copy(s.out_dir / f, dest / "outputs" / f)]
    for pattern in OUTPUT_GLOBS:
        for p in sorted(s.out_dir.glob(pattern)):
            rel = p.relative_to(s.out_dir).as_posix()
            if _copy(p, dest / "outputs" / rel):
                outputs.append(rel)
    raw = [rel for pair in docs for rel in pair if rel and _copy(s.raw_dir / rel, dest / "rawdocs" / rel)]
    cache = s.cache_dir / "llm"
    n_cache = 0
    if cache.exists():
        shutil.copytree(cache, dest / "llm_cache")
        n_cache = sum(1 for _ in (dest / "llm_cache").rglob("*.json"))

    manifest = {
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": counts,
        "outputs": outputs,
        "raw_documents": len(raw),
        "raw_tickers": DEMO_RAW_TICKERS,
        "llm_cache_entries": n_cache,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def has_data() -> bool:
    p = db_path()
    if not p.exists():
        return False
    con = sqlite3.connect(p)
    try:
        return con.execute("SELECT count(*) FROM companies").fetchone()[0] > 0
    except sqlite3.Error:
        return False
    finally:
        con.close()


def load(force: bool = False, src: Path | None = None) -> dict:
    """Install the demo snapshot. Existing data is kept unless force; with force the old DB is backed up first."""
    s = get_settings()
    src = src or demo_dir()
    gz = src / "localledger.db.gz"
    if not gz.exists():
        raise FileNotFoundError(f"{gz} not found (run `ll demo-export` on a machine with data)")
    target = db_path()
    if has_data() and not force:
        raise FileExistsError(f"{target} already has data; pass --force to replace it (a .bak copy is kept)")

    from localledger.db import reset_engine

    reset_engine()
    if target.exists():
        backup = target.with_name(target.name + f".bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.move(target, backup)
    for suffix in ("-wal", "-shm"):
        side = target.with_name(target.name + suffix)
        if side.exists():
            side.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gz, "rb") as g, target.open("wb") as f:
        shutil.copyfileobj(g, f)

    copied = {"outputs": 0, "raw": 0, "llm_cache": 0}
    for key, sub, dst_root in (
        ("outputs", "outputs", s.out_dir),
        ("raw", "rawdocs", s.raw_dir),
        ("llm_cache", "llm_cache", s.cache_dir / "llm"),
    ):
        base = src / sub
        for p in base.rglob("*") if base.exists() else []:
            if p.is_file() and _copy(p, dst_root / p.relative_to(base), overwrite=False):
                copied[key] += 1

    from localledger.db import init_db

    init_db()  # additive migrations, in case the snapshot predates a column
    manifest = json.loads((src / "manifest.json").read_text()) if (src / "manifest.json").exists() else {}
    return {"database": str(target), "copied": copied, "manifest": manifest}
