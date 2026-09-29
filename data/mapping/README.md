# Mapping tables: third-party code system terms

These CSVs are small, hand-made lookups (about 120 rows in total) written for a prototype. They reference
identifiers from code systems owned by others. Using or redistributing them does not transfer any rights in
those systems. Prototype, not clinical advice.

| file | code system | notice |
|---|---|---|
| `loinc.csv`, `vista_lab_tests.csv` | LOINC | This material contains content from LOINC (http://loinc.org). LOINC is copyright (c) 1995-2026, Regenstrief Institute, Inc. and the Logical Observation Identifiers Names and Codes (LOINC) Committee and is available at no cost under the license at http://loinc.org/license. LOINC(R) is a registered United States trademark of Regenstrief Institute, Inc. Only a handful of codes and their short display names are reproduced here. |
| `snomed_conditions.csv` | SNOMED CT | SNOMED CT(R) is a registered trademark of SNOMED International, and its content is licensed to members' countries and to affiliates (in the United States through the National Library of Medicine's UMLS Metathesaurus License). A few concept identifiers with short labels are used here only to recognise problem-list entries in synthetic data. Anyone reusing this file, or using SNOMED CT more broadly, must hold their own licence (free in the US and other member countries; see https://www.snomedinternational.org/get-snomed/). |
| `rxnorm_ingredients.csv`, `rxnorm_clinical_drugs.csv` | RxNorm | RxNorm identifiers (RXCUIs) and names are produced by the U.S. National Library of Medicine (NLM). RxNorm is a subset of the UMLS Metathesaurus, and the RxNorm APIs and files are available under the NLM terms of service (https://www.nlm.nih.gov/research/umls/rxnorm/docs/termsofservice.html). NLM is not responsible for this product and does not endorse or recommend it. The `classes` and `synonyms` columns are the author's own. |
| `icd9_prefixes.csv` | ICD-9-CM | ICD-9-CM is a U.S. Government (CMS/NCHS) publication and is in the public domain. Only 3-digit code prefixes are used. |

If you add codes, add the source and its terms here.
