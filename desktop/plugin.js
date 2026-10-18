/**
 * Search-Merge — Desktop-Oberflaeche (Native Hermes Desktop App).
 *
 * Gegenstueck zur Web-Haelfte (../../dashboard/dist/index.js); beide sprechen
 * denselben Backend-Namespace /api/plugins/search-merge/ (../../dashboard/plugin_api.py).
 *
 * Ablage: <App-Home>/desktop-plugins/search-merge/plugin.js
 *   -> Rein lokale Datei der App. Bei einem REMOTE-Backend (Gateway auf einem
 *      anderen Rechner) liegt diese Datei auf dem Rechner, auf dem die App
 *      laeuft — nicht auf dem Gateway-Host. Die Kopie in diesem Repo
 *      (../../desktop/plugin.js) ist der Quellstand; beide muessen gleich sein,
 *      sonst driftet die geladene Kopie gegen die Quelle.
 *
 * Plain ESM, uncompiled: nur '@hermes/plugin-sdk', 'react', 'react/jsx-runtime'
 * sind importierbar, es gibt kein JSX. Kein Slider im SDK -> nativer
 * range-Regler mit accentColor aus einer Theme-Variable.
 *
 * Wichtig: `ctx.rest(path)` ist per Konstruktion auf den Namespace der eigenen
 * Plugin-ID beschraenkt. Die `id` im default-Export MUSS daher 'search-merge'
 * sein — mit der alten ID wuerde jedes Laden/Speichern ins Leere laufen.
 */

import {
	Button,
	host,
	PANES_AREA,
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
	Separator,
	StatusDot,
	Switch,
	useValue,
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'search-merge'
const ACCENT = 'var(--ui-accent)'

// ctx wird in register() gesetzt; die Datenhooks brauchen es.
let CTX = null

function currentProfile() {
	try {
		const value = host.state.profile && host.state.profile.get ? host.state.profile.get() : null
		return value || ''
	} catch (err) {
		return ''
	}
}

function endpoint(path) {
	const profile = currentProfile()
	if (!profile || profile === 'default') return path
	return path + (path.indexOf('?') === -1 ? '?' : '&') + 'profile=' + encodeURIComponent(profile)
}

function errorText(err) {
	if (!err) return 'unbekannter Fehler'
	return err.message ? err.message : String(err)
}

// ------------------------------------------------------------------ Bausteine

function Range(props) {
	return jsx('input', {
		type: 'range',
		min: props.min,
		max: props.max,
		step: props.step,
		value: props.value,
		disabled: !!props.disabled,
		'aria-label': props.label,
		'aria-valuetext': props.text,
		onChange: (e) => props.onChange(parseFloat(e.target.value)),
		style: { width: '100%', accentColor: ACCENT }
	})
}

// Trefferzahl je Anbieter als Regler (Kostengrenze 1..100). Dieselbe Logik wie
// in der Web-Haelfte: der aktuelle Wert muss sichtbar mitlaufen, und ein leerer
// Wert faellt auf die Vorgabe der Kostenklasse (ein Regler kennt kein "leer").
function sourceLimitValue(s, cost) {
	const fallback = s && s.default_limit != null ? s.default_limit : (cost && cost.free_default_limit) || 20
	const n = s ? parseFloat(s.limit) : NaN
	return isNaN(n) ? fallback : n
}

function OptionRow(props) {
	return jsxs('div', {
		className: 'flex items-center justify-between gap-3 py-1',
		children: [
			jsxs('div', {
				className: 'flex flex-col',
				children: [
					jsx('span', { className: 'text-xs', children: props.label }),
					props.hint
						? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: props.hint })
						: null
				]
			}),
			props.children
		]
	})
}

// ---------------------------------------------------------------------- Pane

