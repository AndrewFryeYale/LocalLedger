"""Prompt files live in /prompts/*.md. Format:

---
tier: fast            # fast | heavy
max_tokens: 2000
---
## system
...
## user
... {{ jinja_variables }} ...
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import yaml
from jinja2 import Environment, StrictUndefined

from localledger.config import get_settings

_env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


@dataclass(frozen=True)
class Prompt:
    name: str
    tier: str
    max_tokens: int
    system: str
    user: str
    raw: str

    def render(self, variables: dict) -> tuple[str, str]:
        return _env.from_string(self.system).render(**variables), _env.from_string(self.user).render(**variables)


@lru_cache(maxsize=128)
def load_prompt(name: str) -> Prompt:
    path = get_settings().prompts_dir / f"{name}.md"
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        meta = yaml.safe_load(front) or {}
    sections: dict[str, list[str]] = {"system": [], "user": []}
    current = None
    for line in body.splitlines():
        header = line.strip().lower()
        if header in ("## system", "## user"):
            current = header[3:]
            continue
        if current:
            sections[current].append(line)
    if not sections["user"]:
        raise ValueError(f"prompt {name} has no '## user' section")
    return Prompt(
        name=name,
        tier=str(meta.get("tier", "fast")),
        max_tokens=int(meta.get("max_tokens", 2000)),
        system="\n".join(sections["system"]).strip(),
        user="\n".join(sections["user"]).strip(),
        raw=raw,
    )
