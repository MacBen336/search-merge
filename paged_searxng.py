"""SearXNG-Pagination als eigener Provider — Name ``searxng-paged``.

Warum das hier liegt und nicht im gebuendelten Provider:
Der gebuendelte ``plugins/web/searxng/provider.py`` setzt ``pageno`` hart auf 1 und
holt genau eine Seite; jedes ``limit`` oberhalb der Seitengroesse wird still
unterschlagen (gemessen 11-10-2026: bei ``limit=100`` lieferte er 31-99 Zeilen,
mit Seiten-Schleife 100). Ein Patch an dieser Datei waere der richtige Ort —
aber ``~/.hermes/hermes-agent`` ist ein Git-Checkout, und ``hermes update``
stasht lokale Aenderungen vor dem Tree-Move und parkt sie danach
(``hermes_cli/update_cmd_stash.py``). Der Fix waere also nach jedem Update
**still wirkungslos**. Er liegt deshalb im eigenen, update-festen Plugin.

Warum ein EIGENER Name und nicht ein Ueberschreiben von ``searxng``:
Ein gleichnamiger Provider wird von der Registry **ersetzt**, nicht verworfen —
``builtin_names`` der Web-Registry ist leer (gemessen 11-10-2026), das
Schutz-Gate greift also nicht. Ein Ueberschreiben haengt damit an der
Registrierungsreihenfolge (unser Plugin nach dem gebuendelten) und waere bei
einer Aenderung dort **still** wirkungslos. Der eigene Name haengt an nichts.

Die Zuordnung der Konfigurationsquelle ``searxng`` auf diesen Provider macht der
Merge (``provider.SOURCE_ALIASES`` / ``limits.SOURCE_ALIASES``) — sichtbar in
``meta.sources.<name>.resolved_as``, nicht im Verborgenen. Ist dieser Provider
nicht registriert, faellt der Merge auf den gebuendelten zurueck.

Kein Extract: wie der gebuendelte Provider nur Suche.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from plugins.web._common import (
    BaseWebSearchProvider,
    http_get_json,
    provider_env,
    search_fail,
    search_ok,
    setup_schema,
    titled_rows,
)

logger = logging.getLogger(__name__)

#: Registry-Name. Bewusst NICHT ``searxng`` (siehe Modul-Docstring).
PROVIDER_NAME = "searxng-paged"
#: Wie beim gebuendelten Provider: die Instanz-URL ist die Verfuegbarkeitsbedingung.
KEY_ENV_NAME = "SEARXNG_URL"

#: Hartes Seitenlimit. SearXNG hat keinen serverseitigen Cap; ohne Deckel wuerde
#: ein ``limit`` von 100 auf einer klein paginierten Instanz Dutzende Abrufe
#: ausloesen.
MAX_PAGES = 5
#: Zeitbudget fuer den ganzen Seitenlauf. Seite 1 ist die wertvolle; tiefere
#: Seiten werden schlechter und duerfen das Budget des Fan-outs nicht fressen.
PAGE_BUDGET_S = 10.0


def _score_of(row: dict[str, Any]) -> float:
    """SearXNGs ``score`` je Treffer, tolerant gegen fehlende/kaputte Werte."""
    try:
        return float(row.get("score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


class PagedSearxngWebSearchProvider(BaseWebSearchProvider):
    """Wie der gebuendelte SearXNG-Provider, aber mit Seiten-Schleife bis ``limit``."""

    NAME = PROVIDER_NAME
    DISPLAY_NAME = "SearXNG (paginiert)"
    KEY_ENV = KEY_ENV_NAME

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        base_url = provider_env(KEY_ENV_NAME).rstrip("/")
        if not base_url:
            return search_fail("SEARXNG_URL is not set")
        try:
            wanted = int(limit)
        except (TypeError, ValueError):
            wanted = 5
        wanted = max(1, wanted)

        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        started = time.monotonic()
        page = 1
        raw_total = 0
        while len(collected) < wanted and page <= MAX_PAGES:
            data, failure = http_get_json(
                "SearXNG", f"{base_url}/search", params={"q": query, "format": "json", "pageno": page},
                headers={"Accept": "application/json"}, timeout=15, logger=logger,
                reach_target=f"SearXNG at {base_url}",
            )
            if failure is not None:
                if page == 1:
                    return failure
                logger.warning(
                    "SearXNG: Seite %d fehlgeschlagen nach %d Treffer(n) — der Rest wird geliefert",
                    page, len(collected),
                )
                break
            raw_results = data.get("results", []) or []
            raw_total += len(raw_results)
            # Score absteigend sortieren; die Positionen vergebe ich erst nach dem
            # Dedupe ueber die Seiten, damit sie 1..n ueber die ganze Liste sind.
            added = 0
            for row in sorted(raw_results, key=_score_of, reverse=True):
                url = str(row.get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                collected.append(row)
                added += 1
                if len(collected) >= wanted:
                    break
            # Leere Seite oder nichts Neues: die Instanz hat tiefer nichts mehr.
            if not raw_results or added == 0:
                break
            if time.monotonic() - started > PAGE_BUDGET_S:
                logger.info(
                    "SearXNG: Seitenbudget von %.1fs nach %d Seite(n) erreicht (limit %d)",
                    PAGE_BUDGET_S, page, wanted,
                )
                break
            page += 1

        web_results = titled_rows(collected[:wanted], "content")
        logger.info(
            "SearXNG (paginiert) '%s': %d Treffer (aus %d roh ueber %d Seite(n), limit %d)",
            query, len(web_results), raw_total, page, wanted,
        )
        return search_ok(web_results)

    def get_setup_schema(self) -> dict[str, Any]:
        return setup_schema(
            "SearXNG (paginiert)", "free · self-hosted",
            "Wie SearXNG, holt aber weitere Seiten nach, bis die gewuenschte Trefferzahl erreicht ist. "
            "Vom Plugin search-merge bereitgestellt; die Quelle heisst in der Konfiguration weiter 'searxng'.",
            KEY_ENV_NAME, "SearXNG instance URL (e.g. http://localhost:8080)", "https://searx.space/",
        )
