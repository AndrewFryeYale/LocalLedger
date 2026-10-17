# Legistar fixtures

All files are real responses from `https://webapi.legistar.com/v1/...`, fetched 2026-10-17 with the declared
User-Agent. Nothing was edited except as noted (selection of records only; field values are verbatim).

| File | Source | Trimming |
|---|---|---|
| `mauicounty_events.json` | `/v1/mauicounty/events?$filter=EventDate ...` | 2 of the window's event objects kept: 4925 (Housing and Land Use Committee, 2026-10-21) and 4837 (Disaster Recovery, International Affairs, and Planning Committee, 2026-07-29) |
| `mauicounty_eventitems_4837.json` | `/v1/mauicounty/events/4837/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1` | none (includes the item "ACQUISITION OF MAUI LAND & PINEAPPLE COMPANY, INC.'S WATER INFRASTRUCTURE ASSETS ...") |
| `mauicounty_eventitems_4925.json` | `/v1/mauicounty/events/4925/eventitems?...` | none (Lanai zoning bills; no MLP terms) |
| `maricopa_matters.json` | `/v1/maricopa/matters` | 4 real matter records combined from two responses (`$filter=substringof('Global Water', MatterTitle)` and `$filter=MatterAgendaDate ge datetime'2026-08-18'`): 15437 (special event liquor license at Global Water's office, a false positive), 15201 (Global Water presentation), 13805 (GW-Santa Cruz asset purchase contract), 15429 (rezoning, no GWRS terms) |

The `nyc` client's real answer (HTTP 403 "Token is required", empty body) is reproduced inline in the tests.
