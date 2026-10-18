"""search-merge — Agent-Haelfte des Plugins (V2 des Such-Fan-outs).

Registriert DREI Web-Such-Provider und liefert die Konfigurations-Oberflaeche
plus Backend-Routen:

  provider.py       — das Merge-Backend (ID `search-merge`, plus Alias `search-fanout`)
  paged_searxng.py  — SearXNG mit Seiten-Schleife (ID `searxng-paged`)
  dashboard/        — Web-Dashboard (manifest.json, dist/index.js, plugin_api.py)
  desktop/          — native Desktop-App (plugin.js)

Der Plugin-Loader verlangt dieses Modul im Ordner — ohne __init__.py bricht er
mit "No __init__.py" ab (belegt im Log, 03-10-2026).

WARUM DREI NAMEN, alle aus einem Grund:

1. `search-merge` ist der neue Name des Merge-Backends. Bei der Umstellung trugen
   der Default und alle fuenf benannten Profile noch
   `web.search_backend: search-fanout`.
2. `search-fanout` bleibt als **Alias** registriert (eigene Unterklasse, gleicher
   Code): ohne ihn waere der Kern in `_resolve()` auf den Legacy-Walk
   zurueckgefallen (firecrawl -> parallel -> tavily -> ...) und jedes Profil
   haette still ueber einen anderen Anbieter gesucht, ohne Fehlermeldung. Der
   Alias darf erst weg, wenn kein Profil mehr die alte ID setzt.

       grep -rl 'search_backend: search-fanout' ~/.hermes/config.yaml ~/.hermes/profiles/*/config.yaml

3. `searxng-paged` ist die paginierende SearXNG-Variante. Der gebuendelte Provider
   setzt `pageno` hart auf 1 und liefert genau eine Seite; ein Patch dort waere
   nach jedem `hermes update` in einem Autostash geparkt und still wirkungslos.
   Der Merge loest die Konfigurationsquelle `searxng` auf diesen Namen auf
   (`limits.SOURCE_ALIASES`) und weist es je Quelle als `resolved_as` aus.
   Eigener Name statt Ueberschreiben von `searxng`: ein gleichnamiger Provider
   wird von der Registry ERSETZT (gemessen: `builtin_names` der Web-Registry ist
   leer, das Schutz-Gate greift also nicht) — das haenge dann an der
   Registrierungsreihenfolge und waere bei einer Aenderung dort still kaputt.

Keine Tools, keine Hooks, keine Capabilities: Der Merge liest nur die eigene
Konfigurationsdatei und ruft die bereits registrierten Such-Provider derselben
Installation auf. Er schreibt nichts nach aussen — kein Vault, keine Datei
ausser der eigenen Konfiguration (die schreibt allein die Dashboard-API).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Merge-Backend, Legacy-Alias und den paginierenden SearXNG-Provider anmelden."""
    from .paged_searxng import PROVIDER_NAME as PAGED_NAME
    from .paged_searxng import PagedSearxngWebSearchProvider
    from .provider import (
        ID,
        LEGACY_ID,
        SearchFanoutLegacyWebSearchProvider,
        SearchMergeWebSearchProvider,
    )

    primary = SearchMergeWebSearchProvider()
    alias = SearchFanoutLegacyWebSearchProvider()
    paged = PagedSearxngWebSearchProvider()

    # Kein `assert`: unter `python -O` verschwindet der Test, und dann faellt
    # eine vertauschte ID erst zur Laufzeit als stille Fehlleitung auf.
    expected = ((primary, ID), (alias, LEGACY_ID), (paged, PAGED_NAME))
    wrong = [(getattr(obj, "name", ""), want) for obj, want in expected if getattr(obj, "name", "") != want]
    if wrong:
        logger.error(
            "search-merge: Registry-Namen stimmen nicht (%s) — Suche kann ins Leere laufen",
            ", ".join(f"{got!r} statt {want!r}" for got, want in wrong),
        )

    ctx.register_web_search_provider(primary)
    ctx.register_web_search_provider(alias)
    ctx.register_web_search_provider(paged)
    logger.info(
        "search-merge: registriert als %r, %r (Alias) und %r (SearXNG-Pagination)", ID, LEGACY_ID, PAGED_NAME
    )
