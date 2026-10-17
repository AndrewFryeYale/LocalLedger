import shutil

from sqlmodel import select

from localledger.connectors.census_bps import CensusBpsConnector, file_url, parse_bps_rows, prior_year
from localledger.models import Signal
from tests.conftest import FIXTURES

C = FIXTURES / "census_bps"
GEOS = [
    {
        "id": "arapahoe-co",
        "level": "county",
        "state_fips": "08",
        "county_fips": "005",
        "name": "Arapahoe County, CO",
        "tickers": ["PCYO"],
    },
    {
        "id": "maui-hi",
        "level": "county",
        "state_fips": "15",
        "county_fips": "009",
        "name": "Maui County, HI",
        "tickers": ["MLP"],
    },
]


def test_parse_county_rows_totals_all_unit_types():
    rows = {r["key"]: r for r in parse_bps_rows((C / "co2508c.txt").read_text(), "county")}
    arap = rows[("08", "005")]
    assert arap["units_1"] == 100 and arap["units_5plus"] == 558
    assert arap["units_total"] == 658
    assert arap["name"] == "Arapahoe County"


def test_file_urls():
    assert file_url("county", "2608", "c") == "https://www2.census.gov/econ/bps/County/co2608c.txt"
    assert file_url("place", "2608", "y", "West").endswith("/Place/West%20Region/we2608y.txt")
    assert prior_year("2608") == "2508"


def _routes():
    return {
        "https://www2.census.gov/econ/bps/County/co2608c.txt": C / "co2608c.txt",
        "https://www2.census.gov/econ/bps/County/co2508c.txt": C / "co2508c.txt",
    }


def test_census_signals_from_fixtures(seeded, fake_http):
    conn = CensusBpsConnector(seeded, http=fake_http(_routes()), geo_map=GEOS, latest="2608", origin="fixture")
    result = conn.run()
    assert result.signals == 2, result.errors
    sig = seeded.exec(select(Signal).where(Signal.ticker == "PCYO")).one()
    assert sig.metric == "permits_units_month_yoy"
    assert sig.value == 121 and sig.prior_value == 658
    assert "Arapahoe County, CO" in sig.summary and "-81.6% y/y" in sig.summary
    assert sig.citation_url == "https://www2.census.gov/econ/bps/County/co2608c.txt"


def test_robots_block_falls_back_to_manual_inbox(seeded, tmp_path):
    from localledger.http import RobotsDisallowed

    class BlockedHttp:
        def get_ok(self, url, **kw):
            raise RobotsDisallowed(url)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    conn = CensusBpsConnector(seeded, http=BlockedHttp(), geo_map=GEOS, inbox=inbox)
    result = conn.run()
    assert result.status == "unavailable" and "robots" in result.errors[0]
    for name in ("co2608c.txt", "co2508c.txt"):
        shutil.copy(C / name, inbox / name)
    conn = CensusBpsConnector(seeded, http=BlockedHttp(), geo_map=GEOS, inbox=inbox)
    result = conn.run()
    assert result.signals == 2, result.errors
