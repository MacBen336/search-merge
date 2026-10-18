# search-merge

A backend plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/docs) that replaces the
plain web search call with a **fan-out across several providers**:

- queries the configured providers in parallel,
- merges the results (dedupe + rank fusion),
- applies per-domain weighting,
- keeps the LLM-backed source as a *labelled addendum* instead of mixing it into the index results,
- skips slow sources for the turn instead of waiting for them.

Hits per provider are configurable, which is the cost control knob for paid sources.

## Registered providers

| Provider id | What it is |
|---|---|
| `search-merge` | the merge backend |
| `search-fanout` | legacy alias for the same code, so profiles that still name the old id keep working |
| `searxng-paged` | SearXNG with a page loop (the bundled `searxng` provider pins `pageno` to 1) |

Point a profile at it with `web.search_backend: search-merge`.

## Configuration

Per profile, next to that profile's config:

```
<HERMES_HOME>/search-merge.json          # named profiles: <HERMES_HOME>/profiles/<slug>/search-merge.json
```

If `search-merge.json` does not exist, `search-fanout.json` is read instead. The response of the
API reports which file was found and which was used.

| Key | Meaning |
|---|---|
| `sources[]` | one entry per provider: `name`, `role` (`index` or `llm`), `limit`, `weight` |
| `fanout_limit` | maximum number of providers queried per turn |
| `source_limit.free_default` / `.paid_default` | default hits for free and paid sources |
| `deadline_s` | budget for the whole fan-out |
| `llm_budget_s` | budget for the LLM-backed source |
| `hard_timeout_s` | hard cut-off |
| `verify_budget_s` | budget for verification work |
| `max_added` | upper bound of results added by the merge |
| `weight` | range allowed for per-domain weighting |

## Requirements

- Hermes Agent with a working web search toolset — the merge calls the search providers that are
  already registered in the same installation.
- For the paginated SearXNG source, a reachable SearXNG instance (`SEARXNG_URL`).

## Install

```
<HERMES_HOME>/plugins/search-merge/
```

Then restart the gateway and the dashboard. The configuration tab appears after *Skills*.

## Backend routes

Namespace `/api/plugins/search-merge/`:

| Route | Purpose |
|---|---|
| `GET /state` | current configuration plus the provider inventory |
| `POST /config` | validate and save the configuration |
| `POST /reset` | restore defaults |

## Status

Version 0.2.0. The merge backend itself writes nothing outside its own configuration file.