function SearchMergePane() {
	const profile = useValue(host.state.profile)
	const [state, setState] = useState(null)
	const [draft, setDraft] = useState(null)
	const [status, setStatus] = useState({ tone: 'idle', text: 'lade ...' })
	const [warnings, setWarnings] = useState([])
	const [toAdd, setToAdd] = useState('')
	const [busy, setBusy] = useState(false)

	useEffect(() => {
		let alive = true
		setStatus({ tone: 'idle', text: 'lade ...' })
		CTX.rest(endpoint('/state'))
			.then((data) => {
				if (!alive) return
				setState(data)
				setDraft(data.config)
				setWarnings(data.warnings || [])
				setStatus({ tone: 'ok', text: 'geladen' })
			})
			.catch((err) => {
				if (!alive) return
				setStatus({ tone: 'err', text: 'Laden fehlgeschlagen: ' + errorText(err) })
			})
		return () => {
			alive = false
		}
	}, [profile])

	function patchSource(name, patch) {
		setDraft((cur) => {
			if (!cur) return cur
			const next = Object.assign({}, cur)
			next.sources = (cur.sources || []).map((s) => (s.name === name ? Object.assign({}, s, patch) : s))
			return next
		})
	}

	function patchConfig(patch) {
		setDraft((cur) => (cur ? Object.assign({}, cur, patch) : cur))
	}

	function save() {
		if (!draft) return
		setBusy(true)
		setStatus({ tone: 'idle', text: 'speichere ...' })
		CTX.rest('/config', { method: 'POST', body: { profile: currentProfile(), config: draft } })
			.then((res) => {
				setDraft(res.config)
				setWarnings(res.warnings || [])
				setStatus({ tone: 'ok', text: 'gespeichert' })
			})
			.catch((err) => {
				setStatus({ tone: 'err', text: 'Speichern fehlgeschlagen: ' + errorText(err) })
			})
			.then(() => setBusy(false))
	}

	function reset() {
		setBusy(true)
		CTX.rest('/reset', { method: 'POST', body: { profile: currentProfile() } })
			.then((res) => {
				setDraft(res.config)
				setWarnings(res.warnings || [])
				setStatus({ tone: 'ok', text: 'auf Defaults zurueckgesetzt' })
			})
			.catch((err) => {
				setStatus({ tone: 'err', text: 'Zuruecksetzen fehlgeschlagen: ' + errorText(err) })
			})
			.then(() => setBusy(false))
	}

	if (!draft) {
		return jsxs('div', {
			className: 'flex h-full flex-col gap-2 p-3 text-sm',
			children: [
				jsx('div', { className: 'font-medium', children: 'Search-Merge' }),
				jsx('div', { className: 'text-(--ui-text-tertiary)', children: status.text })
			]
		})
	}

	const sources = draft.sources || []
	const active = sources.filter((s) => s.enabled)
	const inactive = sources.filter((s) => !s.enabled)
	const rec = state && state.recommendation ? state.recommendation : null
	const recMax = rec ? rec.max_parallel_index_sources : 3
	const cost = (state && state.cost) || {}
	const lim = (state && state.limits) || {}
	const srcLimits = lim.source_limit || { min: 1, max: 100 }
	const activeLlm = active.filter((s) => s.llm_role || (draft.llm_sources || []).indexOf(s.name) !== -1).length
	const activeIndex = active.length - activeLlm

	return jsxs('div', {
		className: 'flex h-full flex-col gap-3 overflow-auto p-3 text-sm',
		children: [
			jsxs('div', {
				className: 'flex items-center justify-between gap-2',
				children: [
					jsx('div', { className: 'font-medium', children: 'Search-Merge' }),
					jsxs('div', {
						className: 'flex items-center gap-2',
						children: [
							jsx(StatusDot, { tone: status.tone === 'err' ? 'red' : 'green' }),
							jsx('span', {
								className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
								children: (state && state.profile) || profile || 'default'
							}),
							jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: active.length + ' aktiv' })
						]
					})
				]
			}),

			// --- Migrationshinweis ---------------------------------------
			state && state.migrated_from
				? jsxs('div', {
						className: 'rounded-md border border-(--ui-stroke-secondary) p-2 text-[0.6875rem]',
						children: [
							'Einstellungen kommen noch aus ',
							jsx('span', { className: 'font-medium', children: state.migrated_from }),
							'. Ab dem ersten Speichern gilt ',
							jsx('span', { className: 'font-medium', children: state.config_path }),
							'.'
						]
				  })
				: null,

			// --- Aktive Anbieter + "+" zum Hinzufuegen --------------------
			jsxs('div', {
				className: 'flex flex-col gap-2',
				children: [
					jsxs('div', {
						className: 'flex items-center justify-between gap-2',
						children: [
							jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Im Merge aktiv (' + active.length + ')' }),
							activeIndex > recMax
								? jsx('span', {
										className: 'text-[0.6875rem] text-(--ui-text-secondary)',
										children: 'ueber Empfehlung (' + recMax + ')'
								  })
								: null
						]
					}),

					jsx('span', {
						className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
						children:
							'Trefferzahl je Anbieter = Kostengrenze. Vorgabe: kostenlos ' +
							(cost.free_default_limit != null ? cost.free_default_limit : 20) +
							', bezahlt ' +
							(cost.paid_default_limit != null ? cost.paid_default_limit : 5) +
							'.'
					}),

					active.length === 0
						? jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: 'kein Anbieter aktiv' })
						: active.map((s) =>
								jsxs(
									'div',
									{
										className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-2',
										children: [
											jsxs('div', {
												className: 'flex items-center justify-between gap-2',
												children: [
													jsxs('div', {
														className: 'flex items-center gap-2',
														children: [
															jsx('span', { className: 'text-xs font-medium', children: s.label }),
															jsx('span', {
																className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
																children: s.cost_class === 'free' ? 'kostenlos' : 'bezahlt'
															}),
															s.llm_role
																? jsx('span', {
																		className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
																		children: 'LLM-Nachtrag'
																  })
																: null,
															s.served_by && s.served_by !== s.name
																? jsx('span', {
																		className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
																		children: 'holt Seiten nach'
																  })
																: null
															]
													}),
													jsxs('div', {
														className: 'flex items-center gap-2',
														children: [
															jsx('span', {
																className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
																children:
																	s.available === true
																		? 'verfuegbar'
																		: s.available === false
																		? 'nicht verfuegbar'
																		: 'Status unbekannt'
															}),
															jsx(Button, {
																disabled: busy,
																title: 'entfernen',
																'aria-label': s.label + ' entfernen',
																onClick: () => patchSource(s.name, { enabled: false }),
																children: '\u2212'
															})
														]
													})
												]
											}),
											jsxs('div', {
												className: 'flex flex-col gap-3',
												children: [
													jsxs('div', {
														className: 'flex flex-col gap-1',
														children: [
															jsx('span', {
																className: 'text-xs text-(--ui-text-tertiary)',
																children: 'Trefferzahl: ' + sourceLimitValue(s, cost)
															}),
															jsx(Range, {
																min: srcLimits.min,
																max: srcLimits.max,
																step: 1,
																value: sourceLimitValue(s, cost),
																label: s.label + ' Trefferzahl',
																text: String(sourceLimitValue(s, cost)),
																onChange: (v) => patchSource(s.name, { limit: Math.round(v) })
															}),
															jsx('span', {
																className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
																children:
																	(s.cost_class === 'free' ? 'kostenlos/self-hosted' : 'bezahlt/LLM — klein halten') +
																	' · Vorgabe ' + (s.default_limit != null ? s.default_limit : '—')
															})
														]
													}),
													jsxs('div', {
														className: 'flex flex-1 flex-col gap-1',
														children: [
															jsx('span', {
																className: 'text-xs text-(--ui-text-tertiary)',
																children: 'Gewichtung: ' + Number(s.weight).toFixed(1)
															}),
															jsx(Range, {
																min: 0,
																max: 2,
																step: 0.1,
																value: s.weight,
																label: s.label + ' Gewichtung',
																text: Number(s.weight).toFixed(1),
																onChange: (v) => patchSource(s.name, { weight: v })
															})
														]
													})
												]
											})
										]
									},
									s.name
								)
						  ),

					inactive.length
						? jsxs('div', {
								className: 'flex items-end gap-2',
								children: [
									jsxs('div', {
										className: 'flex flex-1 flex-col gap-1',
										children: [
											jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Anbieter hinzufuegen' }),
											jsx(Select, {
												value: toAdd,
												onValueChange: setToAdd,
												children: jsxs(SelectTrigger, {
													className: 'w-full',
													children: [jsx(SelectValue, { placeholder: 'vorhandenen Anbieter waehlen' })]
												})
											}),
											jsx(SelectContent, {
												children: inactive.map((s) =>
													jsx(SelectItem, { value: s.name, children: s.label }, s.name)
												)
											})
										]
									}),
									jsx(Button, {
										disabled: !toAdd,
										title: 'hinzufuegen',
										'aria-label': 'Anbieter hinzufuegen',
										onClick: () => {
											if (!toAdd) return
											patchSource(toAdd, { enabled: true })
											setToAdd('')
										},
										children: '+'
									})
								]
						  })
						: null,

					rec && rec.note
						? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: rec.note })
						: null
				]
			}),

			jsx(Separator, {}),

			// --- Budgets --------------------------------------------------
			jsxs('div', {
				className: 'flex flex-col gap-1',
				children: [
					jsxs('div', {
						className: 'flex flex-col gap-1',
						children: [
							jsx('span', {
								className: 'text-xs text-(--ui-text-tertiary)',
								children: 'Budget Index-Quellen: ' + Number(draft.deadline_s).toFixed(0) + ' s'
							}),
							jsx(Range, {
								min: 1,
								max: 120,
								step: 1,
								value: draft.deadline_s,
								label: 'Budget Index-Quellen in Sekunden',
								text: String(draft.deadline_s) + ' Sekunden',
								onChange: (v) => patchConfig({ deadline_s: v })
							})
						]
					}),
					jsxs('div', {
						className: 'flex flex-col gap-1',
						children: [
							jsx('span', {
								className: 'text-xs text-(--ui-text-tertiary)',
								children: 'Budget LLM-Nachtrag: ' + Number(draft.llm_budget_s).toFixed(0) + ' s'
							}),
							jsx(Range, {
								min: 1,
								max: 120,
								step: 1,
								value: draft.llm_budget_s,
								label: 'Budget LLM-Nachtrag in Sekunden',
								text: String(draft.llm_budget_s) + ' Sekunden',
								onChange: (v) => patchConfig({ llm_budget_s: v })
							})
						]
					}),
					jsxs('div', {
						className: 'flex flex-col gap-1',
						children: [
							jsx('span', {
								className: 'text-xs text-(--ui-text-tertiary)',
								children: 'Deckel gesamt: ' + Number(draft.hard_timeout_s).toFixed(0) + ' s'
							}),
							jsx(Range, {
								min: 5,
								max: 300,
								step: 5,
								value: draft.hard_timeout_s,
								label: 'Deckel gesamt in Sekunden',
								text: String(draft.hard_timeout_s) + ' Sekunden',
								onChange: (v) => patchConfig({ hard_timeout_s: v })
							})
						]
					}),
					jsxs('div', {
						className: 'flex flex-col gap-1',
						children: [
							jsx('span', {
								className: 'text-xs text-(--ui-text-tertiary)',
								children: 'max. Zusatzquellen: ' + Number(draft.max_added).toFixed(0)
							}),
							jsx(Range, {
								min: 0,
								max: 50,
								step: 1,
								value: draft.max_added,
								label: 'maximale Zusatzquellen',
								text: String(draft.max_added),
								onChange: (v) => patchConfig({ max_added: v })
							}),
							jsx('span', {
								className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
								children:
									'Obergrenze fuer zusaetzliche Treffer aus der LLM-Zweitmeinung, zusaetzlich zu den ' +
									'Index-Treffern angehaengt. 0 = keine. Wirkt nur bei aktivem LLM-Nachtrag.'
							})
						]
					}),
					jsx(OptionRow, {
						label: 'auf den LLM-Nachtrag warten',
						hint: 'bis zu seinem Budget',
						children: jsx(Switch, {
							checked: !!draft.llm_wait,
							onCheckedChange: (v) => patchConfig({ llm_wait: !!v })
						})
					}),
					jsx(OptionRow, {
						label: 'auf ALLE Quellen warten',
						hint: 'Alt-Schalter — hebt Budgets auf, bis zum Deckel',
						children: jsx(Switch, {
							checked: !!draft.wait_for_all,
							onCheckedChange: (v) => patchConfig({ wait_for_all: !!v })
						})
					}),
					jsx(OptionRow, {
						label: 'LLM-Nachtrag anhaengen',
						hint: draft.llm_position === 'meta_only' ? 'aus — nur in meta' : 'an die Trefferliste',
						children: jsx(Switch, {
							checked: draft.llm_position !== 'meta_only',
							onCheckedChange: (v) => patchConfig({ llm_position: v ? 'append' : 'meta_only' })
						})
					}),
					jsx(OptionRow, {
						label: 'LLM-Nachtrag einsammeln',
						hint: 'zweite Meinung der LLM-Quellen',
						children: jsx(Switch, {
							checked: !!draft.llm_second_opinion,
							onCheckedChange: (v) => patchConfig({ llm_second_opinion: !!v })
						})
					}),
					jsx(OptionRow, {
						label: 'LLM-URLs pruefen',
						hint: 'Abruf vor Aufnahme',
						children: jsx(Switch, {
							checked: !!draft.verify_urls,
							onCheckedChange: (v) => patchConfig({ verify_urls: !!v })
						})
					}),
					jsx(OptionRow, {
						label: 'Rerank',
						hint: 'Domain-/Themengewichtung',
						children: jsx(Switch, {
							checked: !!draft.rerank_enabled,
							onCheckedChange: (v) => patchConfig({ rerank_enabled: !!v })
						})
					}),
					jsxs('div', {
						className: 'flex flex-col gap-1',
						children: [
							jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: 'unbestaetigte LLM-Quellen' }),
							jsx(Select, {
								value: draft.unverified,
								onValueChange: (v) => patchConfig({ unverified: v }),
								children: jsxs(SelectTrigger, {
									className: 'w-full',
									children: [jsx(SelectValue, {})]
								})
							}),
							jsx(SelectContent, {
								children: [
									jsx(SelectItem, { value: 'mark', children: 'als unbestaetigt kennzeichnen' }, 'mark'),
									jsx(SelectItem, { value: 'drop', children: 'verwerfen' }, 'drop')
								]
							})
						]
					})
				]
			}),

			jsx(Separator, {}),

			// --- Aktionen + Status ----------------------------------------
			jsxs('div', {
				className: 'flex flex-wrap items-center gap-2',
				children: [
					jsx(Button, { onClick: save, disabled: busy, children: busy ? 'speichert ...' : 'Speichern' }),
					jsx(Button, { onClick: reset, disabled: busy, children: 'Zuruecksetzen' })
				]
			}),
			jsx('div', {
				className:
					'text-[0.6875rem] ' + (status.tone === 'err' ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-quaternary)'),
				children: status.text
			}),
			jsx('div', {
				className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
				children:
					'Wirkt: ' +
					((state && state.config_path_read) || '—') +
					' · Geschrieben: ' +
					((state && state.config_path) || '—')
			}),
			warnings.length
				? jsx('ul', {
						className: 'list-disc pl-4 text-[0.6875rem] text-(--ui-text-tertiary)',
						children: warnings.map((w, i) => jsx('li', { children: w }, String(i)))
				  })
				: null
		]
	})
}

export default {
	id: ID,
	name: 'Search-Merge',
	register(ctx) {
		CTX = ctx
		ctx.register({
			id: 'pane',
			area: PANES_AREA,
			title: 'suche',
			data: { placement: 'right', width: '380px' },
			render: () => jsx(SearchMergePane, {})
		})
	}
}
