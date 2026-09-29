import type { Lab, PatientSnapshot } from '../types'

function labText(lab: Lab | null, unit: string): string {
  if (!lab) return 'none on file'
  return `${lab.value} ${unit} (${lab.date}, ${lab.source})`
}

export function PatientPanel({ patient }: { patient: PatientSnapshot }) {
  return (
    <section className="patient" data-testid="patient-panel">
      <h3>
        Patient {patient.id} ({patient.source.toUpperCase()}) - {patient.sex}, {patient.age} y
      </h3>
      <dl>
        <dt>Record date (anchor for relative windows)</dt>
        <dd>{patient.as_of}</dd>
        <dt>Conditions</dt>
        <dd>{patient.conditions.join(', ') || 'none mapped'}</dd>
        <dt>Current meds</dt>
        <dd>{patient.meds.join(', ') || 'none'}</dd>
        {patient.unmapped_meds.length > 0 && (
          <>
            <dt>Unmapped med names (not evaluated)</dt>
            <dd>{patient.unmapped_meds.join('; ')}</dd>
          </>
        )}
        <dt>eGFR</dt>
        <dd data-testid="egfr">{labText(patient.egfr, 'mL/min/1.73 m2')}</dd>
        <dt>Creatinine</dt>
        <dd>{labText(patient.creatinine, 'mg/dL')}</dd>
        <dt>Potassium</dt>
        <dd>{labText(patient.potassium, 'mmol/L')}</dd>
        <dt>Weight</dt>
        <dd>{labText(patient.weight, 'kg')}</dd>
      </dl>
    </section>
  )
}
