"""Merge-Backend von Search-Merge (V2 des Such-Fan-outs).

Fragt die aktivierten Anbieter PARALLEL ab, fuehrt die Treffer zusammen und
gewichtet sie. Wird nur aktiv, wenn ``web.search_backend: search-merge``
gesetzt ist — bis dahin aendert sich am Suchpfad nichts.

Gelesen wird dieselbe Konfiguration, die die Oberflaeche schreibt:
``<HERMES_HOME>/search-merge.json``; fehlt sie, ``search-fanout.json``
(Migrationspfad, siehe ``load_config``).

Was V2 gegenueber V1 aendert — vier Punkte, jeder mit einem Grund:

1. **Trefferzahl JE QUELLE** statt eines globalen ``provider_limit: 100``.
   In V1 holten firecrawl und xAI mit demselben Deckel wie die eigene,
   kostenlose SearXNG-Instanz. Vorgabe jetzt aus der Kostenklasse
   (``limits.default_limit_for``): kostenlose Quellen breit, bezahlte/LLM klein.
   Die Zahl ist eine Kostengrenze, kein Versprechen — der wirksame Wert und die
   gelieferte Menge stehen beide in ``meta.sources``.

2. **Ein langsamer Anbieter haelt den Turn nicht mehr fest.** V1 hatte
   ``wait_for_all: true`` + ``hard_timeout_s: 120`` als Vorgabe ("Qualitaet vor
   Latenz"): die xAI-Suche braucht gemessen 12,5-31 s und wurde bis zu zwei
   Minuten abgewartet. V2 hat zwei Budgets (Index-Quellen / LLM-Nachtrag) und
   verwirft jede Quelle, die ihr Budget reisst, fuer diesen Turn — sichtbar in
   ``meta.ignored_sources``. ``wait_for_all`` bleibt als Schalter erhalten,
   seine Vorgabe ist jetzt False.

3. **Ehrliches meta.** Je Quelle: ok, Dauer, gelieferte Zeilen, wirksames
   Limit, Rolle (index/llm), ignoriert ja/nein. Dazu das wirksame Wartefenster
   und die verwendete Konfigurationsdatei.

4. **Der LLM-Nachtrag ist optional vom Ergebnis trennbar** (``llm_position``):
   Default ``append`` (wie V1, mit ``[LLM-Sicht]`` gekennzeichnet), ``meta_only``
   legt ihn ausschliesslich nach ``meta.llm_results``.

SearXNG-Pagination liegt bewusst NICHT hier: der gebuendelte Provider unter
``plugins/web/searxng/provider.py`` setzte ``pageno`` fest auf 1 und holte genau
eine Seite. Repariert wurde das dort, wo der Fehler sitzt — ein zweiter
HTTP-Pfad im Fan-out wuerde die Vendor-Abstraktion aufbrechen. Der Fan-out
nimmt deshalb weiterhin nur ``len(rows) == limit`` NICHT an.

Gemessene Grundlage (03-10-2026, cache/scratch/fanout_eval.py):
  1 Quelle ~40 %, 2 Quellen ~70 %, 3 Quellen 100 % der gefundenen URLs.
  Deshalb Vorgabe: 3 Index-Quellen; xAI laeuft als Nachtrag.
"""

from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from plugins.web._common import BaseWebSearchProvider, search_ok

# Registry-Namen. ``ID`` ist der eine Name, den `web.search_backend` treffen
# soll; ``LEGACY_ID`` bleibt registriert, solange irgendein Profil ihn setzt —
# ohne ihn faellt der Kern in `_resolve()` auf den Legacy-Walk zurueck und jedes
# Profil suchte still ueber einen anderen Anbieter (siehe __init__.py).
ID = "search-merge"
LEGACY_ID = "search-fanout"

CONFIG_NAME = "search-merge.json"
LEGACY_CONFIG_NAME = "search-fanout.json"

logger = logging.getLogger(__name__)

# Quellen mit LLM-Rolle: sie liefern generierte URLs ohne Index dahinter und
# laufen als gekennzeichneter Nachtrag, nicht in der Rangfusion. Ueber die
# Konfiguration erweiterbar (`llm_sources`); die Liste hier ist nur die Vorgabe.
LLM_ROLE_DEFAULT = ("xai",)

# Rangfusion: ueblicher RRF-Daempfungswert.
RRF_K = 60.0

# Rerank-Faktoren. Bewusst moderat: bei k=60 ist der Rangabstand ~1/61; ein
# Faktor 1,5 entspraeche rund 30 Rangplaetzen und uebersteuerte die Fusion
# (Grok-Review 03-10-2026).
BOOST_FACTOR = 1.12
DEMOTE_FACTOR = 0.88

