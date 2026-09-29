export type Source = 'fhir' | 'vista'

export interface PatientSummary {
  id: string
  label: string
  kind: string
  note: string
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
