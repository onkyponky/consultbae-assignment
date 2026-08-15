# consultbae-assignment

Merging three overlapping people-databases (recruitment, gig workers, CBNexus)
into one clean database, then automating on top of it.

## Layout

```
data/    the three source CSVs, exactly as received (never modified in place)
src/     ingestion + matching pipeline
db/      SQLite database (generated, git-ignored)
```

## Status

- [x] Phase 1 — repo setup, schema, raw staging ingestion
- [ ] Phase 2 — matching + merge into canonical person records
- [ ] Phase 3 — audio collection app
- [ ] Phase 4 — n8n automation

## Setup

```bash
python -m venv .venv
source .venv/Scripts/activate    # Windows (Git Bash); use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

## Matching

No single ID is shared across the three files. Source 1 is the bridge: it
carries both email and phone. Source 1 joins source 2 on email, and source 3
on phone. Sources 2 and 3 share only names, which is not a key — this data
contains three different Arjun Mehtas and two different Deepak Nairs, so
name-only look-alikes go to `person_review_candidate` and are never merged.

Names on records that appear in only one file keep that source's own casing,
which is why `MANISH BHATIA` appears in caps in `person`. This is a decision,
not an oversight: title casing would invent a canonical form for a name seen
only once, and `.title()` corrupts real names (`McDonald` → `Mcdonald`,
`van der Berg` → `Van Der Berg`).

## Data issues report

See [Task 4 write-up](#) — populated as the pipeline is built.