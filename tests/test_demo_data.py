"""Bundled demo dataset: export -> load round trip, and existing data is never silently replaced."""

import sqlite3

import pytest

from localledger.demo_data import db_path, export, has_data, load


def test_demo_round_trip_keeps_existing_data_unless_forced(seeded, env):
    from localledger.config import get_settings

    s = get_settings()
    (s.out_dir / "backtest").mkdir(parents=True)
    (s.out_dir / "backtest" / "results.json").write_text("{}")
    m = export(env / "bundle")
    assert m["counts"]["companies"] == 237 and "backtest/results.json" in m["outputs"]
    assert not list((env / "bundle").glob("*.db*-wal"))

    with pytest.raises(FileExistsError):
        load(src=env / "bundle")

    (s.out_dir / "backtest" / "results.json").unlink()
    r = load(force=True, src=env / "bundle")
    assert has_data() and r["copied"]["outputs"] >= 1
    assert (s.out_dir / "backtest" / "results.json").exists()
    assert list(db_path().parent.glob(db_path().name + ".bak-*"))  # the replaced DB was kept
    con = sqlite3.connect(db_path())
    assert con.execute("SELECT count(*) FROM presence").fetchone()[0] == 0
    con.close()
