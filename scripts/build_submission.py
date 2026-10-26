"""Build report.html and slides.html at the repo root from docs/submission/*.src.html.

Every <img src="img/..."> is inlined as a base64 data URI and every <!--chart:name--> becomes an inline SVG
(scripts/submission_charts.py), so both files open by double-clicking with no
other files beside them: python scripts/build_submission.py
"""

import base64
import mimetypes
import re
from pathlib import Path

from submission_charts import CHARTS

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "submission"


def inline(html: str) -> str:
    def data_uri(match: re.Match) -> str:
        path = SRC / match.group(1)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f'src="data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"'

    html = re.sub(r"<!--chart:(\w+)-->", lambda m: CHARTS[m.group(1)](), html)
    return re.sub(r'src="(img/[^"]+)"', data_uri, html)


def main() -> None:
    for name in ("report", "slides"):
        out = ROOT / f"{name}.html"
        out.write_text(inline((SRC / f"{name}.src.html").read_text()))
        print(f"{out.relative_to(ROOT)}  {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
