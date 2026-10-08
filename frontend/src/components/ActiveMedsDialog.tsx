import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { idLabel } from '../patientLabel'
import type { MedicationItem, PatientSummary, Source } from '../types'

interface Props {
  open: boolean
  source: Source
  patient: PatientSummary | undefined
  onClose: () => void
  /** Element that gets focus back when the dialog closes (the picker or the "View active meds" button). */
  returnFocus: () => HTMLElement | null
}

type Load =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'ok'; meds: MedicationItem[] }

function statusText(m: MedicationItem): string {
  // VistA keeps its own word (PENDING / ACTIVE / HOLD); FHIR status otherwise.
  return (m.sourceStatus ?? m.status).toLowerCase()
}

function rxnormText(m: MedicationItem): string {
  if (m.rxnorm.length === 0) return 'not mapped'
  const codes = m.rxnorm.map((r) => r.code).join(', ')
  return m.rxnormSource === 'mapped' ? `${codes} (mapped from text)` : codes
}

/**
 * Pop-up card listing the selected patient's current medications from the selected source. A native modal
 * <dialog>: focus moves into it, Tab stays inside, Esc / the Close button / a backdrop click dismiss it, and focus
 * returns to the control that opened it.
 */
export function ActiveMedsDialog({ open, source, patient, onClose, returnFocus }: Props) {
  const ref = useRef<HTMLDialogElement>(null)
  const closeBtn = useRef<HTMLButtonElement>(null)
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const patientId = patient?.id

  useEffect(() => {
    const dlg = ref.current
    if (!dlg) return
    if (open && !dlg.open) {
      dlg.showModal()
      closeBtn.current?.focus()
    } else if (!open && dlg.open) {
      dlg.close()
    }
  }, [open])

  useEffect(() => {
    if (!open || !patientId) return
    let cancelled = false
    setLoad({ state: 'loading' })
    api
      .medications(source, patientId)
      .then((r) => { if (!cancelled) setLoad({ state: 'ok', meds: r.medications }) })
      .catch((e: unknown) => { if (!cancelled) setLoad({ state: 'error', message: e instanceof Error ? e.message : String(e) }) })
    return () => {
      cancelled = true
    }
  }, [open, source, patientId])

  const handleClose = () => {
    onClose()
    returnFocus()?.focus()
  }

  return (
    <dialog
      ref={ref}
      className="meds-dialog"
      aria-labelledby="meds-dialog-title"
      aria-describedby="meds-dialog-desc"
      data-testid="meds-dialog"
      onClose={handleClose}
      onClick={(e) => {
        if (e.target === ref.current) ref.current.close() // backdrop click
      }}
    >
      <div className="meds-card">
        <header className="meds-head">
          <h2 id="meds-dialog-title">Current active medications</h2>
          <button ref={closeBtn} type="button" className="secondary" onClick={() => ref.current?.close()}>
            Close
          </button>
        </header>
        {patient && (
          <p className="meds-patient" data-testid="meds-patient">
            <strong>{patient.name ?? 'Name unavailable'}</strong>
            {patient.sex && <> · {patient.sex}</>}
            {patient.age !== null && <>, {patient.age} y</>}
            {patient.birthDate && <> · DOB {patient.birthDate}</>} · {idLabel(source)} {patient.id}
            {patient.mrn && <> · MRN {patient.mrn}</>}
          </p>
        )}
        <p id="meds-dialog-desc" className="meds-sub">
          Source: {source === 'vista' ? 'VistA (ORWPS ACTIVE)' : 'FHIR MedicationRequest (status=active)'}. Synthetic data
          only.
        </p>
        {load.state === 'loading' && <p aria-live="polite">Loading medications...</p>}
        {load.state === 'error' && (
          <p role="alert" className="error">
            Could not load medications: {load.message}
          </p>
        )}
        {load.state === 'ok' && load.meds.length === 0 && (
          <p className="empty" data-testid="meds-empty">
            No current medications on file.
          </p>
        )}
        {load.state === 'ok' && load.meds.length > 0 && (
          <table className="meds-table" data-testid="meds-table">
            <thead>
              <tr>
                <th scope="col">Medication</th>
                <th scope="col">Dose / sig</th>
                <th scope="col">Status</th>
                <th scope="col">RxNorm</th>
              </tr>
            </thead>
            <tbody>
              {load.meds.map((m, i) => (
                <tr key={m.id ?? `${m.name}-${i}`} data-testid="med-row">
                  <td>
                    {m.name}
                    {m.category && <span className="meds-cat"> ({m.category})</span>}
                  </td>
                  <td>{m.sig ?? '-'}</td>
                  <td>{statusText(m)}</td>
                  <td>{rxnormText(m)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="card-disclaimer small">Prototype, not clinical advice. Synthetic data only.</p>
      </div>
    </dialog>
  )
}
