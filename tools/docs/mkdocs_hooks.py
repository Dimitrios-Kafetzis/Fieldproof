"""MkDocs hooks for the Fieldproof documentation site (task P0-T06).

Registered in ``mkdocs.yml`` under ``hooks``. Before every build the ADR index
(``docs/adr/README.md``) is regenerated with :mod:`adr_index`, and the ADR pages are inserted into
the ``Decisions`` section of the navigation, so a new ADR needs no edit to ``mkdocs.yml``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import adr_index

log = logging.getLogger("mkdocs.hooks.fieldproof")

DECISIONS_SECTION = "Decisions"


def _adr_dir(config: Any) -> Path:
    return Path(config["docs_dir"]) / "adr"


def on_config(config: Any) -> Any:
    """Regenerate the ADR index and expand the Decisions navigation section."""
    adr_dir = _adr_dir(config)
    adrs = adr_index.load_adrs(adr_dir)
    index = adr_dir / adr_index.INDEX_NAME
    content = adr_index.render(adrs)
    if not index.exists() or index.read_text(encoding="utf-8") != content:
        index.write_text(content, encoding="utf-8")
        log.info("Regenerated %s", index)

    entries: list[Any] = [f"adr/{adr_index.INDEX_NAME}"]
    entries.extend({f"ADR-{adr.number} {adr.title}": f"adr/{adr.file_name}"} for adr in adrs)
    entries.append({"ADR template": f"adr/{adr_index.TEMPLATE_NUMBER}-adr-template.md"})

    nav = config["nav"] or []
    for item in nav:
        if isinstance(item, dict) and DECISIONS_SECTION in item:
            item[DECISIONS_SECTION] = entries
            break
    else:
        nav.append({DECISIONS_SECTION: entries})
    config["nav"] = nav
    log.info("Navigation: %d ADRs under %s", len(adrs), DECISIONS_SECTION)
    return config
