import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import { ActiveMedsDialog } from './components/ActiveMedsDialog'
import { AlertCard } from './components/AlertCard'
import { PatientPanel } from './components/PatientPanel'
import { patientOptionText } from './patientLabel'
import type { CompareResponse, Drug, PatientSummary, Source, SourceStatus } from './types'

const DISCLAIMER = 'Prototype, not clinical advice. Synthetic data only. No real patients.'

// Label the FHIR source by what the backend actually serves (/api/sources mode), so a fixtures demo is not shown as HAPI.
function sourceLabel(source: Source, mode?: string): string {
  if (source === 'vista') return 'VistA (RPC Broker)'
  if (mode === 'fixtures') return 'FHIR R4 (bundled fixtures)'
  if (mode === 'http') return 'FHIR R4 (HAPI)'
  return 'FHIR R4'
}

export default function App() {
  const [source, setSource] = useState<Source>('fhir')
  const [statuses, setStatuses] = useState<Partial<Record<string, SourceStatus>>>({})
  const [patients, setPatients] = useState<PatientSummary[]>([])
  const [drugs, setDrugs] = useState<Drug[]>([])
  const [patientId, setPatientId] = useState('')
  const [rxcui, setRxcui] = useState('')
  const [result, setResult] = useState<CompareResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [medsOpen, setMedsOpen] = useState(false)
  const [medsRequest, setMedsRequest] = useState(0)
  const opener = useRef<HTMLElement | null>(null)
  const patientSelect = useRef<HTMLSelectElement>(null)
  const medsButton = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    api.drugs().then((d) => {
      setDrugs(d)
      setRxcui((cur) => cur || d[0]?.rxcui || '')
    }).catch((e: unknown) => { setError(String(e)) })
    api.sources().then(setStatuses).catch(() => { setStatuses({}) })
  }, [])

  useEffect(() => {
    let cancelled = false
    api
      .patients(source)
      .then((p) => {
        if (cancelled) return
        setPatients(p)
        setPatientId(p[0]?.id ?? '')
        setResult(null)
        setMedsOpen(false)
      })
      .catch((e: unknown) => { setError(String(e)) })
    return () => {
      cancelled = true
    }
  }, [source])

  const prescribe = () => {
    setBusy(true)
    setError(null)
    api
      .compare(source, patientId, rxcui)
      .then(setResult)
      .catch((e: unknown) => { setError(e instanceof Error ? e.message : String(e)); setResult(null) })
      .finally(() => { setBusy(false) })
  }

  const st = statuses[source]
  const selected = patients.find((p) => p.id === patientId)

  return (
    <main>
      <header>
        <h1>medsafe-cds: mock order entry</h1>
        <p className="banner" role="note" data-testid="banner">
          {DISCLAIMER}
        </p>
      </header>

      <section className="form" aria-label="Order entry">
        <fieldset>
          <legend>Data source</legend>
          {(['fhir', 'vista'] as const).map((s) => (
            <label key={s}>
              <input
                type="radio"
                name="source"
                value={s}
                checked={source === s}
                onChange={() => { setSource(s) }}
              />{' '}
              {sourceLabel(s, statuses[s]?.mode)}
            </label>
          ))}
          {st && (
            <span className="source-status" data-testid="source-status">
              mode: <strong>{st.mode}</strong>, reachable: {String(st.reachable)}
            </span>
          )}
        </fieldset>

        <label>
          Patient{' '}
          <select
            ref={patientSelect}
            value={patientId}
            onChange={(e) => {
              // Choosing a patient pops up their current medications (as an EHR chart opens on its med list).
              setPatientId(e.target.value)
              setResult(null)
              opener.current = patientSelect.current
              setMedsOpen(true)
              setMedsRequest((n) => n + 1)
            }}
            aria-label="Patient"
          >
            {patients.map((p) => (
              <option key={p.id} value={p.id}>
                {patientOptionText(p, source)}
              </option>
            ))}
          </select>
        </label>
        <button
          ref={medsButton}
          type="button"
          className="secondary"
          disabled={!patientId}
          aria-haspopup="dialog"
          onClick={() => {
            opener.current = medsButton.current
            setMedsOpen(true)
            setMedsRequest((n) => n + 1)
          }}
        >
          View active meds
        </button>
        {selected?.note && <small className="note">{selected.note}</small>}

        <label>
          Prescribe{' '}
          <select value={rxcui} onChange={(e) => { setRxcui(e.target.value) }} aria-label="Drug">
            {drugs.map((d) => (
              <option key={d.rxcui} value={d.rxcui}>
                {d.display}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={prescribe} disabled={busy || !patientId || !rxcui}>
          {busy ? 'Checking...' : 'Sign order'}
        </button>
      </section>

      {error && (
        <p role="alert" className="error" data-testid="error">
          {error}
        </p>
      )}

      {result && (
        <>
          <PatientPanel patient={result.patient} />
          <h2>
            Order: {result.order} - baseline vs context-aware
          </h2>
          <div className="columns" data-testid="comparison">
            <section aria-label="Baseline" data-testid="panel-baseline">
              <h3>
                Baseline (drug-class match) <span className="count" data-testid="count-baseline">{result.baseline.length}</span>
              </h3>
              {result.baseline.length === 0 && <p className="empty">No alert.</p>}
              {result.baseline.map((c) => (
                <AlertCard key={c.uuid} card={c} serviceId="medsafe-order-sign-baseline" interactive={false} />
              ))}
            </section>
            <section aria-label="Context-aware" data-testid="panel-context">
              <h3>
                Context-aware <span className="count" data-testid="count-context">{result.context.length}</span>
              </h3>
              {result.context.length === 0 && <p className="empty">No alert.</p>}
              {result.context.map((c) => (
                <AlertCard key={c.uuid} card={c} serviceId="medsafe-order-sign" />
              ))}
              {result.suppressed.length > 0 && (
                <div className="suppressed" data-testid="suppressed">
                  <strong>Suppressed by context ({result.suppressed.length})</strong>
                  <ul>
                    {result.suppressed.map((s) => (
                      <li key={s.ruleId}>
                        <code>{s.ruleId}</code>: {s.reasons.join('; ')}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          </div>
        </>
      )}

      <ActiveMedsDialog
        open={medsOpen}
        request={medsRequest}
        source={source}
        patient={selected}
        onClose={() => { setMedsOpen(false) }}
        returnFocus={() => opener.current}
      />

      <footer>
        <p>{DISCLAIMER}</p>
        <p className="credit" data-testid="credit">
          Built by Blue Ridge Bear Automation (BRBAutomation)
        </p>
      </footer>
    </main>
  )
}
