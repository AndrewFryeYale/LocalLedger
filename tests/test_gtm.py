"""GTM engine: 13F leads (synthetic mini data set), outreach drafts (mock LLM), PDFs, publish command."""

import csv
import io
import json
import zipfile
from datetime import date

import httpx
import pytest
from sqlmodel import select
from typer.testing import CliRunner

from localledger.connectors.edgar import EdgarConnector
from localledger.gtm.leads13f import build_leads, norm_name
from localledger.gtm.outreach import MAX_WORDS, body_words, build_outreach, clean_body
from localledger.llm.client import LLM
from localledger.models import Lead, Note
from tests.test_edgar import routes

H = {
    "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
    "A-1\t14-AUG-2026\t13F-HR\t0000000001\t30-JUN-2026\n"
    "A-2\t14-AUG-2026\t13F-HR\t0000000002\t30-JUN-2026\n"
    "A-3\t14-AUG-2026\t13F-HR\t0000000003\t30-JUN-2026\n",
    "COVERPAGE.tsv": "ACCESSION_NUMBER\tISAMENDMENT\tAMENDMENTTYPE\tFILINGMANAGER_NAME\tFILINGMANAGER_CITY\t"
    "FILINGMANAGER_STATEORCOUNTRY\n"
    "A-1\tN\t\tSmall Cap Value Partners LLC\tDENVER\tCO\n"
    "A-2\tN\t\tOne Name Capital\tBOSTON\tMA\n"
    "A-3\tN\t\tVanguard Group Inc\tMALVERN\tPA\n",
    "SUMMARYPAGE.tsv": "ACCESSION_NUMBER\tTABLEVALUETOTAL\nA-1\t50000000\nA-2\t9000000\nA-3\t9000000000000\n",
    "INFOTABLE.tsv": "ACCESSION_NUMBER\tNAMEOFISSUER\tTITLEOFCLASS\tCUSIP\tVALUE\tPUTCALL\n"
    "A-1\tPURE CYCLE CORP\tCOM NO PAR\t746228303\t2000000\t\n"
    "A-1\tTEJON RANCH CO\tCOM\t879080109\t1500000\t\n"
    "A-1\tAPPLE INC\tCOM\t037833100\t9000000\t\n"
    "A-2\tPURE CYCLE CORP\tCOM NO PAR\t746228303\t800000\t\n"
    "A-3\tPURE CYCLE CORP\tCOM NO PAR\t746228303\t90000000\t\n"
    "A-3\tTEJON RANCH CO\tCOM\t879080109\t80000000\t\n",
}


class FigiHttp:
    """Stub: OpenFIGI answers by CUSIP; nothing else is reachable."""

    TICKERS = {"746228303": "PCYO", "879080109": "TRC"}

    def request(self, method, url, json_body=None, **kw):
        assert url.startswith("https://api.openfigi.com/")
        out = [{"data": [{"ticker": self.TICKERS.get(j["idValue"], "ZZZ")}]} for j in json_body]
        return httpx.Response(200, content=json.dumps(out).encode(), request=httpx.Request(method, url))


@pytest.fixture()
def mini_13f(tmp_path):
    path = tmp_path / "mini_form13f.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in H.items():
            zf.writestr(name, text)
    return path


def test_norm_name():
    assert norm_name("Tejon Ranch Co.") == norm_name("TEJON RANCH CO") == "tejon ranch"


def test_leads_from_13f(seeded, mini_13f, monkeypatch, env):
    from localledger.config import get_settings

    (env / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(type(get_settings()), "data_dir", property(lambda self: env / "data"))
    result = build_leads(seeded, http=FigiHttp(), zip_path=mini_13f)
    assert result.verified == 2
    rows = list(csv.DictReader(result.path.open()))
    assert [r["fund_name"] for r in rows] == ["Small Cap Value Partners LLC", "Vanguard Group Inc"]  # one-name fund out
    top = rows[0]
    assert top["overlap_count"] == "2" and top["meaningful_tickers"] == "PCYO TRC" and top["is_mega"] == "False"
    assert rows[1]["is_mega"] == "True"
    assert seeded.exec(select(Lead)).first() is not None


def test_outreach_drafts_are_short_specific_and_unsent(seeded, fake_http, env):
    EdgarConnector(seeded, http=fake_http(routes()), today=date(2026, 10, 17), origin="fixture").run(tickers=["PCYO"])
    seeded.add(
        Lead(
            cik="1",
            fund_name="Small Cap Value Partners LLC",
            city="Denver",
            state="CO",
            rank=1,
            report_period="30-JUN-2026",
            overlap_count=2,
            overlap_tickers="PCYO TRC",
        )
    )
    seeded.commit()
    result = build_outreach(seeded, LLM("test"), top=50)
    assert result.drafts == 1
    text = next(result.out_dir.glob("001-*.md")).read_text()
    assert "DRAFT. Not sent." in text and "PCYO" in text and "https://www.sec.gov/" in text
    body = text.split("\n\n", 1)[1].split("\n\nAndrew")[0]
    assert body_words(body) <= MAX_WORDS


def test_clean_body_strips_model_signoff():
    body = "Hi,\n\nText here. Reply 'no' and I won't follow up.\n\nBest,\nAndrew\nFounder"
    assert clean_body(body).endswith("won't follow up.")


def test_pdfs_and_publish_block(seeded, fake_http, env, monkeypatch):
    from localledger import compliance
    from localledger.cli import app
    from localledger.gtm.pdfs import build_pdfs

    paths = build_pdfs(seeded)
    assert paths and paths[0].read_bytes()[:4] == b"%PDF"
    note = Note(kind="signal", ticker="PCYO", title="t", body_md="Revenue rose. [1]", factcheck_passed=True)
    seeded.add(note)
    seeded.commit()
    monkeypatch.setattr(
        compliance,
        "load_holdings",
        lambda path=None: compliance.Holdings(policy="p", blackout_days=3, tickers={"PCYO"}),
    )
    runner = CliRunner()
    blocked = runner.invoke(app, ["publish", "--note-id", str(note.id)])
    assert blocked.exit_code == 2 and "BLOCKED" in blocked.output
    ok = runner.invoke(app, ["publish", "--note-id", str(note.id), "--disclose"])
    assert ok.exit_code == 0 and "Trading blackout" in ok.output


def test_zip_reader_handles_tsv(mini_13f):
    with zipfile.ZipFile(mini_13f) as zf:
        rows = list(csv.DictReader(io.TextIOWrapper(zf.open("INFOTABLE.tsv")), delimiter="\t"))
    assert len(rows) == 6