# Vorgaben nur fuer den Fall, dass die Konfiguration fehlt/unvollstaendig ist.
# Autoritativ fuer Defaults und Validierung bleibt dashboard/plugin_api.py.
DEFAULT_DEADLINE_S = 15.0
# Der LLM-Nachtrag darf etwas laenger brauchen als die Index-Quellen — xAI wurde
# mit 12,5 s (schnell) bis 31,2 s (langsam) gemessen. 20 s nimmt die schnellen
# Laeufe mit und verwirft die langsamen, statt den Turn zwei Minuten zu halten.
DEFAULT_LLM_BUDGET_S = 20.0
# Absoluter Deckel ueber beide Budgets. V1 stand hier auf 120 s.
DEFAULT_HARD_TIMEOUT_S = 30.0
# V1-Vorgabe war True ("Qualitaet vor Latenz") — V2 dreht das um: das Budget
# zaehlt, und wer es reisst, wird fuer den Turn ignoriert.
DEFAULT_WAIT_FOR_ALL = False
DEFAULT_LLM_WAIT = True
DEFAULT_WEIGHT = 1.0
DEFAULT_ENABLED = {"searxng", "ddgs", "xai", "firecrawl"}

# Grenzwerte und Kostenklassen kommen aus limits.py — einzige Quelle, die auch
# dashboard/plugin_api.py liest (siehe dort die Begruendung des Ladewegs).
# Hier nur die Zuweisung; die Zahlen selbst stehen dort.
if True:  # bewusst: Import an Ort und Stelle, damit provider.py allein lauffaehig bleibt
    import hashlib as _hashlib
    import importlib.util as _ilu
    import sys as _sys

    _LIMITS_PATH = Path(__file__).resolve().parent / "limits.py"
    # Modulname MIT Inhalts-Hash, nicht stabil. Ein stabiler Name laesst ein
    # nachgeladenes provider.py die ALTE, im Prozess gecachte limits-Instanz
    # wiederverwenden. Genau das ist passiert (gemessen 11-10-2026, 18:43:35:
    # "Failed to load plugin 'search-merge': module 'search_merge_limits_shared'
    # has no attribute 'legacy_to_v2'") — der Reload zog den neuen provider.py,
    # die Grenzwerte blieben die alten, und das Plugin fiel komplett aus, weil der
    # Import in __init__.py hochkam. Mit dem Hash gilt: gleicher Inhalt ->
    # derselbe Modulname (provider.py und plugin_api.py teilen eine Instanz,
    # Objektidentitaet bleibt pruefbar), geaenderter Inhalt -> neue Instanz statt
    # stiller Altwerte.
    _LIMITS_MOD = "search_merge_limits_shared_" + _hashlib.sha256(_LIMITS_PATH.read_bytes()).hexdigest()[:12]
    _mod = _sys.modules.get(_LIMITS_MOD)
    if _mod is None:
        _spec = _ilu.spec_from_file_location(_LIMITS_MOD, _LIMITS_PATH)
        _mod = _ilu.module_from_spec(_spec)
        _sys.modules[_LIMITS_MOD] = _mod
        _spec.loader.exec_module(_mod)

    KERN_MAX = _mod.KERN_MAX
    FANOUT_MIN = _mod.FANOUT_MIN
    LIMIT_MIN = _mod.LIMIT_MIN
    LIMIT_MAX = _mod.LIMIT_MAX
    DEADLINE_MIN_S = _mod.DEADLINE_MIN_S
    DEADLINE_MAX_S = _mod.DEADLINE_MAX_S
    LLM_BUDGET_MIN_S = _mod.LLM_BUDGET_MIN_S
    LLM_BUDGET_MAX_S = _mod.LLM_BUDGET_MAX_S
    HARD_TIMEOUT_MIN_S = _mod.HARD_TIMEOUT_MIN_S
    HARD_TIMEOUT_MAX_S = _mod.HARD_TIMEOUT_MAX_S
    MAX_ADDED_MIN = _mod.MAX_ADDED_MIN
    MAX_ADDED_MAX = _mod.MAX_ADDED_MAX
    clamp_int = _mod.clamp_int
    clamp_float = _mod.clamp_float
    default_limit_for = _mod.default_limit_for
    cost_class = _mod.cost_class
    legacy_to_v2 = _mod.legacy_to_v2
    SOURCE_ALIASES = _mod.SOURCE_ALIASES

# Wie viele Fan-out-Runden gleichzeitig laufen duerfen: ohne Deckel kann jede
# parallele Suche des Gateways einen eigenen Pool samt Threads aufmachen
# (Grok-Review, "Worker-Starvation").
FANOUT_SLOTS = threading.BoundedSemaphore(4)

# Zaehler fuer Quellen, die nach dem Zeitlimit im Hintergrund weiterlaufen.
_ORPHAN_LOCK = threading.Lock()
_ORPHANED_TOTAL = 0

# Ein SSL-Kontext fuer alle Pruefungen (nicht pro URL neu bauen).
_SSL_CTX = ssl.create_default_context()
_UA = "hermes-search-merge/0.2 (+web-search-provider)"

# Parameter, die nur Tracking sind und nichts ueber die Identitaet einer Seite
# sagen. Alles andere in der Query BLEIBT — sonst kollabieren z. B.
# youtube.com/watch?v=A und ?v=B zu einem Treffer (Grok-Review 03-10-2026).
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "ref", "ref_src", "srsltid", "spm", "yclid", "_hsenc", "_hsmi",
}

