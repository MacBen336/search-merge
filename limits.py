"""Grenzwerte und Kostenklassen — EINZIGE Quelle fuer beide Seiten.

Warum eine eigene Datei: Die Zahlen galten einmal in `provider.py` (Suchpfad,
Gateway-Prozess) und einmal in `dashboard/plugin_api.py` (Konfigurations-API,
Plugin-Host-Subprozess). Zwei Fundstellen bedeuten: sie laufen bei der naechsten
Aenderung auseinander, und die Oberflaeche zeigt dann Werte an, die der
Suchpfad nie anwendet.

Warum trotzdem zweimal geladen: Die beiden Dateien werden von Hermes an
unterschiedlichen Wegen importiert. `provider.py` kommt als Paketmodul
(`hermes_plugins.<slug>.provider`), ein relatives `from .limits import` waere
korrekt — aber `dashboard/plugin_api.py` laedt Hermes per
`spec_from_file_location("hermes_dashboard_plugin_<name>", ...)` OHNE
`submodule_search_locations`; ein relativer Import daraus schlaegt mit
ImportError fehl (gemessen 03-10-2026 mit exakt diesem Ladeweg nachgebaut).
Beide Seiten laden deshalb dieselbe Datei ueber einen Pfad-Loader mit einem
stabilen Modulnamen — dadurch gibt es im jeweiligen Prozess genau eine Kopie,
und die Zahlen stehen nur hier. Der Modulname ist pro Plugin eindeutig
(`search_merge_limits_shared`): zwei Plugins mit demselben Namen wuerden sich
sonst ueber `sys.modules` gegenseitig ihre Grenzwerte unterschieben.

Konfigurierbarkeit bleibt Sache des Nutzers: Diese Werte sind die Grenzen der
Regler, nicht ihre Vorgaben.
"""

from __future__ import annotations

from typing import Any

# Obergrenze des Aufruferwerts. Entspricht dem maximum im Tool-Schema von
# `web_search` (tools/web_tools.py) — der Kern wuerde darueber selbst klemmen,
# dann aber auf seinen Wert zurueckfallen und der Aufrufer glaubte, es sei
# das gewuenschte Ergebnis gewesen. Deshalb als Konstante hier, nicht im Kern.
KERN_MAX = 100

# Untergrenze des Aufruferwerts: unterhalb davon lohnt das Zusammenfuehren
# mehrerer Quellen nicht, der Aufrufer sieht dann eine Liste, die schneller
# gefunden waere als eine einzelne Quelle.
FANOUT_MIN = 10

# Trefferzahl JE QUELLE (V2 ersetzt das globale `provider_limit: 100`).
# Die Zahl ist eine Kostengrenze, kein Versprechen: eine Quelle darf weniger
# liefern als der Aufrufer anfragt. Der wirksame Wert und die gelieferte Menge
# stehen beide in `meta.sources.<name>`.
LIMIT_MIN = 1
LIMIT_MAX = KERN_MAX

# Kostenklassen. Kostenlose/self-hosted Quellen duerfen breit holen, bezahlte
# und LLM-Quellen starten klein — sonst laeuft jede bezahlte Suche mit
# demselben Deckel wie die eigene Gratis-Instanz, und Kosten/Quota sind nicht
# kontrollierbar.
FREE_SOURCES = ("searxng", "searxng-paged", "ddgs", "brave_free")
FREE_DEFAULT_LIMIT = 20
PAID_DEFAULT_LIMIT = 5

#: Quellen-Aliase: welcher registrierte Provider eine Konfigurationsquelle
#: tatsaechlich bedient. Grund fuer die Indirektion: der gebuendelte
#: SearXNG-Provider holt genau eine Seite (``pageno`` hart 1), der paginierende
#: Provider liegt update-fest im Plugin (``paged_searxng.py``) und traegt einen
#: eigenen Namen. Die Zuordnung wird NICHT versteckt: sie steht je Quelle als
#: ``resolved_as`` in ``meta``. Ist das Ziel nicht registriert, bedient der
#: gebuendelte Provider die Quelle unveraendert.
SOURCE_ALIASES: dict[str, str] = {"searxng": "searxng-paged"}

