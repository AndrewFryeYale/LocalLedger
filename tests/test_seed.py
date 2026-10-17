from sqlmodel import func, select

from localledger.models import Archetype, Company
from localledger.seed import coverage_points, size_band


def test_seed_loads_full_universe(seeded):
    assert seeded.exec(select(func.count()).select_from(Company)).one() == 237
    assert seeded.exec(select(func.count()).select_from(Archetype)).one() == 19
    trc = seeded.get(Company, "TRC")
    assert trc.archetype == "Land / entitlement developer"
    assert trc.priority == 9  # 3 + 3 + tradable 1 + coverage "0 (checked)" 2
    pcyo = seeded.get(Company, "PCYO")
    assert pcyo.size_band == "$250-500M"
    banks = seeded.exec(select(func.count()).select_from(Company).where(Company.archetype == "Community bank")).one()
    assert banks == 110


def test_priority_formula_pieces():
    assert coverage_points("0 (checked)") == 2
    assert coverage_points("Unverified") == 1
    assert coverage_points("1 (checked)") == 0.5
    assert coverage_points("2+ (checked)") == 0
    assert size_band(10) == "Nano (<$25M)"
    assert size_band(99.9) == "$25-100M"
    assert size_band(250) == "$250-500M"
