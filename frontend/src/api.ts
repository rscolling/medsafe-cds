import type { Card, CompareResponse, Drug, PatientSummary, Source, SourceStatus } from './types'

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new Error(body.detail ?? `HTTP ${res.status}`)
  }
  return (await res.json()) as T
}

export const api = {
  sources: () => fetch('/api/sources').then(json<Record<Source, SourceStatus>>),
  patients: (source: Source) => fetch(`/api/patients?source=${source}`).then(json<PatientSummary[]>),
  drugs: () => fetch('/api/drugs').then(json<Drug[]>),
  compare: (source: Source, patientId: string, rxcui: string) =>
    fetch('/api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, patientId, rxcui }),
    }).then(json<CompareResponse>),
  feedback: (
    serviceId: string,
    card: Card,
    outcome: 'accepted' | 'overridden',
    reasonCode?: string,
    reasonSystem?: string,
  ) =>
    fetch(`/cds-services/${serviceId}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        feedback: [
          {
            card: card.uuid,
            outcome,
            outcomeTimestamp: new Date().toISOString(),
            ...(outcome === 'accepted' && card.suggestions?.[0]
              ? { acceptedSuggestions: [{ id: card.suggestions[0].uuid }] }
              : {}),
            ...(outcome === 'overridden' && reasonCode && reasonSystem
              ? { overrideReason: { reason: { code: reasonCode, system: reasonSystem } } }
              : {}),
          },
        ],
      }),
    }).then((r) => {
      if (!r.ok) throw new Error(`feedback failed: HTTP ${r.status}`)
    }),
}