# Domain-Regeln des Reranks: Boulevard/Aggregatoren abwerten, Primaer- und
# Fachquellen aufwerten. Bewusst klein und nachvollziehbar gehalten.
DEMOTE_DOMAINS = (
    "youtube.com", "facebook.com", "instagram.com", "tiktok.com", "pinterest.com",
    "medium.com", "reddit.com", "quora.com", "msn.com", "bild.de", "express.de",
)
BOOST_DOMAINS = (
    "arxiv.org", "doi.org", "nature.com", "science.org", "pubmed.ncbi.nlm.nih.gov",
    "europa.eu", "bundesregierung.de", "destatis.de", "bsi.bund.de",
    "github.com", "nvd.nist.gov", "cve.org",
)


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def config_paths() -> list[Path]:
    """Suchreihenfolge der Konfiguration: neue Datei zuerst, alte als Migration."""
    home = _hermes_home()
    return [home / CONFIG_NAME, home / LEGACY_CONFIG_NAME]


def load_config() -> tuple[dict[str, Any], str]:
    """Konfiguration lesen -> ``(config, verwendeter Pfad oder "")``.

    Migrationspfad: eine Installation, die noch ``search-fanout.json`` hat,
    sucht unveraendert weiter — die Datei wird gelesen, nicht umgeschrieben und
    nicht geloescht. Geschrieben wird ab V2 ausschliesslich ``search-merge.json``
    (plugin_api.py), damit ein Zurueckrollen auf V1 die alte Datei intakt findet.
    Fehlende/unlesbare Datei -> leere Vorgabe (wird geloggt), Inhalt muss ein
    Objekt sein.
    """
    for path in config_paths():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: Konfiguration unlesbar (%s: %s) — Defaults", path.name, type(exc).__name__, exc)
            return {}, ""
        if not isinstance(data, dict):
            logger.warning("%s: Konfiguration ist kein Objekt — Defaults", path.name)
            return {}, str(path)
        if path.name == LEGACY_CONFIG_NAME:
            # V1-Werte auf V2-Semantik abbilden (siehe limits.legacy_to_v2). Ohne
            # das waere `wait_for_all: true` aus den sechs Bestandsdateien eine
            # stille Ruecknahme der neuen Ignore-Regel.
            data, notes = legacy_to_v2(data)
            _log_migration_once(path, notes)
        return data, str(path)
    return {}, ""


# Merkt sich je Prozess, fuer welche Datei die Migrationshinweise schon im Log
# stehen — der Suchpfad liest die Konfiguration bei jeder Suche.
_MIGRATION_LOGGED: set[tuple[str, float]] = set()


def _log_migration_once(path: Path, notes: list[str]) -> None:
    if not notes:
        return
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        key = (str(path), 0.0)
    if key in _MIGRATION_LOGGED:
        return
    _MIGRATION_LOGGED.add(key)
    logger.info("%s: V1-Konfiguration abgebildet — %s", path.name, "; ".join(notes))


def _as_bool(value: Any, default: bool) -> bool:
    """Boolesche Konfigwerte: echte Booleans zaehlen, Strings nach ueblicher Lesart.

    JSON kennt nur true/false, aber eine handgeschriebene Datei kann "false"
    enthalten — `bool("false")` waere True und wuerde eine Abschaltung still in
    eine Einschaltung drehen.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("false", "0", "no", "off", "nein", "aus"):
            return False
        if low in ("true", "1", "yes", "on", "ja", "an"):
            return True
        return default
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def clamp_weight(value: Any) -> float:
    """Gewicht auf 0..2 klemmen — ein Wert <= 0 wuerde den Beitrag nullen oder drehen."""
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return DEFAULT_WEIGHT
    return max(0.0, min(2.0, weight))


def canon_url(url: str) -> str:
    """Vergleichbare URL-Form: Schema/www weg, Tracking-Parameter weg, Rest bleibt.

    Robust gegen kaputte Eingaben: `urlsplit` kann bei ungewoehnlichem netloc
    (z. B. NFKC-feindliche Zeichen) werfen, und ein leerer Host darf nicht
    verschiedene Muell-URLs auf denselben Schluessel kollabieren lassen
    (Grok-Review, Runde 2).
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else "http://" + raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        return raw.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return raw.lower()  # nichts zu normalisieren -> Original behalten
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    query = urlencode(sorted(pairs))
    return host + path + ("?" + query if query else "")


def host_of(canon: str) -> str:
    return canon.split("/", 1)[0].split("?", 1)[0]


