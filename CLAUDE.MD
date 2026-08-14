# ConsultBae AI Automation Assignment
Three messy CSVs from three different systems get merged
into one database, plus an n8n automation and a mini audio collection web app.

I have to defend every line of this live on a call. If I cannot explain it
out loud, it does not belong in the repo.

## Stack, do not substitute
Python 3.11
SQLAlchemy ORM over SQLite
pandas for CSV loading only
FastAPI plus Jinja2 templates for the web app
Vanilla JS and the MediaRecorder API for browser recording
ffprobe and pydub for audio metadata

Not allowed: Django, Streamlit, React, Next.js, any ORM other than
SQLAlchemy, any fuzzy matching library.
If you think another library is needed, say why and wait for my answer.
Never install something silently.

## How to work with me
Stop after each numbered step and wait for approval before continuing.
Propose schema and logic in plain text before writing code, never after.
Change only what I asked for. Do not refactor code I did not mention.
Keep functions short, single purpose, and commented in plain language.
No clever one-liners. Readable beats compact.
If something is ambiguous, ask instead of assuming.

## Data rules
Source 1 (Naukri) is the bridge file: it has both email and phone.
Source 1 joins source 2 on email. Source 1 joins source 3 on phone.
Sources 2 and 3 share only names, which is weak evidence.

Never auto merge people on name similarity. The data contains two different
Arjun Mehtas and two different Deepak Nairs. Name only candidates go into a
review table, never straight into the merged person table.

Staging tables store every value as TEXT, untouched. No date parsing, no
numeric casting at ingestion. The raw value must survive so it can be quoted
in the data issues report.

Every cleaning, matching, skipping or merging decision writes a row into
data_issues (source_file, row_number, issue_type, raw_value, action_taken,
detected_at). That table is a graded deliverable, treat it as important as
the code.

## Repo layout
/data      the three source CSVs
/src       pipeline and app code
/db        SQLite file, gitignored
/uploads   submitted audio, gitignored

## Commits
Small and frequent, plain English messages. The reviewer reads commit
history. No single giant commit at the end.

## Phases
1. Repo, schema, raw ingestion (no cleaning)
2. Normalisation and matching, merged person table
3. Audio collection app with duration, sample rate, bitrate, loudness
4. n8n flow, exported JSON
5. README, data issues report, stuck log, scaling note, video