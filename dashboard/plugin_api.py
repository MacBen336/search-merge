"""Backend fuer das Search-Merge-Plugin (V2 des Such-Fan-outs).

Bedient BEIDE Oberflaechen ueber denselben Namespace /api/plugins/search-merge/:
  - die Web-Dashboard-Haelfte (dashboard/dist/index.js)
  - die Desktop-Haelfte      (desktop/plugin.js)

Laeuft im Gateway-Prozess (FastAPI-Mount), daher Zugriff auf den hermes-agent-Code.

Routen:
  GET  /state?profile=<slug>   -> Anbieterliste (bekannt/verfuegbar), aktuelle Config, Defaults
  POST /config                 -> Config validieren + speichern
  POST /reset                  -> auf Defaults zuruecksetzen

Konfigurationsdatei je Profil: <HERMES_HOME>/search-merge.json
  Default-Profil -> ~/.hermes/search-merge.json
  benannte Profile -> ~/.hermes/profiles/<slug>/search-merge.json

MIGRATION: Gelesen wird search-merge.json; fehlt sie, search-fanout.json (der
V1-Name). GESCHRIEBEN wird ausschliesslich die neue Datei — die alte bleibt
unangetastet liegen, damit ein Rollback auf V1 seine Einstellungen wiederfindet.
Ein Profil, das noch nie ueber die Oberflaeche gespeichert wurde, liest also
weiter seine V1-Datei; ab dem ersten Speichern gilt die neue. Beide Pfade
stehen in der Antwort (`config_path` / `config_path_read`), damit sichtbar ist,
welche Datei wirklich wirkt.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
import re
import sys as _sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

# Profilnamen: Buchstaben/Ziffern/Bindestrich/Unterstrich. Bewusst eng — der
# Name wird zu einem Verzeichnisnamen unter ROOT/"profiles".
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

PLUGIN_ID = "search-merge"
CONFIG_NAME = "search-merge.json"
LEGACY_CONFIG_NAME = "search-fanout.json"

# Grenzwerte aus limits.py — dieselbe Datei, die provider.py liest, damit
# Oberflaeche und Suchpfad dieselben Zahlen anwenden.
#
# Der Import ist ein Pfad-Loader statt `from ..limits import`: Hermes laedt
# diese Datei per `spec_from_file_location("hermes_dashboard_plugin_<name>", ...)`
# OHNE `submodule_search_locations` (hermes_cli/plugin_host_child.py:308), ein
# relativer Import daraus schlaegt mit ImportError fehl — gemessen 03-10-2026
# mit genau diesem Ladeweg nachgebaut.
#
# Der Modulname traegt den Inhalts-Hash (siehe limit.py und provider.py):
# gleicher Inhalt -> eine Instanz fuer beide Seiten, geaenderter Inhalt -> neue
# Instanz. Ein STABILER Name hat provider.py eine veraltete limits-Instanz
# untergeschoben und das Plugin beim Reload komplett ausfallen lassen.
import hashlib as _hashlib

_LIMITS_PATH = Path(__file__).resolve().parent.parent / "limits.py"
_LIMITS_MOD = "search_merge_limits_shared_" + _hashlib.sha256(_LIMITS_PATH.read_bytes()).hexdigest()[:12]
_limits = _sys.modules.get(_LIMITS_MOD)
if _limits is None:
    _spec = _ilu.spec_from_file_location(_LIMITS_MOD, _LIMITS_PATH)
    _limits = _ilu.module_from_spec(_spec)
    _sys.modules[_LIMITS_MOD] = _limits
    _spec.loader.exec_module(_limits)

HERMES_ROOT = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
# Im Gateway-Kontext zeigt HERMES_HOME ggf. auf ein Profil; die Profil-Homes
# liegen dann eine Ebene hoeher. Beide Faelle abfangen.
if HERMES_ROOT.name == "default":
    ROOT = HERMES_ROOT.parent.parent
elif (HERMES_ROOT / "profiles").is_dir():
    ROOT = HERMES_ROOT
elif HERMES_ROOT.parent.name == "profiles":
    ROOT = HERMES_ROOT.parent.parent
else:
    ROOT = HERMES_ROOT

# Die Anbieter, die diese Installation mitbringt (plugins/web/<name>/).
KNOWN_PROVIDERS = [
    "brave_free",
    "ddgs",
    "exa",
    "firecrawl",
    "keenable",
    "openai_native",
    "parallel",
    "perplexity",
    "searxng",
    "tavily",
    "xai",
]

LABELS = {
    "brave_free": "Brave (free)",
    "ddgs": "DuckDuckGo (ddgs)",
    "exa": "Exa",
    "firecrawl": "Firecrawl",
    "keenable": "Keenable",
    "openai_native": "OpenAI",
    "parallel": "Parallel",
    "perplexity": "Perplexity",
    "searxng": "SearXNG",
    "tavily": "Tavily",
    "xai": "xAI / Grok",
}

# Startbelegung: kostenlose/verfuegbare Quellen an, Rest aus.
DEFAULT_ENABLED = {"searxng", "ddgs", "xai", "firecrawl"}

# Quellen mit LLM-Rolle: generierte URLs ohne Index dahinter, laufen als
# gekennzeichneter Nachtrag statt in der Rangfusion. Muss zu
# provider.LLM_ROLE_DEFAULT passen.
DEFAULT_LLM_SOURCES = ["xai"]

_RERANK_UNUSED = "rerank_skip_wikipedia_downgrade"  # Altfeld, bleibt im Schema

DEFAULTS: dict[str, Any] = {
    # Budget der Index-Quellen.
    "deadline_s": 15,
    # Budget des LLM-Nachtrags (xAI gemessen 12,5-31 s; 20 s nimmt die schnellen
    # Laeufe mit und verwirft die langsamen, statt den Turn lang zu halten).
    "llm_budget_s": 20,
    # Absoluter Deckel ueber beide Budgets (V1: 120).
    "hard_timeout_s": 30,
    # Alt-Schalter "auf alle warten" — V1-Vorgabe war True und hat den Turn bis
    # zu zwei Minuten festgehalten. V2: das Budget zaehlt.
    "wait_for_all": False,
    # Auf den LLM-Nachtrag bis zu seinem Budget warten (sonst gar nicht starten).
    "llm_wait": True,
    # GLOBALES Altfeld. V2 dosiert je Quelle (`sources[].limit`); dieses Feld
    # bleibt nur im Schema, damit eine V1-Datei verlustfrei durch die Validierung
    # laeuft, und wird vom Suchpfad NICHT mehr angewandt.
    "provider_limit": 100,
    "sources": [],  # unten gefuellt
    "llm_sources": list(DEFAULT_LLM_SOURCES),
    "llm_second_opinion": True,
    # "append" = Nachtrag an data.web anhaengen (V1-Verhalten, mit [LLM-Sicht]
    # gekennzeichnet) | "meta_only" = nur unter meta.llm_results.
    "llm_position": "append",
    "verify_urls": True,
    "verify_budget_s": 10,
    "unverified": "mark",
    "max_added": 10,
    "rerank_enabled": True,
    "rerank_skip_wikipedia_downgrade": True,
}

DEFAULTS["sources"] = [
    {
        "name": n,
        "weight": 1.0,
        "enabled": n in DEFAULT_ENABLED,
        "limit": _limits.default_limit_for(n),
    }
    for n in KNOWN_PROVIDERS
]


def _known_profiles() -> dict[str, Path]:
    """Die real existierenden Profil-Homes: Ordner unter ``profiles/`` mit config.yaml.

    Nicht ueber den angefragten Slug enumerieren, sondern aus dem Dateisystem —
    und Symlinks auslassen: ``is_dir()`` folgt ihnen, ein Symlink-Ordner wuerde
    sonst als Profil gelistet und die Config ausserhalb der Wurzel geschrieben.
    """
    idx: dict[str, Path] = {}
    proot = ROOT / "profiles"
    try:
        children = sorted(proot.iterdir()) if proot.is_dir() else []
    except OSError:
        children = []
    for child in children:
        if child.name in ("default", "bender"):
            continue  # diese Identitaeten liegen in der Wurzel, nicht unter profiles/
        try:
            if not child.is_dir() or child.is_symlink():
                continue
        except OSError:
            continue
        if not (child / "config.yaml").is_file():
            continue
        idx[child.name] = child
    return idx


def _profile_home(profile: str | None) -> Path | None:
    """Profil-Home zum Namen — oder ``None``, wenn der Name unbrauchbar/unbekannt ist.

    Bewusst KEIN Pfad aus dem Request bauen: der Name geht in einen Dateipfad ein
    (``ROOT / "profiles" / slug``), und ``../../…`` schrieb damit ausserhalb des
    Hermes-Baums — die Route kennt nur Profilnamen, also muss sie auch nur
    bekannte Namen annehmen (404 fuer alles andere, auch Pfadartiges).
    """
    slug = (profile or "").strip()
    if not slug or slug in ("default", "bender"):
        return ROOT
    if not _SLUG_RE.match(slug):
        return None
    home = _known_profiles().get(slug)
    if home is None:
        return None
    # Klammer-Pruefung: der aufgeloeste Pfad muss innerhalb der Wurzel bleiben.
    try:
        resolved, root = home.resolve(), ROOT.resolve()
    except OSError:
        return None
    if not (resolved == root or root in resolved.parents):
        return None
    return home


def _require_home(profile: str | None) -> Path:
    home = _profile_home(profile)
    if home is None:
        raise HTTPException(status_code=404, detail=f"unbekanntes Profil: {profile!r}")
    return home


def _config_path(profile: str | None) -> Path:
    return _require_home(profile) / CONFIG_NAME


def _legacy_config_path(profile: str | None) -> Path:
    return _require_home(profile) / LEGACY_CONFIG_NAME


def _read_path(profile: str | None) -> Path:
    """Welche Datei gelesen wird: die neue, sonst die alte (Migration)."""
    home = _require_home(profile)
    new = home / CONFIG_NAME
    if new.is_file():
        return new
    old = home / LEGACY_CONFIG_NAME
    if old.is_file():
        return old
    return new


def _available_providers() -> dict[str, Any]:
    """Live-Verfuegbarkeit aus der Provider-Registry. Fehler werden gemeldet, nicht geworfen."""
    out: dict[str, Any] = {}
    try:
        import agent.web_search_registry as wsr

        providers = wsr.list_providers() or []
        items = providers.items() if isinstance(providers, dict) else providers
        for entry in items:
            if isinstance(entry, tuple):
                name, obj = entry[0], entry[1]
            else:
                name, obj = getattr(entry, "name", str(entry)), entry
            # Das Merge-Backend selbst ist keine waehlbare Quelle — es wuerde
            # sich sonst rekursiv aufrufen. Beide Namen ausblenden.
            if str(name) in (PLUGIN_ID, "search-fanout"):
                continue
            try:
                avail = bool(wsr.is_available_safe(obj)) if hasattr(wsr, "is_available_safe") else bool(obj.is_available())
            except Exception:  # noqa: BLE001
                avail = None
            out[str(name)] = {"available": avail}
    except Exception as exc:  # noqa: BLE001
        out["__registry_error__"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return out


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, num))


def _validate(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Unbekannte Anbieter verwerfen, fehlende ergaenzen, Gewichte und Limits klemmen."""
    warnings: list[str] = []
    cfg: dict[str, Any] = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg, ["Eingabe war kein Objekt - Defaults zurueckgegeben"]

    cfg["deadline_s"] = _limits.clamp_float(raw.get("deadline_s"), _limits.DEADLINE_MIN_S, _limits.DEADLINE_MAX_S, DEFAULTS["deadline_s"])
    cfg["llm_budget_s"] = _limits.clamp_float(raw.get("llm_budget_s"), _limits.LLM_BUDGET_MIN_S, _limits.LLM_BUDGET_MAX_S, DEFAULTS["llm_budget_s"])
    cfg["hard_timeout_s"] = _limits.clamp_float(raw.get("hard_timeout_s"), _limits.HARD_TIMEOUT_MIN_S, _limits.HARD_TIMEOUT_MAX_S, DEFAULTS["hard_timeout_s"])
    cfg["wait_for_all"] = _bool(raw.get("wait_for_all"), DEFAULTS["wait_for_all"])
    cfg["llm_wait"] = _bool(raw.get("llm_wait"), DEFAULTS["llm_wait"])
    cfg["max_added"] = int(_limits.clamp_int(raw.get("max_added"), _limits.MAX_ADDED_MIN, _limits.MAX_ADDED_MAX, DEFAULTS["max_added"]))
    cfg["verify_budget_s"] = _limits.clamp_float(raw.get("verify_budget_s"), _limits.VERIFY_BUDGET_MIN_S, _limits.VERIFY_BUDGET_MAX_S, DEFAULTS["verify_budget_s"])
    cfg["llm_second_opinion"] = _bool(raw.get("llm_second_opinion"), DEFAULTS["llm_second_opinion"])
    cfg["verify_urls"] = _bool(raw.get("verify_urls"), DEFAULTS["verify_urls"])
    cfg["rerank_enabled"] = _bool(raw.get("rerank_enabled"), DEFAULTS["rerank_enabled"])
    cfg["rerank_skip_wikipedia_downgrade"] = _bool(
        raw.get("rerank_skip_wikipedia_downgrade"), DEFAULTS["rerank_skip_wikipedia_downgrade"]
    )
    # Altfeld: wird nur noch durchgereicht, nicht mehr angewandt (siehe DEFAULTS).
    cfg["provider_limit"] = int(_limits.clamp_int(raw.get("provider_limit"), 1, _limits.KERN_MAX, DEFAULTS["provider_limit"]))

    position = str(raw.get("llm_position") or DEFAULTS["llm_position"]).strip().lower()
    if position not in ("append", "meta_only"):
        warnings.append(f"llm_position '{position}' unbekannt - auf 'append' gesetzt")
        position = "append"
    cfg["llm_position"] = position

    unverified = str(raw.get("unverified") or DEFAULTS["unverified"])
    if unverified not in ("drop", "mark"):
        warnings.append(f"unverified '{unverified}' unbekannt - auf 'mark' gesetzt")
        unverified = "mark"
    cfg["unverified"] = unverified

    # LLM-Rollen: nur bekannte Anbieter, immer eine Liste.
    incoming_llm = raw.get("llm_sources")
    if incoming_llm is None:
        llm_names = list(DEFAULT_LLM_SOURCES)
    elif isinstance(incoming_llm, str):
        llm_names = [incoming_llm]
    elif isinstance(incoming_llm, (list, tuple, set)):
        llm_names = [str(n).strip() for n in incoming_llm if str(n or "").strip()]
    else:
        warnings.append("llm_sources war kein Text/keine Liste - Vorgabe gesetzt")
        llm_names = list(DEFAULT_LLM_SOURCES)
    unknown_llm = [n for n in llm_names if n not in KNOWN_PROVIDERS]
    for n in unknown_llm:
        warnings.append(f"LLM-Quelle '{n}' unbekannt - verworfen")
    cfg["llm_sources"] = [n for n in llm_names if n in KNOWN_PROVIDERS] or list(DEFAULT_LLM_SOURCES)

    incoming: dict[str, dict[str, Any]] = {}
    for item in raw.get("sources") or []:
        if isinstance(item, dict) and item.get("name"):
            incoming[str(item["name"])] = item
        elif isinstance(item, str):
            incoming[item] = {"name": item}

    for name in list(incoming):
        if name not in KNOWN_PROVIDERS:
            warnings.append(f"unbekannter Anbieter '{name}' verworfen")
            incoming.pop(name)

    sources = []
    for name in KNOWN_PROVIDERS:  # stabile Reihenfolge, alle bekannten persistieren
        item = incoming.get(name, {})
        default_limit = _limits.default_limit_for(name)
        raw_limit = item.get("limit")
        if raw_limit is None or (isinstance(raw_limit, str) and not raw_limit.strip()):
            limit = default_limit
        else:
            limit = _limits.clamp_int(raw_limit, _limits.LIMIT_MIN, _limits.LIMIT_MAX, default_limit)
            if limit != _limits.clamp_int(raw_limit, 0, 10**6, default_limit):
                warnings.append(
                    f"{LABELS.get(name, name)}: Trefferzahl {raw_limit!r} auf {limit} geklemmt "
                    f"({_limits.LIMIT_MIN}..{_limits.LIMIT_MAX})"
                )
        sources.append(
            {
                "name": name,
                "label": LABELS.get(name, name),
                "weight": round(_clamp(item.get("weight"), 0.0, 2.0, 1.0), 2),
                "enabled": _bool(item.get("enabled"), name in DEFAULT_ENABLED),
                "limit": limit,
                "cost_class": _limits.cost_class(name),
                "default_limit": default_limit,
                "llm_role": name in cfg["llm_sources"],
            }
        )
    cfg["sources"] = sources
    return cfg, warnings


