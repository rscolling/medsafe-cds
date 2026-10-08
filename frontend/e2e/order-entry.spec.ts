import { expect, test, type Locator, type Page } from '@playwright/test'

const DISCLAIMER = 'Prototype, not clinical advice'

async function pick(select: Locator, text: RegExp) {
  const value = await select.locator('option', { hasText: text }).first().getAttribute('value')
  if (!value) throw new Error(`no option matching ${text.source}`)
  await select.selectOption(value)
}

// Choosing a patient pops up the active-medications card; dismiss it (Esc) before ordering.
async function choosePatient(page: Page, patient: RegExp) {
  await pick(page.getByLabel('Patient'), patient)
  const dialog = page.getByRole('dialog', { name: 'Current active medications' })
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
}

async function prescribe(page: Page, patient: RegExp, drug: RegExp) {
  await choosePatient(page, patient)
  await pick(page.getByLabel('Drug'), drug)
  await page.getByRole('button', { name: 'Sign order' }).click()
}

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.getByTestId('banner')).toContainText(DISCLAIMER)
  await expect(page.getByLabel('Patient').locator('option')).not.toHaveCount(0)
})

test('FHIR: metformin with eGFR 36 fires with why + source + disclaimer', async ({ page }) => {
  await prescribe(page, /^Synthetic, A · /, /metformin hydrochloride 500/)
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
  await prescribe(page, /^Synthetic, B · /, /metformin hydrochloride 500/)
  await expect(page.getByTestId('count-baseline')).toHaveText('1')
  await expect(page.getByTestId('count-context')).toHaveText('0')
  await expect(page.getByTestId('suppressed')).toContainText('Suppressed by context')
})

test('VistA source: same rule fires for the VistA twin as for the FHIR patient', async ({ page }) => {
  await page.getByLabel('VistA (RPC Broker)').check()
  await expect(page.getByTestId('source-status')).toContainText('mode:')
  await prescribe(page, /^SYNTHETICPATIENT,ATWIN · /, /metformin hydrochloride 500/)
  const card = page.getByTestId('panel-context').getByTestId('alert-card')
  await expect(card).toHaveAttribute('data-rule', 'metformin-low-egfr')
  await expect(page.getByTestId('egfr')).toContainText('computed')
})

test('real VEHU record: computed eGFR drives a critical card', async ({ page }) => {
  await page.getByLabel('VistA (RPC Broker)').check()
  await prescribe(page, / · DFN 100881$/, /metformin hydrochloride 500/)
  const card = page.getByTestId('panel-context').getByTestId('alert-card')
  await expect(card).toHaveAttribute('data-rule', 'metformin-low-egfr')
  await expect(card.locator('.pill-critical')).toBeVisible()
  await expect(page.getByTestId('egfr')).toContainText('computed')
})

test('data gap card is shown, flagged and informational', async ({ page }) => {
  await prescribe(page, /^Synthetic, C · /, /metformin hydrochloride 500/)
  const card = page.getByTestId('panel-context').getByTestId('alert-card')
  await expect(card).toHaveAttribute('data-gap', 'true')
  await expect(card.locator('.pill-info')).toBeVisible()
})

test('override reason is sent and audited', async ({ page }) => {
  await prescribe(page, /^Synthetic, A · /, /metformin hydrochloride 500/)
  const card = page.getByTestId('panel-context').getByTestId('alert-card')
  await card.getByLabel('Override reason').selectOption('benefit-outweighs-risk')
  await card.getByRole('button', { name: 'Override' }).click()
  await expect(card.getByTestId('card-status')).toContainText('benefit-outweighs-risk')
  const audit = await page.request.get('/api/audit?kind=feedback')
  const events = (await audit.json()) as { override_code: string }[]
  expect(events.some((e) => e.override_code === 'benefit-outweighs-risk')).toBe(true)
})

test('every rendered card carries the disclaimer', async ({ page }) => {
  await prescribe(page, /^Synthetic, D · /, /ibuprofen 800/)
  const cards = page.getByTestId('alert-card')
  await expect(cards).toHaveCount(2) // baseline + context
  for (const c of await cards.all()) {
    await expect(c.getByTestId('card-disclaimer')).toContainText(DISCLAIMER)
  }
})

test('footer credits Blue Ridge Bear Automation and keeps the disclaimer', async ({ page }) => {
  await expect(page.getByTestId('credit')).toHaveText('Built by Blue Ridge Bear Automation (BRBAutomation)')
  await expect(page.locator('footer')).toContainText(DISCLAIMER)
})