# Fallback fuer Quellen ohne eigenes `limit` und ohne Kostenklasse.
UNKNOWN_DEFAULT_LIMIT = PAID_DEFAULT_LIMIT

# Zeitgrenzen (Konfigurierbar ueber search-merge.json).
# Zwei Budgets statt einer Deadline: der LLM-Nachtrag darf etwas laenger
# brauchen als die Index-Quellen, aber nicht beliebig. `hard_timeout_s` ist der
# absolute Deckel — wer ihn reisst, wird fuer diesen Turn ignoriert.
DEADLINE_MIN_S = 1
DEADLINE_MAX_S = 300
LLM_BUDGET_MIN_S = 1
LLM_BUDGET_MAX_S = 300
HARD_TIMEOUT_MIN_S = 5
HARD_TIMEOUT_MAX_S = 300

# Deckel fuer den LLM-Nachtrag (in addition zum Aufruferwert).
MAX_ADDED_MIN = 0
MAX_ADDED_MAX = 50

# Budget der Abrufpruefung fuer LLM-Quellen (Sekunden).
VERIFY_BUDGET_MIN_S = 1
VERIFY_BUDGET_MAX_S = 60

WEIGHT_MIN = 0.0
WEIGHT_MAX = 2.0


def cost_class(name: object) -> str:
    """'free' fuer kostenlose/self-hosted Quellen, sonst 'paid'.

    Unbekannte Namen sind bewusst 'paid': eine neue, noch nicht eingeordnete
    Quelle wird lieber zu knapp dosiert und sichtbar zu knapp gemeldet als
    still mit Gratis-Breite auf Kosten des Nutzers abgefragt.
    """
    return "free" if str(name or "").strip().lower() in FREE_SOURCES else "paid"


def default_limit_for(name: object) -> int:
    """Vorgabe der Trefferzahl je Quelle aus ihrer Kostenklasse."""
    return FREE_DEFAULT_LIMIT if cost_class(name) == "free" else PAID_DEFAULT_LIMIT


def limits_payload() -> dict[str, dict[str, Any]]:
    """Die Grenzen als Daten — fuer GET /state (die Oberflaeche braucht sie,
    um Eingabefelder zu begrenzen, statt hartkodierte Zahlen zu zeigen).

    Form: je Regler ein Objekt mit `min`/`max` (+ Vorgaben wo es sie gibt).
    Bewusst KEINE Liste [min, max] mehr — das Frontend liest die Grenzen als
    Objekt; die Min/Max-Form ist zusaetzlich selbstbeschreibend, was bei acht
    Reglern den Unterschied zwischen Ober- und Untergrenze sichtbar macht.
    """
    return {
        "fanout_limit": {"min": FANOUT_MIN, "max": KERN_MAX},
        "source_limit": {
            "min": LIMIT_MIN,
            "max": LIMIT_MAX,
            "free_default": FREE_DEFAULT_LIMIT,
            "paid_default": PAID_DEFAULT_LIMIT,
        },
        "deadline_s": {"min": DEADLINE_MIN_S, "max": DEADLINE_MAX_S},
        "llm_budget_s": {"min": LLM_BUDGET_MIN_S, "max": LLM_BUDGET_MAX_S},
        "hard_timeout_s": {"min": HARD_TIMEOUT_MIN_S, "max": HARD_TIMEOUT_MAX_S},
        "verify_budget_s": {"min": VERIFY_BUDGET_MIN_S, "max": VERIFY_BUDGET_MAX_S},
        "max_added": {"min": MAX_ADDED_MIN, "max": MAX_ADDED_MAX},
        "weight": {"min": WEIGHT_MIN, "max": WEIGHT_MAX},
    }


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """Ganzzahl auf [low, high] klemmen; unbrauchbare Eingabe -> default.

    Bewusst tolerant: None, "", True, 3.7, "zwanzig" und Negative fallen auf
    `default` zurueck, statt eine Ausnahme in den Aufrufer zu tragen. Die
    wirksamen Werte werden in `meta` ausgewiesen — eine stille Klemme ist sonst
    nicht von einer gewollten Einstellung zu unterscheiden.
    """
    try:
        if value is None or isinstance(value, bool):
            return int(default)
        n = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    return max(int(low), min(int(high), n))


