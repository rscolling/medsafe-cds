import { useState } from 'react'
import { api } from '../api'
import type { Card } from '../types'

const DISCLAIMER = 'Prototype, not clinical advice. Synthetic data only.'

interface Props {
  card: Card
  serviceId: string
  interactive?: boolean
}

export function AlertCard({ card, serviceId, interactive = true }: Props) {
  const [status, setStatus] = useState<string | null>(null)
  const [reason, setReason] = useState<string>(card.overrideReasons?.[0]?.code ?? '')
  const ext = card.extension

  const send = (outcome: 'accepted' | 'overridden') => {
    const system = card.overrideReasons?.[0]?.system
    api
      .feedback(serviceId, card, outcome, outcome === 'overridden' ? reason : undefined, system)
      .then(() => {
        setStatus(outcome === 'accepted' ? 'Suggestion accepted (audited)' : `Overridden: ${reason} (audited)`)
      })
      .catch((e: unknown) => {
        setStatus(e instanceof Error ? e.message : 'feedback failed')
      })
  }

  return (
    <article
      className={`card card-${card.indicator}`}
      data-testid="alert-card"
      data-rule={ext['org.medsafe.ruleId']}
      data-gap={ext['org.medsafe.dataGap']}
    >
      <p className="card-disclaimer" data-testid="card-disclaimer">
        {DISCLAIMER}
      </p>
      <h4 className="card-summary">{card.summary}</h4>
      <p className="card-meta">
        <span className={`pill pill-${card.indicator}`}>{card.indicator}</span>{' '}
        <code>
          {ext['org.medsafe.ruleId']} v{ext['org.medsafe.ruleVersion']}
        </code>{' '}
        ({ext['org.medsafe.mode']} mode)
        {ext['org.medsafe.dataGap'] && <span className="pill pill-gap">data gap</span>}
      </p>
      <section>
        <strong>Why this fired</strong>
        <ul data-testid="card-why">
          {ext['org.medsafe.why'].map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      </section>
      {card.suggestions && (
        <section>
          <strong>Suggested alternative</strong>
          <ul data-testid="card-suggestions">
            {card.suggestions.map((s) => (
              <li key={s.uuid}>{s.label}</li>
            ))}
          </ul>
        </section>
      )}
      <p>
        Source:{' '}
        <a href={card.source.url} target="_blank" rel="noreferrer" data-testid="card-source">
          {card.source.label}
        </a>
      </p>
      {interactive && card.overrideReasons && (
        <div className="card-actions">
          {card.suggestions && (
            <button type="button" onClick={() => { send('accepted') }}>
              Accept suggestion
            </button>
          )}
          <label>
            Override reason{' '}
            <select value={reason} onChange={(e) => { setReason(e.target.value) }} aria-label="Override reason">
              {card.overrideReasons.map((r) => (
                <option key={r.code} value={r.code}>
                  {r.display}
                </option>
              ))}
            </select>
          </label>
          <button type="button" onClick={() => { send('overridden') }}>
            Override
          </button>
        </div>
      )}
      {status && (
        <p role="status" data-testid="card-status">
          {status}
        </p>
      )}
      <p className="card-disclaimer small">{DISCLAIMER}</p>
    </article>
  )
}
