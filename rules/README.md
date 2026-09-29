# Rules

Each `*.yaml` file is one versioned rule. Schema (validated by `app/rules/schema.py`):

- `id`, `version` (semver), `title`, `status` (`prototype`), `severity`
- `match`: which orders trigger the rule. `groups` is a list of drug-class lists; the regimen
  (draft order + current active meds) must contain a *distinct* drug for every group, and the draft
  order must be one of them. This is the **baseline** ("any drug-class match fires") definition.
- `context`: what context-aware mode adds. Either
  - `check: <name>` - a named Python function in `backend/app/rules/checks.py` for logic YAML can't
    express (e.g. "latest eGFR within 90 days, else data gap"), with `params`; and/or
  - `suppress_when_all` - declarative predicates (`lab`/`condition`/`age`) that, when all true, suppress.
- `card`: summary, detail, why templates, suggestions, override reasons, and the public `source`.

All clinical thresholds below are **prototype interpretations of public labelling/literature for a
portfolio demo. Not clinical advice.**
