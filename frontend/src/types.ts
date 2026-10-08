export type Source = 'fhir' | 'vista'

export interface PatientSummary {
  id: string
  /** Identity from the patient's own Patient resource / VistA demographics (null when the source had none). */
  name: string | null
  sex: string | null
  birthDate: string | null
  age: number | null
  mrn: string | null
  demographics: 'ok' | 'unavailable'
  /** Internal demo-scenario label (docs/tests). Never shown in the patient picker. */
  label: string
  kind: string
  note: string
}

export interface RxNormCode {
  code: string
  display: string
}

export interface MedicationItem {
  id: string | null
  name: string
  sig: string | null
  status: string
  sourceStatus: string | null
  category: string | null
  rxnorm: RxNormCode[]
  rxnormSource: 'coded' | 'mapped' | null
}

export interface MedicationList {
  patientId: string
  source: Source
  disclaimer: string
  medications: MedicationItem[]
}

export interface Drug {
  rxcui: string
  display: string
  ingredient_rxcui: string
}

export interface CardSuggestion {
  label: string
  uuid: string
  isRecommended?: boolean
}

export interface OverrideReason {
  code: string
  system: string
  display: string
}

export interface CardExtension {
  'org.medsafe.ruleId': string
  'org.medsafe.ruleVersion': string
  'org.medsafe.mode': string
  'org.medsafe.dataGap': boolean
  'org.medsafe.why': string[]
  'org.medsafe.disclaimer': string
}

export interface Card {
  uuid: string
  summary: string
  detail: string
  indicator: 'info' | 'warning' | 'critical'
  source: { label: string; url: string }
  suggestions?: CardSuggestion[]
  overrideReasons?: OverrideReason[]
  extension: CardExtension
}

export interface Lab {
  value: number
  unit: string
  date: string
  source: 'reported' | 'computed'
}

export interface PatientSnapshot {
  id: string
  source: string
  sex: string
  age: number
  as_of: string
  meds: string[]
  unmapped_meds: string[]
  conditions: string[]
  egfr: Lab | null
  creatinine: Lab | null
  potassium: Lab | null
  weight: Lab | null
}

export interface Suppressed {
  ruleId: string
  ruleVersion: string
  reasons: string[]
}

export interface CompareResponse {
  disclaimer: string
  patient: PatientSnapshot
  order: string
  baseline: Card[]
  context: Card[]
  suppressed: Suppressed[]
}

export interface SourceStatus {
  mode: string
  reachable: boolean
  [key: string]: unknown
}
