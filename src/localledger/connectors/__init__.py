"""Connector registry. Order matters: EDGAR first (it maps tickers to CIKs for everyone else)."""

from __future__ import annotations

import importlib

REGISTRY: dict[str, str] = {
    "edgar": "localledger.connectors.edgar:EdgarConnector",
    "census_bps": "localledger.connectors.census_bps:CensusBpsConnector",
}


def load_connector(source_id: str):
    module_name, cls_name = REGISTRY[source_id].split(":")
    return getattr(importlib.import_module(module_name), cls_name)