def _is_public_url(url: str) -> bool:
    """SSRF-Schutz: nur oeffentlich geroutete Ziele pruefen.

    Positivpruefung ueber `is_global` statt einer Negativliste: damit fallen
    auch `0.0.0.0` (`is_unspecified`) und IPv4-mapped Adressen wie
    `::ffff:169.254.169.254` heraus (Grok-Review, Runde 2).

    Bewusste Restluecke: zwischen dieser Pruefung und dem Verbindungsaufbau
    loest urllib den Namen erneut auf (DNS-Rebinding). Ein vollstaendiger Fix
    muesste die gepruefte IP pinnen und den Host nur als SNI/Header mitgeben —
    fuer Suchtreffer aus oeffentlichen Indizes ist das Verhaeltnis dafuer zu
    unguenstig, es ist hier dokumentiert statt versteckt.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP
        )
    except Exception:  # noqa: BLE001
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global:
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects nicht folgen — verhindert Umleitungen auf interne Ziele."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


class SearchMergeWebSearchProvider(BaseWebSearchProvider):
    """Fragt mehrere Anbieter parallel ab und fuehrt die Treffer zusammen."""

    name = ID

    # ------------------------------------------------------------- Faehigkeiten

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def is_available(self) -> bool:
        """Nur verfuegbar, wenn mindestens eine Quelle aktiviert ist.

        Vorher unbedingt True — irrefuehrend, wenn die Konfiguration leer ist
        (Grok-Review 03-10-2026).
        """
        index_sources, llm_sources = self._load_sources(load_config()[0])
        return bool(index_sources or llm_sources)

    # ---------------------------------------------------------------- Fan-out

    def _llm_role_names(self, cfg: dict[str, Any]) -> set[str]:
        raw = cfg.get("llm_sources")
        if raw is None:
            return set(LLM_ROLE_DEFAULT)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            return set(LLM_ROLE_DEFAULT)
        names = {str(item).strip() for item in raw if str(item or "").strip()}
        return names or set(LLM_ROLE_DEFAULT)

    def _source_limit(self, item: dict[str, Any], name: str) -> int:
        """Trefferzahl DIESER Quelle: eigenes `limit`, sonst Vorgabe der Kostenklasse.

        Das Altfeld `provider_limit` wird bewusst NICHT als Fallback benutzt. In
        V1 war es der eine Deckel fuer alle Quellen (100) — als Fallback
        uebernommen wuerde jede migrierte Konfiguration die bezahlten Quellen
        weiter mit 100 abfragen und die Kostenkontrolle waere wirkungslos, obwohl
        sie in der Oberflaeche steht. Stattdessen greift die Kostenklasse; das
        Feld bleibt nur im Schema erhalten, damit eine V1-Datei verlustfrei
        durch die Validierung laeuft.
        """
        raw = item.get("limit")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default_limit_for(name)
        return clamp_int(raw, LIMIT_MIN, LIMIT_MAX, default_limit_for(name))

    def _load_sources(self, cfg: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        raw = cfg.get("sources")
        if not isinstance(raw, list):
            raw = [
                {"name": n, "weight": DEFAULT_WEIGHT, "enabled": n in DEFAULT_ENABLED}
                for n in sorted(DEFAULT_ENABLED)
            ]
        llm_role = self._llm_role_names(cfg)
        index_sources: list[dict] = []
        llm_sources: list[dict] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            if not item.get("enabled"):
                continue
            name = str(item["name"])
            entry = {
                "name": name,
                "weight": clamp_weight(item.get("weight", DEFAULT_WEIGHT)),
                "limit": self._source_limit(item, name),
                "role": "llm" if name in llm_role else "index",
            }
            (llm_sources if entry["role"] == "llm" else index_sources).append(entry)
        return index_sources, llm_sources

    def _resolve(self, name: str):
        """Provider-Adapter aus der Registry holen (wie der Kern es tut)."""
        from agent.web_search_registry import get_provider  # noqa: PLC0415

        try:
            return get_provider(name)
        except Exception:  # noqa: BLE001
            return None

    def _scoped_secret_scope(self):
        """Contextmanager: bindet das Secret-Scope des aktiven Profils.

        Notwendig, weil der Fan-out an MEHRERE Provider delegiert: unter
        Multiplex-Semantik verweigert `get_provider_env` ohne gebundenes Scope
        jeden Secret-Zugriff (gemessen 03-10-2026: UnscopedSecretError bei
        searxng, firecrawl und xai — 0 Treffer). Der Agent-Turn bindet den
        Scope fuer normale Tool-Aufrufe nicht; nur MCP-, Kanban- und
        Dashboard-Pfade tun das (web_server_mcp.py:132,
        kanban_db_dispatch.py:2672, web_server_profiles.py:316).

        Uebersehen wird ein bereits gebundener Scope — der Aufrufer hat dann
        Vorrang. Die Bindung ist ein ContextVar, greift also pro Thread; sie
        wird deshalb im Fan-out VOR den Worker-Threads gesetzt.
        """
        import contextlib

        @contextlib.contextmanager
        def _scope():
            from agent.secret_scope import (
                build_profile_secret_scope,
                current_secret_scope,
                reset_secret_scope,
                set_secret_scope,
            )

            if current_secret_scope() is not None:
                yield  # Aufrufer hat bereits gebunden
                return
            try:
                secrets = build_profile_secret_scope(_hermes_home())
            except Exception as exc:  # noqa: BLE001
                logger.warning("search-merge: Secret-Scope nicht baubar (%s)", type(exc).__name__)
                yield
                return
            token = set_secret_scope(secrets, profile_home=str(_hermes_home()))
            try:
                yield
            finally:
                reset_secret_scope(token)

        return _scope()

    def _effective_provider_name(self, name: str) -> str:
        """Konfigurationsquelle -> tatsaechlich gerufener Registry-Name.

        Siehe ``limits.SOURCE_ALIASES``: ``searxng`` wird auf den paginierenden
        Provider des Plugins gelegt, weil der gebuendelte genau eine Seite holt.
        Ist das Ziel nicht registriert, bleibt es beim gebuendelten Provider —
        ein fehlendes Ziel darf die Suche nicht verschlechtern. Der wirksame Name
        steht je Quelle in ``meta.sources.<name>.resolved_as``.
        """
        target = SOURCE_ALIASES.get(name)
        if not target or target == name:
            return name
        if self._resolve(target) is None:
            logger.info(
                "search-merge: '%s' nicht registriert — Quelle '%s' laeuft ueber den gebuendelten Provider",
                target, name,
            )
            return name
        return target

    def _query_one(self, name: str, query: str, limit: int) -> dict[str, Any]:
        started = time.time()
        provider_name = self._effective_provider_name(name)
        try:
            provider = self._resolve(provider_name)
            if provider is None:
                return {
                    "name": name,
                    "resolved_as": provider_name,
                    "ok": False,
                    "error": "nicht registriert",
                    "dur": 0.0,
                    "rows": [],
                }
            response = provider.search(query, limit)
            rows = ((response or {}).get("data") or {}).get("web") or []
            return {
                "name": name,
                "resolved_as": provider_name,
                "ok": True,
                "dur": time.time() - started,
                "rows": [r for r in rows if isinstance(r, dict) and r.get("url")],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "search-merge: Quelle '%s' (ueber %s) fehlgeschlagen (%s)",
                name, provider_name, type(exc).__name__,
            )
            return {
                "name": name,
                "resolved_as": provider_name,
                "ok": False,
                "error": f"{type(exc).__name__}",
                "dur": time.time() - started,
                "rows": [],
            }

    def _fan_out(
        self,
        sources: list[dict],
        query: str,
        limits: dict[str, int],
        window_s: float,
    ) -> tuple[dict[str, dict], float, list[str], int]:
        """Alle Quellen parallel abfragen, hoechstens `window_s` lang auf sie warten.

        Rueckgabe: Ergebnisse, Wanduhr, Namen der fuer diesen Turn ignorierten
        Quellen, Anzahl der Threads, die danach im Hintergrund weiterlaufen.

        `window_s` ist das wirksame Wartefenster (siehe `search`) — es wird von
        `deadline_s`/`llm_budget_s`/`hard_timeout_s` gebildet, nicht mehr von
        einem starren `hard_timeout_s: 120`.
        """
        # Scope binden BEVOR die Threads starten (copy_context unten nimmt ihn
        # mit). Sonst faellt jeder Provider mit UnscopedSecretError aus.
        with self._scoped_secret_scope():
            return self._fan_out_with_scope(sources, query, limits, window_s)

    def _fan_out_with_scope(
        self,
        sources: list[dict],
        query: str,
        limits: dict[str, int],
        window_s: float,
    ) -> tuple[dict[str, dict], float, list[str], int]:
        """Der eigentliche Fan-out; laeuft innerhalb des gebundenen Secret-Scopes."""
        global _ORPHANED_TOTAL  # noqa: PLW0603

        started = time.time()
        results: dict[str, dict] = {}
        if not sources:
            return results, 0.0, [], 0

        orphaned: list[str] = []
        # Gleichzeitige Runden begrenzen: verhindert, dass parallele Suchen des
        # Gateways beliebig viele Pools und Threads aufmachen. Pool-Erstellung
        # liegt IM try, sonst bliebe der Semaphore-Slot bei einem Fehler belegt.
        FANOUT_SLOTS.acquire()
        pool = None
        try:
            pool = ThreadPoolExecutor(max_workers=min(len(sources), 6))
            # copy_context(): der Secret-Scope ist ein ContextVar, ein
            # ThreadPoolExecutor uebertraegt den Kontext nicht.
            # PRO WORKER EIN EIGENER KONTEXT (gemessen 03-10-2026): ein
            # gemeinsamer ctx.run unter N gleichzeitig laufenden Workern wirft
            # RuntimeError "cannot enter context: ... is already entered" — ein
            # Context-Objekt darf nicht parallel betreten werden. Der Fehler
            # wurde bisher von `except BaseException` als warning geschluckt,
            # sichtbar war nur "1 Quelle ok, Rest 0". Deshalb hier pro submit
            # ein frisches copy_context().
            futures = {
                pool.submit(
                    contextvars.copy_context().run,
                    self._query_one,
                    s["name"],
                    query,
                    limits.get(s["name"], default_limit_for(s["name"])),
                ): s["name"]
                for s in sources
            }
            try:
                for future in as_completed(futures, timeout=window_s):
                    try:
                        res = future.result()
                    except BaseException as exc:  # CancelledError ist BaseException
                        logger.warning("search-merge: Future lieferte einen Fehler (%s)", type(exc).__name__)
                        continue
                    results[res["name"]] = res
            except TimeoutError:
                pass
            # Fertige, aber nicht mehr geerntete Futures nachlesen — sonst zaehlt
            # eine puenktliche Quelle als "Zeitlimit" mit leeren Treffern, und ein
            # geworfener Fehler verschwaende still (Grok-Review).
            for future, name in futures.items():
                if name in results or future.cancelled() or not future.done():
                    continue
                try:
                    res = future.result()
                    results[res["name"]] = res
                except BaseException as exc:  # noqa: BLE001
                    results[name] = {
                        "name": name,
                        "ok": False,
                        "error": type(exc).__name__,
                        "dur": time.time() - started,
                        "rows": [],
                    }
            # Nicht fertig = fuer DIESEN Turn ignoriert. Kein Nachwarten mehr:
            # genau das war das V1-Verhalten, das den Turn bis zu 120 s hielt.
            orphaned = [name for fut, name in futures.items() if name not in results and not fut.done()]
            for fut, name in futures.items():
                if name not in results:
                    results[name] = {
                        "name": name,
                        "ok": False,
                        "error": "Budget ueberschritten",
                        "dur": time.time() - started,
                        "rows": [],
                    }
        finally:
            # Bewusst OHNE "with": dessen shutdown(wait=True) wartet auf die
            # langsamste Quelle und hebt das Limit auf (gemessen 39,3 s bei
            # deadline_s 15). Laufende provider.search()-Aufrufe sind in Python
            # nicht abbrechbar — sie laufen im Hintergrund aus, ihr Ergebnis wird
            # verworfen (gezaehlt als "orphaned").
            try:
                if pool is not None:
                    pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                logger.warning("search-merge: shutdown des Pools fehlgeschlagen")
            finally:
                # Slot immer freigeben, auch wenn shutdown wirft.
                FANOUT_SLOTS.release()

        if orphaned:
            with _ORPHAN_LOCK:
                _ORPHANED_TOTAL += len(orphaned)
                total = _ORPHANED_TOTAL
            logger.info(
                "search-merge: %s Quelle(n) ueber dem Budget, fuer diesen Turn ignoriert (%s); insgesamt %s",
                len(orphaned),
                ", ".join(orphaned),
                total,
            )
        return results, time.time() - started, orphaned, len(orphaned)

    # ------------------------------------------------------------------ Merge

    def _merge(self, results: dict[str, dict], weights: dict[str, float]) -> tuple[list[dict[str, Any]], set[str]]:
        """URL-Kanonisierung -> Dedupe -> Rangfusion (RRF) -> Neu-Nummerierung.

        Deterministisch: behalten wird die Zeile mit der besten (kleinsten)
        Position — nicht die zuerst fertige Quelle, denn die Dict-Reihenfolge
        war die Fertigstellungsreihenfolge (Grok-Review 03-10-2026).
        """
        scores: dict[str, float] = {}
        best: dict[str, dict[str, Any]] = {}
        best_pos: dict[str, int] = {}
        origins: dict[str, list[str]] = {}
        for name in sorted(results):
            res = results[name]
            weight = clamp_weight(weights.get(name, DEFAULT_WEIGHT))
            for position, row in enumerate(res.get("rows") or [], start=1):
                key = canon_url(row.get("url"))
                if not key:
                    continue
                scores[key] = scores.get(key, 0.0) + weight / (RRF_K + position)
                origins.setdefault(key, [])
                if name not in origins[key]:
                    origins[key].append(name)
                if key not in best or position < best_pos[key]:
                    best[key] = row
                    best_pos[key] = position
        # Zweites Sortierkriterium = URL: gleiche Punktzahl -> stabile Reihenfolge.
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        merged: list[dict[str, Any]] = []
        for rank, (key, score) in enumerate(ordered, start=1):
            row = dict(best[key])
            row["position"] = rank
            row["description"] = row.get("description") or ""
            row["_sources"] = origins.get(key, [])
            row["_score"] = score
            merged.append(row)
        return merged, set(scores)

    def _rerank(self, merged: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Domain-Gewichtung ueber dem Merge (A2). Wikipedia bleibt neutral (A3).

        Die Faktoren sind moderat (1,12 / 0,88): bei k=60 ist der Rangabstand
        rund 1/61, ein Faktor 1,5 entspraeche ~30 Rangplaetzen und waere kein
        Rerank, sondern ein Override (Grok-Review 03-10-2026).
        """
        if not _as_bool(cfg.get("rerank_enabled"), True):
            return merged
        rescored: list[dict[str, Any]] = []
        for row in merged:
            key = canon_url(row.get("url"))
            host = host_of(key)
            factor = 1.0
            if any(host == d or host.endswith("." + d) for d in DEMOTE_DOMAINS):
                factor *= DEMOTE_FACTOR
            if any(host == d or host.endswith("." + d) for d in BOOST_DOMAINS):
                factor *= BOOST_FACTOR
            # Wikipedia bewusst ohne Faktor (A3, Gewicht 1). Die geforderte
            # Differenzierung nach Themen gehoert in einen Themen-Detektor, nicht
            # in einen wirkungslosen Sonderfall an dieser Stelle.
            new_row = dict(row)
            new_row["_score"] = row.get("_score", 0.0) * factor
            rescored.append(new_row)
        rescored.sort(key=lambda row: (-row.get("_score", 0.0), canon_url(row.get("url"))))
        for rank, row in enumerate(rescored, start=1):
            row["position"] = rank
        return rescored

    # --------------------------------------------------------- LLM-Zweitstimme

    def _llm_rows(self, llm_sources: list[dict], results: dict[str, dict], visible: list[dict], cfg: dict[str, Any]) -> list[dict]:
        """Nachtrag der LLM-Sicht: nur bestaetigte URLs, klar gekennzeichnet (A4/A6).

        `visible` ist der Ausschnitt, den der Aufrufer auch sieht — nicht die
        volle Merkliste: eine LLM-URL, die nur jenseits des Slices doppelt waere,
        wuerde sonst ganz verschwinden (Grok-Review, Runde 2). Die Abrufpruefung
        laeuft unter einem Gesamtbudget, damit sie nicht max_added x (DNS +
        Timeout) im Gateway-Thread verbrennt.
        """
        if not _as_bool(cfg.get("llm_second_opinion"), True):
            return []
        max_added = clamp_int(cfg.get("max_added"), MAX_ADDED_MIN, MAX_ADDED_MAX, 10)
        if max_added <= 0:
            return []
        known = {canon_url(r.get("url")) for r in visible}
        verify = _as_bool(cfg.get("verify_urls"), True)
        unverified_mode = str(cfg.get("unverified", "mark"))
        budget_s = clamp_float(cfg.get("verify_budget_s"), 1.0, 60.0, 10.0)
        budget_end = time.time() + budget_s
        extra: list[dict] = []
        for source in llm_sources:
            res = results.get(source["name"]) or {}
            if not res.get("ok"):
                continue  # LLM-Quelle fiel aus oder wurde fuer den Turn ignoriert
            for row in res.get("rows") or []:
                key = canon_url(row.get("url"))
                if not key or key in known:
                    continue  # Bestaetigung, nicht doppelt fuehren
                if verify and time.time() > budget_end:
                    logger.info("search-merge: Abrufpruefung nach Budget beendet (%s uebernommen)", len(extra))
                    return extra
                ok = self._url_reachable(row.get("url")) if verify else None
                if verify and not ok:
                    if unverified_mode == "drop":
                        continue
                known.add(key)
                text = (row.get("description") or "").strip()
                prefix = "[LLM-Sicht]" if ok is not False else "[unbestaetigt, LLM-Sicht]"
                extra.append({
                    "title": row.get("title") or "",
                    "url": row.get("url"),
                    "description": (prefix + " " + text).strip(),
                    "position": 0,
                    "_sources": [source["name"] + " (LLM)"],
                })
                if len(extra) >= max_added:
                    return extra
        return extra

    def _url_reachable(self, url: str) -> bool:
        """Abrufpflicht vor Aufnahme (A6) — mit SSRF-Schutz.

        Nur oeffentliche Ziele, nur HEAD, keine Redirects, kein Body, 3 s.
        """
        target = str(url or "").strip()
        if not _is_public_url(target):
            return False
        opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=_SSL_CTX))
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(target, method=method, headers={"User-Agent": _UA})
                with opener.open(req, timeout=3) as resp:
                    if method == "GET":
                        resp.read(1)  # nur anlesen, keinen Body ziehen
                    return 200 <= int(getattr(resp, "status", 200)) < 400
            except urllib.error.HTTPError as exc:
                if exc.code == 405 and method == "HEAD":
                    continue  # HEAD nicht erlaubt -> einmal mit GET versuchen
                return 200 <= int(exc.code) < 400
            except Exception:  # noqa: BLE001
                return False
        return False

    # ------------------------------------------------------------------ Suche

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        cfg, cfg_path = load_config()
        # Ungeklemmter Aufruferwert festhalten, BEVOR geklemmt wird — sonst
        # waere in meta nicht mehr erkennbar, dass der Wunsch ueber der Grenze lag.
        requested_raw = limit
        limit = clamp_int(limit, FANOUT_MIN, KERN_MAX, FANOUT_MIN)
        index_sources, llm_sources = self._load_sources(cfg)
        if not index_sources and not llm_sources:
            return {
                "success": False,
                "error": "kein Anbieter aktiv (search-merge.json)",
                "data": {"web": []},
            }

        weights = {s["name"]: s["weight"] for s in index_sources + llm_sources}

        deadline_s = clamp_float(cfg.get("deadline_s"), DEADLINE_MIN_S, DEADLINE_MAX_S, DEFAULT_DEADLINE_S)
        llm_budget_s = clamp_float(cfg.get("llm_budget_s"), LLM_BUDGET_MIN_S, LLM_BUDGET_MAX_S, DEFAULT_LLM_BUDGET_S)
        hard_timeout_s = clamp_float(cfg.get("hard_timeout_s"), HARD_TIMEOUT_MIN_S, HARD_TIMEOUT_MAX_S, DEFAULT_HARD_TIMEOUT_S)
        wait_for_all = _as_bool(cfg.get("wait_for_all"), DEFAULT_WAIT_FOR_ALL)
        llm_wait = _as_bool(cfg.get("llm_wait"), DEFAULT_LLM_WAIT)

        # Teure Quellen im reinen Deadline-Modus gar nicht erst starten: die
        # LLM-Suche braucht 12-31 s und wuerde bei 15 s Budget immer verworfen —
        # sie hinterliesse nur einen nicht abbrechbaren Thread im Gateway
        # (Grok-Review, Runde 2: groesstes Risiko fuer den Dauerbetrieb).
        skipped: list[str] = []
        if not wait_for_all and not (llm_wait and llm_sources):
            skipped = [s["name"] for s in llm_sources]
            llm_sources = []
            logger.info("search-merge: LLM-Quellen ohne Wartebudget uebersprungen (%s)", ", ".join(skipped))

        # Wirksames Wartefenster. `wait_for_all` ist der Alt-Schalter ("auf alle
        # warten") und wird auf den absoluten Deckel abgebildet; sonst bestimmt
        # das groessere der beiden Budgets die Wartezeit, hart begrenzt.
        if wait_for_all:
            window_s, window_mode = hard_timeout_s, "wait_for_all"
        else:
            window_s = max(deadline_s, llm_budget_s if (llm_wait and llm_sources) else 0.0)
            window_s = min(window_s, hard_timeout_s)
            window_mode = "budget"

        all_sources = index_sources + llm_sources
        limits = {s["name"]: s["limit"] for s in all_sources}
        llm_names = {s["name"] for s in llm_sources}

        results, wall, ignored, orphaned_n = self._fan_out(all_sources, query, limits, window_s)

        merged, unique_keys = self._merge(
            {k: v for k, v in results.items() if k not in llm_names}, weights
        )
        merged = self._rerank(merged, cfg)
        visible = merged[:limit]  # genau das, was der Aufrufer als Index-Treffer sieht
        with self._scoped_secret_scope():
            llm_extra = self._llm_rows(llm_sources, results, visible, cfg)

        max_added_cap = clamp_int(cfg.get("max_added"), MAX_ADDED_MIN, MAX_ADDED_MAX, 10)
        llm_position = str(cfg.get("llm_position") or "append").strip().lower()
        if llm_position not in ("append", "meta_only"):
            llm_position = "append"
        # Das Limit gilt fuer die Index-Treffer; der LLM-Nachtrag kommt ZUSAETZLICH,
        # aber gedeckelt (limit + max_added) — sonst sprengt er den Aufrufer-
        # Vertrag beliebig (Grok-Review, Runde 2). Bei `meta_only` bleibt er aus
        # `data.web` draussen und steht nur unter `meta.llm_results`.
        combined = (visible + llm_extra)[: limit + max_added_cap] if llm_position == "append" else visible[:limit]
        for rank, row in enumerate(combined, start=1):
            row["position"] = rank
            row.pop("_score", None)  # rein internes Feld, gehoert nicht in die Ausgabe

        result = search_ok(combined)
        result["data"]["meta"] = {
            "wall_s": round(wall, 2),
            "budget": {
                "deadline_s": deadline_s,
                "llm_budget_s": llm_budget_s,
                "hard_timeout_s": hard_timeout_s,
                "window_s": round(window_s, 2),
                "window_mode": window_mode,
                "wait_for_all": wait_for_all,
                "llm_wait": llm_wait,
            },
            "ignored_sources": ignored,
            "ignored_total": _ORPHANED_TOTAL,
            "orphaned_threads": orphaned_n,
            "skipped_sources": skipped,
            "unique_urls": len(unique_keys),
            "merged": len(merged),
            "llm_added": len(llm_extra),
            "llm_position": llm_position,
            # Wirksame Regler ausweisen. Ohne das ist eine stille Klemme nicht
            # von einer gewollten Einstellung zu unterscheiden.
            "requested_limit": requested_raw,
            "fanout_limit": limit,
            "config_path": cfg_path,
            "limits": {
                "fanout_min": FANOUT_MIN,
                "fanout_max": KERN_MAX,
                "source_min": LIMIT_MIN,
                "source_max": LIMIT_MAX,
            },
            "sources": {
                name: {
                    "ok": res["ok"],
                    "dur": round(res["dur"], 2),
                    "n": len(res["rows"]),
                    "error": res.get("error"),
                    "limit": limits.get(name, default_limit_for(name)),
                    "role": "llm" if name in llm_names else "index",
                    "cost_class": cost_class(name),
                    "ignored": name in ignored,
                    # Welcher Registry-Provider die Quelle wirklich bedient (siehe
                    # SOURCE_ALIASES) — sonst muesste man raten, ob die
                    # Seiten-Schleife des Plugins greift.
                    "resolved_as": res.get("resolved_as"),
                }
                for name, res in sorted(results.items())
            },
        }
        if llm_extra:
            result["data"]["meta"]["llm_results"] = [
                {"title": r.get("title"), "url": r.get("url"), "sources": r.get("_sources")} for r in llm_extra
            ]
        return result


class SearchFanoutLegacyWebSearchProvider(SearchMergeWebSearchProvider):
    """Alias fuer Profile, deren ``web.search_backend`` noch ``search-fanout`` sagt.

    Gleiche Klasse, nur ein anderer Registry-Name: kein zweiter Code-Stand, kein
    Verhalten, das auseinanderlaufen koennte. Der Alias ist der Grund, warum die
    Umbenennung kein Flag-Day war — ohne ihn haette der Kern in `_resolve()` auf
    den Legacy-Walk zurueckgegriffen und jedes Profil still ueber firecrawl,
    parallel, tavily oder searxng gesucht, ohne Fehlermeldung.

    Entfernen erst, wenn kein Profil mehr die alte ID setzt:
        grep -rl 'search_backend: search-fanout' ~/.hermes/config.yaml ~/.hermes/profiles/*/config.yaml
    """

    name = LEGACY_ID
