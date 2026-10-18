/**
 * Search-Merge — Web-Dashboard-Oberflaeche (Dashboard-Plugin-Haelfte).
 *
 * Bedient /api/plugins/search-merge/* (siehe ../plugin_api.py) und laeuft
 * damit identisch zur Desktop-Haelfte (../../desktop/plugin.js).
 *
 * Reines IIFE-Bundle, kein Build: window.__HERMES_PLUGIN_SDK__ liefert React,
 * Hooks und die UI-Bausteine. Kein Slider im SDK -> nativer range-Regler, der
 * ueber accent-color die Theme-Farbe erbt (keine hardcodierten Farben).
 *
 * V2 gegenueber V1: Trefferzahl JE ANBIETER (Kostenkontrolle), zwei Budgets
 * statt einer Deadline, und die Anzeige, welche Konfigurationsdatei wirklich
 * gelesen wird (Migrationspfad search-fanout.json -> search-merge.json).
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) {
    console.error("[search-merge] Plugin-SDK nicht gefunden");
    return;
  }

  var React = SDK.React;
  var h = React.createElement;
  var H = SDK.hooks;
  var C = SDK.components;

  var PLUGIN_ID = "search-merge";
  var BASE = "/api/plugins/" + PLUGIN_ID;
  var ACCENT = "var(--primary, var(--ui-accent, currentColor))";

  // Das Dashboard fuehrt das aktive Profil als ?profile=<name> in der URL.
  function activeProfile() {
    try {
      return new URLSearchParams(window.location.search).get("profile") || "";
    } catch (err) {
      return "";
    }
  }

  function endpoint(path) {
    var profile = activeProfile();
    return BASE + path + (profile ? "?profile=" + encodeURIComponent(profile) : "");
  }

  function postJSON(path, payload) {
    return SDK.fetchJSON(endpoint(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }

  function errText(err) {
    if (!err) return "unbekannter Fehler";
    return err.message ? err.message : String(err);
  }

  // ---------------------------------------------------------------- Bausteine

  function Range(props) {
    return h("input", {
      type: "range",
      min: props.min,
      max: props.max,
      step: props.step,
      value: props.value,
      disabled: !!props.disabled,
      "aria-label": props.label,
      "aria-valuetext": props.text,
      onChange: function (e) {
        props.onChange(parseFloat(e.target.value));
      },
      style: { width: "100%", accentColor: ACCENT },
    });
  }

  // Trefferzahl je Quelle als Regler (Kostengrenze 1..100). Zwei Dinge muss die
  // Anzeige leisten: die aktuelle Zahl muss sichtbar mitlaufen (sonst hat man
  // beim Ziehen keine Rueckmeldung), und ein leerer/fehlender Wert muss auf die
  // Vorgabe der Kostenklasse fallen — ein Regler kann keinen leeren Zustand
  // darstellen, ein Zahlenfeld konnte es.
  function sourceLimitValue(s, cost) {
    var fallback = s && s.default_limit != null ? s.default_limit : ((cost && cost.free_default_limit) || 20);
    var n = s ? parseFloat(s.limit) : NaN;
    return isNaN(n) ? fallback : n;
  }

  function Toggle(props) {
    return h(
      "label",
      { className: "flex items-center gap-2 text-sm" },
      h("input", {
        type: "checkbox",
        checked: !!props.checked,
        disabled: !!props.disabled,
        onChange: function (e) {
          props.onChange(e.target.checked);
        },
        style: { accentColor: ACCENT },
      }),
      h("span", null, props.label)
    );
  }

  function Field(props) {
    return h(
      "div",
      { className: "flex flex-col gap-1" },
      h("div", { className: "text-xs text-muted-foreground" }, props.label),
      props.hint ? h("div", { className: "text-xs text-muted-foreground opacity-70" }, props.hint) : null,
      props.children
    );
  }

  function AvailabilityBadge(props) {
    if (props.available === true) return h(C.Badge, { variant: "secondary" }, "verfuegbar");
    if (props.available === false) return h(C.Badge, { variant: "outline" }, "nicht verfuegbar");
    return h(C.Badge, { variant: "outline" }, "Status unbekannt");
  }

  function CostBadge(props) {
    if (props.cost === "free") return h(C.Badge, { variant: "secondary" }, "kostenlos");
    return h(C.Badge, { variant: "outline" }, "bezahlt");
  }

  // ------------------------------------------------------------------- Seite

  function SearchMergePage() {
    var stateHook = H.useState(null);
    var state = stateHook[0];
    var setState = stateHook[1];

    var draftHook = H.useState(null);
    var draft = draftHook[0];
    var setDraft = draftHook[1];

    var statusHook = H.useState({ tone: "idle", text: "lade ..." });
    var status = statusHook[0];
    var setStatus = statusHook[1];

    var warnHook = H.useState([]);
    var warnings = warnHook[0];
    var setWarnings = warnHook[1];

    var addHook = H.useState("");
    var toAdd = addHook[0];
    var setToAdd = addHook[1];

    var busyHook = H.useState(false);
    var busy = busyHook[0];
    var setBusy = busyHook[1];

    var load = H.useCallback(function () {
      setStatus({ tone: "idle", text: "lade ..." });
      SDK.fetchJSON(endpoint("/state"))
        .then(function (data) {
          setState(data);
          setDraft(data.config);
          setWarnings(data.warnings || []);
          setStatus({ tone: "ok", text: "geladen — " + (data.config_path_read || data.config_path) });
        })
        .catch(function (err) {
          setStatus({ tone: "err", text: "Laden fehlgeschlagen: " + errText(err) });
        });
    }, []);

    H.useEffect(function () {
      load();
    }, [load]);

    function patchSource(name, patch) {
      setDraft(function (cur) {
        if (!cur) return cur;
        var next = Object.assign({}, cur);
        next.sources = (cur.sources || []).map(function (s) {
          return s.name === name ? Object.assign({}, s, patch) : s;
        });
        return next;
      });
    }

    function patchConfig(patch) {
      setDraft(function (cur) {
        return cur ? Object.assign({}, cur, patch) : cur;
      });
    }

    function save() {
      if (!draft) return;
      setBusy(true);
      setStatus({ tone: "idle", text: "speichere ..." });
      postJSON("/config", { profile: activeProfile() || undefined, config: draft })
        .then(function (res) {
          setDraft(res.config);
          setWarnings(res.warnings || []);
          setStatus({ tone: "ok", text: "gespeichert — " + res.path });
        })
        .catch(function (err) {
          setStatus({ tone: "err", text: "Speichern fehlgeschlagen: " + errText(err) });
        })
        .then(function () {
          setBusy(false);
        });
    }

    function reset() {
      setBusy(true);
      postJSON("/reset", { profile: activeProfile() || undefined })
        .then(function (res) {
          setDraft(res.config);
          setWarnings(res.warnings || []);
          setStatus({ tone: "ok", text: "auf Defaults zurueckgesetzt — " + res.path });
        })
        .catch(function (err) {
          setStatus({ tone: "err", text: "Zuruecksetzen fehlgeschlagen: " + errText(err) });
        })
        .then(function () {
          setBusy(false);
        });
    }

    if (!draft) {
      return h(
        C.Card,
        null,
        h(C.CardHeader, null, h(C.CardTitle, null, "Search-Merge")),
        h(C.CardContent, null, h("div", { className: "text-sm text-muted-foreground" }, status.text))
      );
    }

    var sources = draft.sources || [];
    var active = sources.filter(function (s) {
      return s.enabled;
    });
    var inactive = sources.filter(function (s) {
      return !s.enabled;
    });
    var rec = (state && state.recommendation) || null;
    var recMax = rec ? rec.max_parallel_index_sources : 3;
    var cost = (state && state.cost) || {};
    var lim = (state && state.limits) || {};
    var srcLimits = lim.source_limit || { min: 1, max: 100 };
    var llmSources = active.filter(function (s) {
      return s.llm_role || (draft.llm_sources || []).indexOf(s.name) !== -1;
    }).length;
    var indexActive = active.length - llmSources;

    return h(
      C.Card,
      null,
      h(
        C.CardHeader,
        null,
        h(
          "div",
          { className: "flex items-center justify-between gap-3" },
          h(C.CardTitle, null, "Search-Merge"),
          h(
            "div",
            { className: "flex items-center gap-2" },
            h(C.Badge, { variant: "outline" }, "Profil: " + ((state && state.profile) || "default")),
            h(C.Badge, { variant: "secondary" }, active.length + " aktiv")
          )
        )
      ),
      h(
        C.CardContent,
        { className: "flex flex-col gap-4" },

        // --- Migrationshinweis -------------------------------------------
        state && state.migrated_from
          ? h(
              "div",
              { className: "rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-xs" },
              "Einstellungen kommen noch aus " +
                state.migrated_from +
                ". Ab dem ersten Speichern gilt " +
                state.config_path +
                " (die alte Datei bleibt liegen)."
            )
          : null,

        // --- Aktive Anbieter + "+" zum Hinzufuegen -----------------------
        h(
          "div",
          { className: "flex flex-col gap-2" },
          h(
            "div",
            { className: "flex items-center justify-between gap-2" },
            h("div", { className: "text-xs text-muted-foreground" }, "Im Merge aktiv (" + active.length + ")"),
            indexActive > recMax
              ? h(C.Badge, { variant: "outline" }, "ueber Empfehlung (" + recMax + " Index-Quellen)")
              : null
          ),

          h(
            "div",
            { className: "text-xs text-muted-foreground" },
            "Trefferzahl je Anbieter = Kostengrenze. Vorgabe aus der Kostenklasse: kostenlos " +
              (cost.free_default_limit != null ? cost.free_default_limit : 20) +
              ", bezahlt " +
              (cost.paid_default_limit != null ? cost.paid_default_limit : 5) +
              "."
          ),

          active.length === 0
            ? h("div", { className: "text-sm text-muted-foreground" }, "kein Anbieter aktiv — unten hinzufuegen")
            : active.map(function (s) {
                return h(
                  "div",
                  {
                    key: s.name,
                    className: "flex flex-col gap-2 rounded-md border p-3",
                  },
                  h(
                    "div",
                    { className: "flex items-center justify-between gap-2" },
                    h(
                      "div",
                      { className: "flex items-center gap-2" },
                      h("div", { className: "text-sm font-medium" }, s.label),
                      h(CostBadge, { cost: s.cost_class }),
                      s.llm_role ? h(C.Badge, { variant: "outline" }, "LLM-Nachtrag") : null,
                      s.served_by && s.served_by !== s.name
                        ? h(C.Badge, { variant: "outline" }, "holt Seiten nach")
                        : null
                    ),
                    h(
                      "div",
                      { className: "flex items-center gap-2" },
                      h(AvailabilityBadge, { available: s.available }),
                      h(
                        C.Button,
                        {
                          variant: "ghost",
                          "aria-label": s.label + " entfernen",
                          title: "entfernen",
                          onClick: function () {
                            patchSource(s.name, { enabled: false });
                          },
                        },
                        "\u2212"
                      )
                    )
                  ),
                  h(
                    "div",
                    { className: "flex flex-col gap-3" },
                    h(
                      Field,
                      {
                        label: "Trefferzahl: " + sourceLimitValue(s, cost),
                        hint:
                          (s.cost_class === "free" ? "kostenlos/self-hosted" : "bezahlt/LLM — klein halten") +
                          " · Vorgabe " + (s.default_limit != null ? s.default_limit : "—"),
                      },
                      h(Range, {
                        min: srcLimits.min,
                        max: srcLimits.max,
                        step: 1,
                        value: sourceLimitValue(s, cost),
                        label: s.label + " Trefferzahl",
                        text: String(sourceLimitValue(s, cost)),
                        onChange: function (v) {
                          patchSource(s.name, { limit: Math.round(v) });
                        },
                      })
                    ),
                    h(
                      "div",
                      { className: "flex-1" },
                      h(
                        Field,
                        { label: "Gewichtung: " + Number(s.weight).toFixed(1) },
                        h(Range, {
                          min: 0,
                          max: 2,
                          step: 0.1,
                          value: s.weight,
                          label: s.label + " Gewichtung",
                          text: Number(s.weight).toFixed(1),
                          onChange: function (v) {
                            patchSource(s.name, { weight: v });
                          },
                        })
                      )
                    )
                  )
                );
              }),

          inactive.length
            ? h(
                "div",
                { className: "flex items-end gap-2" },
                h(
                  "div",
                  { className: "flex-1" },
                  h(
                    Field,
                    { label: "Anbieter hinzufuegen (aus Hermes)" },
                    h(
                      C.Select,
                      {
                        value: toAdd,
                        onValueChange: function (v) {
                          setToAdd(v);
                        },
                      },
                      inactive.map(function (s) {
                        return h(C.SelectOption, { key: s.name, value: s.name }, s.label);
                      })
                    )
                  )
                ),
                h(
                  C.Button,
                  {
                    "aria-label": "Anbieter hinzufuegen",
                    title: "hinzufuegen",
                    disabled: !toAdd,
                    onClick: function () {
                      if (!toAdd) return;
                      patchSource(toAdd, { enabled: true });
                      setToAdd("");
                    },
                  },
                  "+"
                )
              )
            : null,

          rec && rec.note ? h("div", { className: "text-xs text-muted-foreground" }, rec.note) : null
        ),

        h(C.Separator, null),

        // --- Budgets -----------------------------------------------------
        h(
          "div",
          { className: "grid gap-3 sm:grid-cols-2" },
          h(
            Field,
            {
              label: "Budget Index-Quellen: " + Number(draft.deadline_s).toFixed(0) + " s",
              hint: "danach wird eine noch laufende Index-Quelle fuer diesen Turn ignoriert",
            },
            h(Range, {
              min: 1,
              max: 120,
              step: 1,
              value: draft.deadline_s,
              label: "Budget Index-Quellen in Sekunden",
              text: String(draft.deadline_s) + " Sekunden",
              onChange: function (v) {
                patchConfig({ deadline_s: v });
              },
            })
          ),
          h(
            Field,
            {
              label: "Budget LLM-Nachtrag: " + Number(draft.llm_budget_s).toFixed(0) + " s",
              hint: "xAI-Suche gemessen 12,5-31 s — wer es reisst, fehlt in diesem Turn",
            },
            h(Range, {
              min: 1,
              max: 120,
              step: 1,
              value: draft.llm_budget_s,
              label: "Budget LLM-Nachtrag in Sekunden",
              text: String(draft.llm_budget_s) + " Sekunden",
              onChange: function (v) {
                patchConfig({ llm_budget_s: v });
              },
            })
          ),
          h(
            Field,
            {
              label: "Deckel gesamt: " + Number(draft.hard_timeout_s).toFixed(0) + " s",
              hint: "Obergrenze ueber beiden Budgets (V1 stand auf 120 s)",
            },
            h(Range, {
              min: 5,
              max: 300,
              step: 5,
              value: draft.hard_timeout_s,
              label: "Deckel gesamt in Sekunden",
              text: String(draft.hard_timeout_s) + " Sekunden",
              onChange: function (v) {
                patchConfig({ hard_timeout_s: v });
              },
            })
          ),
          h(
            Field,
            {
              label: "max. Zusatzquellen: " + Number(draft.max_added).toFixed(0),
              hint:
                "Obergrenze fuer zusaetzliche Treffer aus der LLM-Zweitmeinung, die zusaetzlich zu den " +
                "Index-Treffern angehaengt werden (Gesamtliste hoechstens Limit + Zusatzquellen). " +
                "0 = keine. Wirkt nur bei aktivem LLM-Nachtrag; die Trefferzahl der Index-Quellen aendert das nicht.",
            },
            h(Range, {
              min: 0,
              max: 50,
              step: 1,
              value: draft.max_added,
              label: "maximale Zusatzquellen",
              text: String(draft.max_added),
              onChange: function (v) {
                patchConfig({ max_added: v });
              },
            })
          ),
          h(Toggle, {
            label: "auf den LLM-Nachtrag warten",
            checked: !!draft.llm_wait,
            onChange: function (v) {
              patchConfig({ llm_wait: v });
            },
          }),
          h(Toggle, {
            label: "auf ALLE Quellen warten (Alt-Schalter, hebt die Budgets auf)",
            checked: !!draft.wait_for_all,
            onChange: function (v) {
              patchConfig({ wait_for_all: v });
            },
          }),
          h(Toggle, {
            label: "LLM-Nachtrag anhaengen (" + (draft.llm_position === "meta_only" ? "nur in meta" : "an die Trefferliste") + ")",
            checked: draft.llm_position !== "meta_only",
            onChange: function (v) {
              patchConfig({ llm_position: v ? "append" : "meta_only" });
            },
          }),
          h(Toggle, {
            label: "LLM-Nachtrag einsammeln",
            checked: draft.llm_second_opinion,
            onChange: function (v) {
              patchConfig({ llm_second_opinion: v });
            },
          }),
          h(Toggle, {
            label: "LLM-URLs vor Aufnahme pruefen (Abruf)",
            checked: draft.verify_urls,
            onChange: function (v) {
              patchConfig({ verify_urls: v });
            },
          }),
          h(
            Field,
            { label: "unbestaetigte LLM-Quellen" },
            h(
              C.Select,
              {
                value: draft.unverified,
                onValueChange: function (v) {
                  patchConfig({ unverified: v });
                },
              },
              h(C.SelectOption, { value: "mark" }, "als unbestaetigt kennzeichnen"),
              h(C.SelectOption, { value: "drop" }, "verwerfen")
            )
          ),
          h(Toggle, {
            label: "Rerank (Domain-/Themengewichtung)",
            checked: draft.rerank_enabled,
            onChange: function (v) {
              patchConfig({ rerank_enabled: v });
            },
          })
        ),

        h(C.Separator, null),

        // --- Aktionen + Status -------------------------------------------
        h(
          "div",
          { className: "flex flex-wrap items-center gap-2" },
          h(C.Button, { onClick: save, disabled: busy }, busy ? "speichert ..." : "Speichern"),
          h(C.Button, { variant: "outline", onClick: reset, disabled: busy }, "Zuruecksetzen"),
          h(C.Button, { variant: "ghost", onClick: load, disabled: busy }, "Neu laden"),
          h(
            "div",
            { className: "text-xs " + (status.tone === "err" ? "text-destructive" : "text-muted-foreground") },
            status.text
          )
        ),

        h(
          "div",
          { className: "text-xs text-muted-foreground" },
          "Wirkt: " + ((state && state.config_path_read) || "—") + " · Geschrieben wird: " + ((state && state.config_path) || "—")
        ),

        warnings.length
          ? h(
              "ul",
              { className: "list-disc pl-5 text-xs text-amber-600" },
              warnings.map(function (w, i) {
                return h("li", { key: i }, w);
              })
            )
          : null
      )
    );
  }

  window.__HERMES_PLUGINS__.register(PLUGIN_ID, SearchMergePage);
})();