def clamp_float(value: Any, low: float, high: float, default: float) -> float:
    """Fließkommazahl auf [low, high] klemmen; unbrauchbare Eingabe -> default."""
    try:
        if value is None or isinstance(value, bool):
            return float(default)
        n = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(float(low), min(float(high), n))


# --- Migration einer V1-Konfiguration (search-fanout.json) -> V2 -----------------
#
# Warum das hier und nicht im Suchpfad: Die Abbildung braucht BEIDE Seiten — der
# Provider liest die alte Datei bei jeder Suche, die Dashboard-API beim Laden
# (und schreibt sie beim ersten Speichern als search-merge.json). Laege sie nur
# auf einer Seite, haetten Oberflaeche und Suchpfad verschiedene Werte im Kopf.
#
# Warum ueberhaupt abbilden: `wait_for_all` und `hard_timeout_s` bedeuten in V2
# das Gegenteil von V1. V1-Vorgabe war "auf alle Quellen warten, Deckel 120 s"
# (die xAI-Suche braucht gemessen 12,5-31 s und wurde abgewartet). Wird der
# Altwert unveraendert uebernommen, ist die neue Regel "wer sein Budget reisst,
# wird fuer diesen Turn ignoriert" stillschweigend wieder abgeschaltet — die
# Einstellung in der Oberflaeche waere gelogen. Also: Altwerte, die genau den
# V1-Vorgaben entsprechen, auf die V2-Vorgabe abbilden und das benennen.

#: Die V1-Vorgaben, an denen ein Altwert als "nie bewusst gesetzt" erkennbar ist.
LEGACY_V1_WAIT_FOR_ALL = True
LEGACY_V1_HARD_TIMEOUT_S = 120.0

#: V2-Vorgaben, auf die abgebildet wird (Muessen mit plugin_api.DEFAULTS passen).
V2_DEFAULT_LLM_BUDGET_S = 20.0
V2_DEFAULT_HARD_TIMEOUT_S = 30.0


def legacy_to_v2(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Eine V1-Konfiguration auf V2-Semantik abbilden -> ``(config, hinweise)``.

    Nur fuer Dateien, die aus ``search-fanout.json`` stammen. Eine bereits
    gespeicherte ``search-merge.json`` wird NICHT durch diese Funktion gefuehrt:
    dort ist ``wait_for_all: true`` eine bewusste Nutzerentscheidung und muss
    erhalten bleiben.
    """
    cfg = dict(raw)
    notes: list[str] = []

    if cfg.get("wait_for_all") is True:
        cfg["wait_for_all"] = False
        notes.append(
            "wait_for_all von true auf false abgebildet: in V1 hiess das 'auf alle Quellen warten bis 120 s', "
            "in V2 wuerde es die Budgets aufheben (Altwert stammt aus der V1-Vorgabe)"
        )
    if "hard_timeout_s" in cfg:
        try:
            legacy_hard = float(cfg["hard_timeout_s"])
        except (TypeError, ValueError):
            legacy_hard = None
        if legacy_hard is not None and legacy_hard >= LEGACY_V1_HARD_TIMEOUT_S:
            cfg["hard_timeout_s"] = V2_DEFAULT_HARD_TIMEOUT_S
            notes.append(
                f"hard_timeout_s von {legacy_hard:g} s auf {V2_DEFAULT_HARD_TIMEOUT_S:g} s abgebildet (V1-Vorgabe)"
            )
    if "llm_budget_s" not in cfg:
        cfg["llm_budget_s"] = V2_DEFAULT_LLM_BUDGET_S
        notes.append(f"llm_budget_s neu auf {V2_DEFAULT_LLM_BUDGET_S:g} s gesetzt (Feld gab es in V1 nicht)")
    if "provider_limit" in cfg:
        notes.append(
            "provider_limit wird nicht mehr angewandt: die Trefferzahl gilt jetzt je Quelle "
            "(sources[].limit), Vorgabe aus der Kostenklasse"
        )
    return cfg, notes