def _bool(value: Any, default: bool) -> bool:
    """Boolesch lesen, ohne `bool("false") == True`.

    Eine handgeschriebene oder von V1 uebernommene Datei kann "false" als Text
    tragen; `bool(...)` wuerde daraus eine Einschaltung machen und z. B.
    `wait_for_all` still wieder aktivieren — genau der Zustand, den V2 abstellt.
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


def _load(profile: str | None) -> tuple[dict[str, Any], list[str]]:
    path = _read_path(profile)
    if not path.is_file():
        return _validate(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _validate(DEFAULTS), [f"Config unlesbar ({type(exc).__name__}) - Defaults geladen"]
    notes: list[str] = []
    if path.name == LEGACY_CONFIG_NAME:
        # V1 -> V2 abbilden, BEVOR validiert wird: sonst uebernimmt `wait_for_all:
        # true` aus den V1-Vorgaben die V2-Semantik und hebt die Budgets auf.
        raw, notes = _limits.legacy_to_v2(raw)
    cfg, warnings = _validate(raw)
    return cfg, notes + warnings


def _atomic_write(path: Path, cfg: dict[str, Any]) -> None:
    """Temp-Datei + replace: ein Abbruch darf keine halbe Config hinterlassen.

    `/reset` schrieb in V1 direkt auf den Zielpfad — bei einem Abbruch mitten im
    Schreiben stand dort eine abgeschnittene Datei, die der Suchpfad als
    "unlesbar" verwarf und still auf Defaults lief.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


