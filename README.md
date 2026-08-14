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

## Data issues report

See [Task 4 write-up](#) — populated as the pipeline is built.