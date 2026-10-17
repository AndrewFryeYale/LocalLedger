"""Connector registry. Order matters: EDGAR first (it maps tickers to CIKs for everyone else)."""

from __future__ import annotations

import importlib

REGISTRY: dict[str, str] = {
    "edgar": "localledger.connectors.edgar:EdgarConnector",
    "og_state": "localledger.connectors.og_state:OgStateConnector",
    "gaming_state": "localledger.connectors.gaming_state:GamingStateConnector",
    "census_bps": "localledger.connectors.census_bps:CensusBpsConnector",
    "ffiec": "localledger.connectors.ffiec:FfiecConnector",
    "legistar": "localledger.connectors.legistar:LegistarConnector",
    "pdf_agenda": "localledger.connectors.pdf_agenda:PdfAgendaConnector",
}


def load_connector(source_id: str):
    module_name, cls_name = REGISTRY[source_id].split(":")
    return getattr(importlib.import_module(module_name), cls_name)
