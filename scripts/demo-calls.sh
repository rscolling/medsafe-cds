#!/usr/bin/env bash
# CDS Hooks calls for the key demo cases, on BOTH data sources (fhir and vista).
# PROTOTYPE, NOT CLINICAL ADVICE. Synthetic data only: hand-authored patients (hand-NN), their labeled VistA
# overlay twins (DFN 90000NN), the seeded Synthea-style cohort (syn-NNNN) and the public VEHU test image (DFN 100881).
#
# Usage:  make demo-local            # in another terminal (API on :8080)
#         ./scripts/demo-calls.sh    # or: MEDSAFE_API=http://host:port ./scripts/demo-calls.sh
#         ./scripts/demo-calls.sh -v # also print each request body and full response JSON
# Needs curl; with jq it prints one line per card and checks the expected result (exit 1 on a mismatch).
set -euo pipefail

API="${MEDSAFE_API:-http://localhost:8080}"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1
HAVE_JQ=0
command -v jq >/dev/null 2>&1 && HAVE_JQ=1
FAILS=0

# RxNorm SCDs (as listed by GET /api/drugs)
METFORMIN_500="861007|metformin hydrochloride 500 MG Oral Tablet"
IBUPROFEN_800="197807|ibuprofen 800 MG Oral Tablet"

uuid() {
  if command -v uuidgen >/dev/null 2>&1; then uuidgen | tr 'A-Z' 'a-z'
  elif [ -r /proc/sys/kernel/random/uuid ]; then cat /proc/sys/kernel/random/uuid
  else python3 -c 'import uuid; print(uuid.uuid4())'
  fi
}

# hook_body <patientId> <source> <rxcui|display>
hook_body() {
  local patient="$1" source="$2" rxcui="${3%%|*}" display="${3#*|}"
  cat <<JSON
{
  "hook": "order-sign",
  "hookInstance": "$(uuid)",
  "context": {
    "userId": "Practitioner/demo",
    "patientId": "$patient",
    "draftOrders": {
      "resourceType": "Bundle",
      "type": "collection",
      "entry": [{"resource": {
        "resourceType": "MedicationRequest", "id": "draft-1", "status": "draft", "intent": "order",
        "subject": {"reference": "Patient/$patient"},
        "medicationCodeableConcept": {
          "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "$rxcui", "display": "$display"}],
          "text": "$display"
        }
      }}]
    }
  },
  "extension": {"org.medsafe.source": "$source"}
}
JSON
}

# call <title> <service> <source> <patientId> <drug> <expected: "indicator:ruleId" | "none">
call() {
  local title="$1" service="$2" source="$3" patient="$4" drug="$5" expect="$6" body resp got
  body="$(hook_body "$patient" "$source" "$drug")"
  printf '\n== %s\n   POST /cds-services/%s  source=%s  patient=%s  order=%s\n' \
    "$title" "$service" "$source" "$patient" "${drug#*|}"
  [ "$VERBOSE" = 1 ] && printf '%s\n' "$body"
  resp="$(curl -sS --fail-with-body -X POST "$API/cds-services/$service" -H 'Content-Type: application/json' -d "$body")"
  if [ "$HAVE_JQ" = 0 ]; then
    printf '%s\n' "$resp"
    return
  fi
  [ "$VERBOSE" = 1 ] && printf '%s\n' "$resp" | jq .
  printf '%s\n' "$resp" | jq -r '
    if (.cards | length) == 0 then "   (no cards)"
    else .cards[] | "   [\(.indicator)] \(.summary)\(if .extension["org.medsafe.dataGap"] then "  (data gap)" else "" end)"
    end'
  got="$(printf '%s\n' "$resp" | jq -r '[.cards[] | "\(.indicator):\(.extension["org.medsafe.ruleId"])"] | if length == 0 then "none" else join(",") end')"
  if [ "$got" = "$expect" ]; then
    printf '   ok (expected %s)\n' "$expect"
  else
    printf '   MISMATCH: expected %s, got %s\n' "$expect" "$got"
    FAILS=$((FAILS + 1))
  fi
}

curl -sS --fail "$API/health" >/dev/null || { echo "API not reachable at $API (start it with: make demo-local)" >&2; exit 2; }
echo "medsafe-cds demo calls against $API  (prototype, not clinical advice; synthetic data only)"

# 1. Metformin with eGFR < 30: critical card
call "Metformin, eGFR < 30 -> critical (FHIR: seeded synthetic patient syn-0001, eGFR 26.8)" \
  medsafe-order-sign fhir syn-0001 "$METFORMIN_500" "critical:metformin-low-egfr"
call "Metformin, eGFR < 30 -> critical (VistA: public VEHU test patient 100881, eGFR computed from creatinine)" \
  medsafe-order-sign vista 100881 "$METFORMIN_500" "critical:metformin-low-egfr"

# 2. Context suppression: baseline fires on the drug class, context sees a normal eGFR and stays quiet
for pair in "fhir hand-02" "vista 9000002"; do
  set -- $pair
  call "Metformin, normal eGFR: BASELINE fires on drug class ($1)" \
    medsafe-order-sign-baseline "$1" "$2" "$METFORMIN_500" "warning:metformin-low-egfr"
  call "Metformin, normal eGFR: CONTEXT suppresses it ($1)" \
    medsafe-order-sign "$1" "$2" "$METFORMIN_500" "none"
done

# 3. Data gap: no renal labs on file -> info card saying what is missing
call "Metformin, no renal labs -> data-gap info card (FHIR hand-03)" \
  medsafe-order-sign fhir hand-03 "$METFORMIN_500" "info:metformin-low-egfr"
call "Metformin, no renal labs -> data-gap info card (VistA twin 9000003)" \
  medsafe-order-sign vista 9000003 "$METFORMIN_500" "info:metformin-low-egfr"

# 4. NSAID + ACEI + diuretic from ONE combination tablet (LISINOPRIL-HCTZ 20-12.5), eGFR ~40
call "Ibuprofen + LISINOPRIL-HCTZ combo tablet, eGFR ~40 -> triple whammy (FHIR hand-11)" \
  medsafe-order-sign fhir hand-11 "$IBUPROFEN_800" "warning:nsaid-raas-diuretic-aki"
call "Ibuprofen + LISINOPRIL-HCTZ combo tablet, eGFR ~40 -> triple whammy (VistA twin 9000011)" \
  medsafe-order-sign vista 9000011 "$IBUPROFEN_800" "warning:nsaid-raas-diuretic-aki"

if [ "$HAVE_JQ" = 1 ]; then
  echo
  if [ "$FAILS" -eq 0 ]; then echo "all demo cases returned the expected cards"; else echo "$FAILS case(s) did not match"; exit 1; fi
fi
