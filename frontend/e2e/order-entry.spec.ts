import { expect, test, type Locator, type Page } from '@playwright/test'

const DISCLAIMER = 'Prototype, not clinical advice'

async function pick(select: Locator, text: RegExp) {
  const value = await select.locator('option', { hasText: text }).first().getAttribute('value')
  if (!value) throw new Error(`no option matching ${text.source}`)
  await select.selectOption(value)
}

async function prescribe(page: Page, patient: RegExp, drug: RegExp) {
  await pick(page.getByLabel('Patient'), patient)
  await pick(page.getByLabel('Drug'), drug)
  await page.getByRole('button', { name: 'Sign order' }).click()
}

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.getByTestId('banner')).toContainText(DISCLAIMER)
  await expect(page.getByLabel('Patient').locator('option')).not.toHaveCount(0)
})

test('FHIR: metformin with eGFR 36 fires with why + source + disclaimer', async ({ page }) => {
  await prescribe(page, /^A\. Metformin, eGFR 30-45$/, /metformin hydrochloride 500/)
  const ctx = page.getByTestId('panel-context')
  await expect(page.getByTestId('count-baseline')).toHaveText('1')
  await expect(page.getByTestId('count-context')).toHaveText('1')
  const card = ctx.getByTestId('alert-card')
  await expect(card).toHaveAttribute('data-rule', 'metformin-low-egfr')
  await expect(card.getByTestId('card-disclaimer')).toContainText(DISCLAIMER)
  await expect(card.getByTestId('card-why')).toContainText('eGFR')
  await expect(card.getByTestId('card-source')).toHaveAttribute('href', /^https:\/\/dailymed\.nlm\.nih\.gov\//)
  await expect(card.getByTestId('card-suggestions')).toContainText('Cancel the metformin order')
})

test('context-aware suppresses a false alarm that baseline shows', async ({ page }) => {
  await prescribe(page, /^B\. Metformin, normal eGFR$/, /metformin hydrochloride 500/)
  await expect(page.getByTestId('count-baseline')).toHaveText('1')
  await expect(page.getByTestId('count-context')).toHaveText('0')
  await expect(page.getByTestId('suppressed')).toContainText('Suppressed by context')
})

test('VistA source: same rule fires for the VistA twin as for the FHIR patient', async ({ page }) => {
  await page.getByLabel('VistA (RPC Broker)').check()
  await expect(page.getByTestId('source-status')).toContainText('mode:')
  await prescribe(page, /^A\. Metformin, eGFR 30-45 \[VistA twin/, /metformin hydrochloride 500/)
  const card = page.getByTestId('panel-context').getByTestId('alert-card')
  await expect(card).toHaveAttribute('data-rule', 'metformin-low-egfr')
  await expect(page.getByTestId('egfr')).toContainText('computed')
})

test('real VEHU record: computed eGFR drives a critical card', async ({ page }) => {
  await page.getByLabel('VistA (RPC Broker)').check()
  await prescribe(page, /100881|DM\/HTN\/CKD/, /metformin hydrochloride 500/)
  const card = page.getByTestId('panel-context').getByTestId('alert-card')
  await expect(card).toHaveAttribute('data-rule', 'metformin-low-egfr')
  await expect(card.locator('.pill-critical')).toBeVisible()
  await expect(page.getByTestId('egfr')).toContainText('computed')
})

test('data gap card is shown, flagged and informational', async ({ page }) => {
  await prescribe(page, /^C\. Metformin, no renal labs/, /metformin hydrochloride 500/)
  const card = page.getByTestId('panel-context').getByTestId('alert-card')
  await expect(card).toHaveAttribute('data-gap', 'true')
  await expect(card.locator('.pill-info')).toBeVisible()
})

test('override reason is sent and audited', async ({ page }) => {
  await prescribe(page, /^A\. Metformin, eGFR 30-45$/, /metformin hydrochloride 500/)
  const card = page.getByTestId('panel-context').getByTestId('alert-card')
  await card.getByLabel('Override reason').selectOption('benefit-outweighs-risk')
  await card.getByRole('button', { name: 'Override' }).click()
  await expect(card.getByTestId('card-status')).toContainText('benefit-outweighs-risk')
  const audit = await page.request.get('/api/audit?kind=feedback')
  const events = (await audit.json()) as { override_code: string }[]
  expect(events.some((e) => e.override_code === 'benefit-outweighs-risk')).toBe(true)
})

test('every rendered card carries the disclaimer', async ({ page }) => {
  await prescribe(page, /^D\. NSAID \+ ACEI/, /ibuprofen 800/)
  const cards = page.getByTestId('alert-card')
  await expect(cards).toHaveCount(2) // baseline + context
  for (const c of await cards.all()) {
    await expect(c.getByTestId('card-disclaimer')).toContainText(DISCLAIMER)
  }
})

test('FHIR source label reflects the mode the backend serves (bundled fixtures here, not HAPI)', async ({ page }) => {
  await expect(page.getByTestId('source-status')).toContainText('mode: fixtures')
  await expect(page.getByLabel('FHIR R4 (bundled fixtures)')).toBeChecked()
  await expect(page.getByText('FHIR R4 (HAPI)')).toHaveCount(0)
})

test('LISINOPRIL-HCTZ combo tablet + ibuprofen fires the triple-whammy rule on FHIR and on the VistA twin', async ({ page }) => {
  await prescribe(page, /^K\. NSAID \+ LISINOPRIL-HCTZ/, /ibuprofen 800/)
  await expect(page.getByTestId('panel-context').getByTestId('alert-card')).toHaveAttribute('data-rule', 'nsaid-raas-diuretic-aki')
  await page.getByLabel('VistA (RPC Broker)').check()
  await expect(page.getByTestId('source-status')).toContainText('mode:')
  await prescribe(page, /^K\. NSAID \+ LISINOPRIL-HCTZ.*\[VistA twin/, /ibuprofen 800/)
  await expect(page.getByTestId('panel-context').getByTestId('alert-card')).toHaveAttribute('data-rule', 'nsaid-raas-diuretic-aki')
  await expect(page.getByTestId('egfr')).toContainText('computed')
})