test('FHIR source label reflects the mode the backend serves (bundled fixtures here, not HAPI)', async ({ page }) => {
  await expect(page.getByTestId('source-status')).toContainText('mode: fixtures')
  await expect(page.getByLabel('FHIR R4 (bundled fixtures)')).toBeChecked()
  await expect(page.getByText('FHIR R4 (HAPI)')).toHaveCount(0)
})

test('LISINOPRIL-HCTZ combo tablet + ibuprofen fires the triple-whammy rule on FHIR and on the VistA twin', async ({ page }) => {
  await prescribe(page, /^Synthetic, K · /, /ibuprofen 800/)
  await expect(page.getByTestId('panel-context').getByTestId('alert-card')).toHaveAttribute('data-rule', 'nsaid-raas-diuretic-aki')
  await page.getByLabel('VistA (RPC Broker)').check()
  await expect(page.getByTestId('source-status')).toContainText('mode:')
  await prescribe(page, /^SYNTHETICPATIENT,KTWIN · /, /ibuprofen 800/)
  await expect(page.getByTestId('panel-context').getByTestId('alert-card')).toHaveAttribute('data-rule', 'nsaid-raas-diuretic-aki')
  await expect(page.getByTestId('egfr')).toContainText('computed')
})

// Option text must read like an EHR patient list: identity only, no drug / lab / scenario words.
const CLINICAL = /metformin|lisinopril|ibuprofen|nsaid|acei|\bkcl\b|lithium|spironolactone|egfr|potassium|furosemide|losartan|apixaban|rivaroxaban|twin of|\bK \d|data gap|scenario/i
const IDENTITY = /^(.+ · [MFOU](, \d+ y)?( · DOB [\d-]+)?|Name unavailable) · (ID|DFN) [\w-]+( · MRN \S+)?$/

for (const source of ['fhir', 'vista'] as const) {
  test(`${source}: patient dropdown shows identifying details only`, async ({ page }) => {
    if (source === 'vista') {
      await page.getByLabel('VistA (RPC Broker)').check()
      await expect(page.getByLabel('Patient').locator('option', { hasText: 'DFN 100881' })).toHaveCount(1)
    }
    const texts = await page.getByLabel('Patient').locator('option').allTextContents()
    expect(texts.length).toBeGreaterThan(10)
    for (const t of texts) {
      expect(t, t).toMatch(IDENTITY)
      expect(t, t).not.toMatch(CLINICAL)
    }
  })
}

test('choosing a patient pops up their active meds: keyboard dismiss, focus return, reopen button', async ({ page }) => {
  const select = page.getByLabel('Patient')
  await pick(select, /^Synthetic, D · /)
  const dialog = page.getByRole('dialog', { name: 'Current active medications' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Close' })).toBeFocused()
  await expect(dialog.getByTestId('meds-patient')).toContainText('ID hand-04')
  const rows = dialog.getByTestId('med-row')
  await expect(rows).toHaveCount(2)
  await expect(rows.nth(0)).toContainText('lisinopril 10 MG Oral Tablet')
  await expect(rows.nth(0)).toContainText('314076')
  await expect(rows.nth(1)).toContainText('furosemide 20 MG Oral Tablet')
  await expect(dialog.getByText('Prototype, not clinical advice')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(select).toBeFocused()

  const reopen = page.getByRole('button', { name: 'View active meds' })
  await reopen.click()
  await expect(dialog).toBeVisible()
  await expect(rows).toHaveCount(2)
  await dialog.getByRole('button', { name: 'Close' }).click()
  await expect(dialog).toBeHidden()
  await expect(reopen).toBeFocused()
})

test('VistA popup lists the source status and mapped RxNorm; a patient with no meds says so', async ({ page }) => {
  await page.getByLabel('VistA (RPC Broker)').check()
  await expect(page.getByTestId('source-status')).toContainText('mode:')
  await pick(page.getByLabel('Patient'), /^SYNTHETICPATIENT,DTWIN · /)
  const dialog = page.getByRole('dialog', { name: 'Current active medications' })
  const rows = dialog.getByTestId('med-row')
  await expect(rows).toHaveCount(2)
  await expect(rows.nth(0)).toContainText('LISINOPRIL 10MG TAB')
  await expect(rows.nth(0)).toContainText('pending')
  await expect(rows.nth(0)).toContainText('29046 (mapped from text)')
  await dialog.getByRole('button', { name: 'Close' }).click()
  await pick(page.getByLabel('Patient'), / · DFN 100881$/)
  await expect(dialog.getByTestId('meds-patient')).toContainText('HYPERTENSION,PATIENT FEMALE')
  await expect(dialog.getByTestId('meds-empty')).toBeVisible()
})
