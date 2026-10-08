import type { PatientSummary, Source } from './types'

/** "DFN" for VistA (its internal patient number), "ID" for a FHIR Patient id. */
export function idLabel(source: Source): string {
  return source === 'vista' ? 'DFN' : 'ID'
}

/**
 * Patient-picker text: identifying details only (name, sex, age, DOB, ID / DFN, MRN when present), like an EHR
 * patient list. No medications, labs or demo-scenario labels. All patients are synthetic.
 */
export function patientOptionText(p: PatientSummary, source: Source): string {
  const parts: string[] = []
  if (p.demographics === 'ok' && p.name) {
    parts.push(p.name)
    const sexAge = [p.sex ?? '', p.age !== null ? `${p.age} y` : ''].filter(Boolean).join(', ')
    if (sexAge) parts.push(sexAge)
    if (p.birthDate) parts.push(`DOB ${p.birthDate}`)
  } else {
    parts.push('Name unavailable')
  }
  parts.push(`${idLabel(source)} ${p.id}`)
  if (p.mrn) parts.push(`MRN ${p.mrn}`)
  return parts.join(' · ')
}