@router.get("/state")
async def state(profile: str | None = None) -> dict[str, Any]:
    cfg, warnings = _load(profile)
    live = _available_providers()
    registry_error = live.pop("__registry_error__", None)
    # Sichtbar machen, wer eine Quelle wirklich bedient: die Konfigurationsquelle
    # `searxng` laeuft ueber den paginierenden Provider des Plugins (siehe
    # limits.SOURCE_ALIASES), sofern der registriert ist. Ohne diese Auskunft
    # muesste der Anwender raten, ob die Seiten-Schleife greift.
    paged_ok = False
    try:
        import agent.web_search_registry as _wsr

        paged_ok = _wsr.get_provider(_limits.SOURCE_ALIASES["searxng"]) is not None
    except Exception:  # noqa: BLE001
        paged_ok = False
    for src in cfg["sources"]:
        info = live.get(src["name"]) or {}
        src["available"] = info.get("available")
        src["served_by"] = _limits.SOURCE_ALIASES.get(src["name"], src["name"]) if paged_ok else src["name"]
    read_path = _read_path(profile)
    write_path = _config_path(profile)
    return {
        "plugin": PLUGIN_ID,
        "profile": profile or "default",
        "config": cfg,
        "defaults": _validate(DEFAULTS)[0],
        "config_path": str(write_path),
        "config_path_read": str(read_path),
        "migrated_from": str(read_path) if read_path.name == LEGACY_CONFIG_NAME else None,
        "known_providers": KNOWN_PROVIDERS,
        "registry": {"error": registry_error.get("error") if registry_error else None, "reported": list(live)},
        "warnings": warnings,
        "limits": _limits.limits_payload(),
        "paged_searxng": paged_ok,
        "cost": {
            "free_sources": list(_limits.FREE_SOURCES),
            "free_default_limit": _limits.FREE_DEFAULT_LIMIT,
            "paid_default_limit": _limits.PAID_DEFAULT_LIMIT,
        },
        "recommendation": {
            "max_parallel_index_sources": 3,
            "note": "Gemessen 03-10-2026 (2 Queries, 4 Anbieter): 1 Quelle deckt ~40 %, "
            "2 Quellen ~70 %, 3 Quellen 100 % der gefundenen URLs ab. Darueber bringt "
            "eine weitere Index-Quelle kaum Neues, kostet aber Credits und Latenz. "
            "xAI ist eine LLM-Quelle und laeuft als Nachtrag.",
            "measuring_run": "cache/scratch/fanout_eval.py",
        },
    }


@router.post("/config")
async def save_config(body: dict, profile: str | None = None) -> dict[str, Any]:
    profile = body.get("profile") or profile
    cfg, warnings = _validate(body.get("config"))
    path = _config_path(profile)
    _atomic_write(path, cfg)
    legacy = _legacy_config_path(profile)
    if legacy.is_file():
        warnings = list(warnings) + [
            f"geschrieben nach {path.name}; die alte {legacy.name} bleibt unangetastet "
            "(sie wird ab jetzt nicht mehr gelesen)"
        ]
    return {
        "ok": True,
        "config": cfg,
        "path": str(path),
        "config_path_read": str(_read_path(profile)),
        "warnings": warnings,
    }


@router.post("/reset")
async def reset(body: dict | None = None, profile: str | None = None) -> dict[str, Any]:
    profile = (body or {}).get("profile") or profile
    cfg, warnings = _validate(DEFAULTS)
    path = _config_path(profile)
    _atomic_write(path, cfg)
    return {
        "ok": True,
        "config": cfg,
        "path": str(path),
        "config_path_read": str(_read_path(profile)),
        "warnings": warnings,
    }
