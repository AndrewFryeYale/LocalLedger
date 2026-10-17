"""Census Building Permits Survey (BPS): monthly new privately-owned housing units authorized.

Access (verified Oct 2026, see SOURCES.md): flat CSV text files on www2.census.gov, no key.
- County:  https://www2.census.gov/econ/bps/County/co{yymm}c.txt   (c = month, y = year-to-date)
- Place:   https://www2.census.gov/econ/bps/Place/{Region}%20Region/{ne|mw|so|we}{yymm}c.txt
Geographies are mapped to tickers in config/geo_map.yaml.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from localledger.connectors.base import Connector, ConnectorUnavailable
from localledger.http import RobotsDisallowed, read_document_bytes
from localledger.models import Document
from localledger.signals.scoring import move_significance, pct_change, scale_by_volume
from localledger.signals.store import SignalDraft

BASE = "https://www2.census.gov/econ/bps"
REGION_PREFIX = {"Northeast": "ne", "Midwest": "mw", "South": "so", "West": "we"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def file_url(level: str, yymm: str, kind: str, region: str | None = None) -> str:
    if level == "county":
        return f"{BASE}/County/co{yymm}{kind}.txt"
    prefix = REGION_PREFIX[region]
    return f"{BASE}/Place/{region}%20Region/{prefix}{yymm}{kind}.txt"


def prior_year(yymm: str) -> str:
    return f"{int(yymm[:2]) - 1:02d}{yymm[2:]}"


def month_label(yymm: str, kind: str) -> str:
    year, month = 2000 + int(yymm[:2]), int(yymm[2:])
    return f"{MONTHS[month - 1]} {year}" if kind == "c" else f"Jan-{MONTHS[month - 1]} {year}"


def parse_bps_rows(text: str, level: str) -> list[dict]:
    """Rows of a BPS county/place file as dicts with totals. Header is two lines then a blank line."""
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip().isdigit():
            continue
        try:
            if level == "county":
                key = (row[1].strip(), row[2].strip())
                name = row[5].strip()
                nums = [int(float(x or 0)) for x in row[6:18]]
            else:
                key = (row[1].strip(), row[5].strip())
                name = row[16].strip()
                nums = [int(float(x or 0)) for x in row[17:29]]
        except (IndexError, ValueError):
            continue
        units = nums[1] + nums[4] + nums[7] + nums[10]
        out.append(
            {
                "survey": row[0].strip(),
                "key": key,
                "name": name,
                "units_1": nums[1],
                "units_2_4": nums[4] + nums[7],
                "units_5plus": nums[10],
                "units_total": units,
                "bldgs_total": nums[0] + nums[3] + nums[6] + nums[9],
                "value_total": nums[2] + nums[5] + nums[8] + nums[11],
                "row_text": ",".join(c.strip() for c in row),
            }
        )
    return out


class CensusBpsConnector(Connector):
    source_id = "census_bps"
    name = "Census Building Permits Survey"
    type = "permits"
    access_method = (
        "Flat CSV files on www2.census.gov/econ/bps (County/co{yymm}{c|y}.txt; Place/{Region} Region/"
        "{prefix}{yymm}{c|y}.txt). Automated fetch only if robots.txt allows it (or a written permission is "
        "recorded in config/robots_permissions.yaml); otherwise files are read from data/inbox/census_bps/."
    )
    cadence = "monthly (~3rd week, for the prior month)"
    terms_note = (
        "Public US government statistical data. www2.census.gov robots.txt groups `User-agent: *` with a "
        "`Disallow: /` rule (RFC 9309 reading), so automated fetch is off pending permission. "
        "Values are estimates with imputation."
    )

    def __init__(
        self,
        *args: Any,
        geo_map: list[dict] | None = None,
        latest: str | None = None,
        inbox: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.geos = geo_map if geo_map is not None else self._load_geo_map()
        self.latest = latest
        self.mode = "http"
        self.inbox = inbox or self.settings.data_dir / "inbox" / "census_bps"

    def _load_geo_map(self) -> list[dict]:
        path = self.settings.config_dir / "geo_map.yaml"
        return (yaml.safe_load(path.read_text()) or {}).get("geographies", [])

    def _geos_in_scope(self) -> list[dict]:
        universe = self.universe()
        return [g for g in self.geos if any(t in universe for t in g["tickers"])]

    def discover(self) -> list[tuple[str, str, str, str | None]]:
        geos = self._geos_in_scope()
        if not geos:
            raise ConnectorUnavailable("geo_map.yaml has no geographies for the selected tickers")
        latest = self.latest
        if latest is None:
            try:
                listing = self.http.get_ok(f"{BASE}/County/").text
                months = sorted(set(re.findall(r"co(\d{4})c\.txt", listing)))
            except RobotsDisallowed:
                self.mode = "inbox"
                months = sorted(set(re.findall(r"co(\d{4})c\.txt", " ".join(p.name for p in self._inbox_files()))))
                if not months:
                    raise ConnectorUnavailable(
                        "www2.census.gov robots.txt disallows generic agents (RFC 9309 reading) and the manual "
                        f"inbox {self.inbox} has no county files; see data/inbox/census_bps/README.md"
                    ) from None
            if not months:
                raise ConnectorUnavailable("could not find monthly county files in the BPS directory listing")
            latest = months[-1]
        self.latest = latest
        targets = []
        levels = {("county", None)} | {("place", g["region"]) for g in geos if g["level"] == "place"}
        for level, region in sorted(levels, key=lambda x: (x[0], x[1] or "")):
            for kind in ("c", "y"):
                for yymm in (latest, prior_year(latest)):
                    targets.append((level, yymm, kind, region))
        return targets

    def _inbox_files(self) -> list[Path]:
        return sorted(self.inbox.glob("*.txt")) if self.inbox.exists() else []

    def fetch(self, target: tuple[str, str, str, str | None]) -> list[Document]:
        level, yymm, kind, region = target
        url = file_url(level, yymm, kind, region)
        meta = {"level": level, "yymm": yymm, "kind": kind, "region": region}
        title = f"Census BPS {level} {month_label(yymm, kind)}" + (f" ({region})" if region else "")
        if self.mode == "inbox":
            local = self.inbox / url.rsplit("/", 1)[-1]
            if not local.exists():
                return []
            content = local.read_bytes()
            meta["acquired"] = "manual download (robots.txt blocks automated access)"
        else:
            try:
                content = self.http.get_ok(url).content
            except RobotsDisallowed:
                self.mode = "inbox"
                return self.fetch(target)
        return [
            self.save(
                url=url,
                content=content,
                content_type="text/plain",
                title=title,
                meta=meta,
                text=content.decode("latin-1"),
            )
        ]

    def parse(self, doc: Document) -> list[dict]:
        import json

        meta = json.loads(doc.meta_json or "{}")
        text = read_document_bytes(doc, self.settings).decode("latin-1")
        header = "\n".join(text.splitlines()[:2])
        rows = {r["key"]: r for r in parse_bps_rows(text, meta["level"])}
        out = []
        for geo in self._geos_in_scope():
            if geo["level"] != meta["level"] or (geo["level"] == "place" and geo.get("region") != meta["region"]):
                continue
            key = (
                (geo["state_fips"], geo["county_fips"])
                if geo["level"] == "county"
                else (geo["state_fips"], geo["place_fips"])
            )
            row = rows.get(key)
            if row is None:
                continue
            out.append(
                {
                    **row,
                    "geo": geo,
                    "yymm": meta["yymm"],
                    "kind": meta["kind"],
                    "header": header,
                    "document_id": doc.id,
                    "url": doc.url,
                }
            )
        return out

    def to_signals(self, records: list[dict]) -> list[SignalDraft]:
        index = {(r["geo"]["id"], r["yymm"], r["kind"]): r for r in records}
        drafts = []
        for (geo_id, yymm, kind), cur in index.items():
            if yymm != self.latest:
                continue
            prior = index.get((geo_id, prior_year(yymm), kind))
            if prior is None:
                continue
            geo = cur["geo"]
            pct = pct_change(cur["units_total"], prior["units_total"])
            volume = max(cur["units_total"], prior["units_total"])
            if kind == "c":
                metric = "permits_units_month_yoy"
                sig = scale_by_volume(move_significance(pct, floor=0.15, full=0.8) * 0.85, volume, 40)
            else:
                metric = "permits_units_ytd_yoy"
                sig = scale_by_volume(move_significance(pct, floor=0.08, full=0.5), volume, 150)
            pct_txt = f"{pct:+.1%} y/y" if pct is not None else "no prior-year base"
            period_now, period_then = month_label(yymm, kind), month_label(prior_year(yymm), kind)
            summary = (
                f"{geo['name']}: {cur['units_total']:,} housing units permitted in {period_now} vs "
                f"{prior['units_total']:,} in {period_then} ({pct_txt}); single-family "
                f"{cur['units_1']:,} vs {prior['units_1']:,}."
            )
            excerpt = (
                f"{cur['header']}\n{cur['row_text']}\n\n[prior-year file {prior['url']}]\n"
                f"{prior['header']}\n{prior['row_text']}"
            )
            for ticker in geo["tickers"]:
                if self.tickers and ticker not in self.tickers:
                    continue
                drafts.append(
                    SignalDraft(
                        ticker=ticker,
                        metric=metric,
                        as_of=_month_end(yymm),
                        period=f"{geo_id}:{yymm}",
                        value=float(cur["units_total"]),
                        prior_value=float(prior["units_total"]),
                        unit="housing units",
                        significance=round(sig, 4),
                        citation_url=cur["url"],
                        document_id=cur["document_id"],
                        summary=summary,
                        detail={
                            "geo": geo_id,
                            "geo_name": geo["name"],
                            "why": geo.get("why"),
                            "pct_change": pct,
                            "units_1": cur["units_1"],
                            "prior_units_1": prior["units_1"],
                            "units_5plus": cur["units_5plus"],
                            "prior_units_5plus": prior["units_5plus"],
                            "prior_document_id": prior["document_id"],
                            "prior_url": prior["url"],
                            "excerpt": excerpt,
                        },
                    )
                )
        return drafts


def _month_end(yymm: str) -> date:
    year, month = 2000 + int(yymm[:2]), int(yymm[2:])
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)
