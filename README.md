# ConsultBae — AI Automation Assignment

Three CSV exports of overlapping people, from three different systems, merged
into one clean database — plus an n8n automation and an audio collection app
built on top of it.

The source data is deliberately messy. Finding and handling the problems is as
much the deliverable as the pipeline, so the full write-up lives in
**[DATA_ISSUES.md](DATA_ISSUES.md) — 18 distinct problems found**, with what
was done about each and what would have broken had it been missed.

| | |
|---|---|
| **Data issues report** | **[DATA_ISSUES.md](DATA_ISSUES.md)** |
| **Stuck log** | **[below](#stuck-log)** |
| **Scaling note** | **[SCALING.md](SCALING.md)** |
| **Video walkthrough** | _link to be added_ |

- [x] Phase 1 — repo, schema, raw ingestion
- [x] Phase 2 — normalisation, matching, merged person table
- [x] Phase 3 — audio collection app
- [x] Phase 4 — n8n duplicate-check flow
- [x] Phase 5 — data issues report, stuck log, scaling note
- [ ] Video

---

## What this is

Three files, no shared ID, overlapping people:

| File | Columns | Keys it carries |
|---|---|---|
| `source1_naukri_applicants.csv` | 8 | **email and phone** |
| `source2_gig_workers.csv` | 6 | email only |
| `source3_cbnexus_contacts.csv` | 5 | phone only |

**105 rows read → 102 staged → 60 people.**

Three deliverables sit on that database: a merge pipeline, an n8n flow that
checks an incoming CSV against it for duplicates, and a web app that collects
audio recordings and attaches them to the same people.

---

## Setup from a clean clone

**Prerequisites:** Python 3.14 and **ffmpeg** (which provides `ffprobe`). The
audio app shells out to both — there is no Python audio library in this
project.

```bash
# ffmpeg — must be on PATH
winget install Gyan.FFmpeg          # Windows
# brew install ffmpeg               # macOS
# sudo apt install ffmpeg           # Debian/Ubuntu

ffprobe -version                    # verify before continuing
```

```bash
git clone <repo-url>
cd consultbae-assignment

python -m venv .venv
.venv\Scripts\activate              # Windows
# source .venv/bin/activate         # macOS/Linux

pip install -r requirements.txt
```

---

## Running the three parts

### 1. The pipeline

```bash
python src/ingest.py     # load all 3 CSVs into staging, verbatim
python src/merge.py      # normalise, match, build the 60 canonical people
```

`ingest.py` prints rows read / loaded / skipped per file; `merge.py` prints
people, links, review candidates and logged issues. Both are **idempotent** —
they clear what they own first, so the database is a pure function of the CSVs
and can be rebuilt at any time.

```bash
python -m pytest tests/ -q          # 253 tests
```

### 2. The audio collection app

```bash
python -m uvicorn app:app --app-dir src --port 8000
```

- `http://127.0.0.1:8000` — enter name and phone, **record in the browser or
  upload a file**, submit
- `http://127.0.0.1:8000/submissions` — every submission with a play button,
  duration, sample rate in kHz, bitrate, loudness in dB and LUFS, and a rough
  noise estimate

Try phone `+91-9000000254` to attach to a person the merge already built, and
any unused number to watch the app create one.

### 3. The n8n duplicate-check flow

The flow calls a **read-only** lookup endpoint on the app, so the app must be
running and bound to `0.0.0.0` — n8n runs in a container and cannot reach a
localhost-only bind:

```bash
python -m uvicorn app:app --app-dir src --host 0.0.0.0 --port 8000
docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n
```

Open `http://localhost:5678`, import [`n8n/workflow.json`](n8n/workflow.json),
click **Execute workflow**, then post the test CSV:

```bash
curl.exe -F "file=@n8n/test_incoming.csv" http://localhost:5678/webhook-test/duplicate-check
```

[`n8n/test_incoming.csv`](n8n/test_incoming.csv) contains a deliberate mix: two
exact duplicates, one row matching **only after phone normalisation**
(`+91-9000000287`, no email), and two genuinely new people. Expected response:
`3 of 5 incoming rows already exist in the database.`

The endpoint answers one question — *does this person exist?* The CSV parsing,
the per-row loop, the branch and the alert are all n8n nodes. Deliberately:
the brief scores a pure-code solution zero.

---

## Architecture

```
data/*.csv  ->  raw_source{1,2,3}_*  ->  person + person_skill
 (verbatim)      (TEXT, untouched)        (canonical, typed)
                        |                        |
                        +--> data_issues         +--> person_source_link
                             (every decision)         (traces back to rows)
                                                 +--> person_review_candidate
                                                 +--> audio_submission
```

**Staging is inviolate.** Every staging column is `TEXT` and holds the original
value — no strip, no casefold, no type conversion. Trailing whitespace in
`'Noida '` and the string `'4.2'` are *evidence*; if ingestion normalised them,
the analysis depending on them would not be defensible. A row either lands
verbatim or is skipped and logged.

**Raw values survive beside normalised ones** wherever producing the clean
value required an assumption: `ctc_raw` / `ctc_source_unit`, `rate_raw` /
`rate_period`, `city_raw`, `applied_date_raw`, `bitrate_is_derived`.

**`person_source_link` has `UNIQUE(source_file, source_row_number)`**, so each
source row belongs to exactly one person and double-counting is a constraint
violation rather than a silent bug.

### Why SQLite with SQLAlchemy

SQLite because the whole database is a single file that rebuilds from the CSVs
in under a second, with no server to install before a reviewer can run
anything. The dataset is 102 rows — a database server would be infrastructure
without a purpose.

SQLAlchemy because the schema is the argument. Declared models put the
`CHECK` constraints (`severity IN ('skipped','repaired','flagged')`), the
`UNIQUE` link constraint and the foreign keys in one readable file, and the
same models run the app and the tests. Nothing here depends on SQLite
specifically — the same models would move to Postgres by changing the URL.

### Why the stdlib `csv` module and not pandas

A forgiving parser dtype-guesses `Current CTC` into floats, hiding that the
column mixes absolute rupees (`417964`) with lakhs per annum (`4.2`). It also
accepts the rotated row at `source2_gig_workers.csv:20` as ordinary data,
because that row has a valid field count and is wrong only in its field
*contents*. Both are findings that had to surface, not be handled for us.

---

## Matching logic

No single ID is common to all three files. **Source 1 is the bridge** — it is
the only file carrying both an email and a phone.

```
source1  <--email-->  source2      15 of 30 rows match
source1  <--phone-->  source3      25 of 30 rows match
source2  <-- ??? -->  source3      no shared key at all
```

The pipeline runs in this order, and the order is load-bearing:

1. **Deduplicate source1 against itself first.** It is the bridge; if one
   person is still two rows when the other files attach, both copies collect
   links and the error propagates into everything downstream. 42 rows → 40
   people.
2. Seed `person` from those 40.
3. Attach source2 on normalised email, source3 on normalised phone.
4. Rows matching nothing become their own person (+15, +5 = **60**).
5. Name-only look-alikes go to `person_review_candidate`, never merged.

Every link records a `match_method` and `match_confidence`, so any merge can be
defended after the fact.

### Why phone, not email, deduplicates source1

Source 1 contains two duplicate pairs, and they are **caught by different
keys**:

```
line 25  R. Verma       rohit.verma13@mailtest.example.org   9000000294
line 31  Rohit Verma    rohit.verma13@mailtest.example.org   9000000294   <- same email AND phone

line 27  Nikhil Chopra  alt.nikhil.chopra70@example.com      09000000103
line 37  Nikhil Chopra      nikhil.chopra70@example.com      09000000103   <- same phone, DIFFERENT email
```

The Verma pair is found by either key. **The Chopra pair is found only by
phone** — the `alt.` prefix makes the two emails genuinely different strings,
so deduplicating on email leaves that pair as two people and every later join
inherits the split. Phone catches both pairs; email catches one.

### Why names are never a key

The data contains **three different Arjun Mehtas** and **two different Deepak
Nairs**. Matching on name similarity fuses distinct people into one record
carrying someone else's contact details. Name-only look-alikes are recorded in
`person_review_candidate` with `resolution='unresolved'` — not merged, not
dropped, and visible as a decision rather than an omission.

---

## Repo layout

```
data/       the three source CSVs, as received, never edited in place
src/        ingest.py  merge.py  normalise.py  audio_meta.py  app.py  models.py
tests/      253 tests
n8n/        workflow.json + test_incoming.csv
db/         SQLite file (generated, git-ignored)
uploads/    submitted audio (git-ignored)
```

---

# Stuck log

Three places this genuinely stopped me, what I tried, and what I threw away.

## 1. The audio recording that looked like it had captured nothing

I built the recording page, clicked **Start recording**, spoke, clicked **Stop**
— and the preview player showed `0:00 / 0:00`. Pressing play did nothing. My
first assumption was that `MediaRecorder` had captured silence, so I went
looking at microphone permissions and constraints.

That was the wrong place to look. The page was also printing
`Recorded 67 KB. Ready to submit.` and the file input was holding
`recording.webm`. **The audio had been captured the whole time.** Only the
preview was broken. That reframing was most of the fix — I had been debugging
capture when the bug was in playback.

The cause: `MediaRecorder` writes a **live stream**. It cannot know the length
until you stop, and it never goes back to patch the header. So the WebM file
has no duration field, `<audio>` reads `duration === Infinity`, and it renders
a dead seek bar.

**What I asked the LLM:** I described the symptom exactly as I saw it —
*"trying to record audio but nothing happens"* — and sent a screenshot of the
page. That framing was itself part of the problem: I had already decided it
was a recording failure, and I had to be shown that the page was reporting
`Recorded 67 KB` at the same time as the player showed `0:00`.

Once it was clear the capture had worked, the question became the right one:
why does an `<audio>` element show `0:00 / 0:00` for a `MediaRecorder` blob
that demonstrably has bytes in it.

Later I asked how to check whether the microphone was capturing anything at
all rather than silence — *"I said 12345678, is there a way to check it
somewhere or is it a problem?"* The answer was to read the extracted numbers
back, which is the check described under "how I tested it" below.

**What I rejected:** the first suggestion was to re-encode the blob in the
browser to stamp a duration into it. That means shipping a WASM encoder to the
client to fix a display bug, on a page whose whole point is to be small. I
also rejected setting a duration manually from a timer I ran during recording
— that number is my stopwatch, not the file's, and it would drift from the
truth the moment the two disagreed.

**What worked:** seek past the end (`currentTime = 1e101`). The browser cannot
honour that without decoding to the real end, and in doing so it learns the
true duration. Then rewind to `0`.

**Then the same problem hit the server.** When I ran `ffprobe` over a
browser-shaped recording, this is what came back:

```
stream.bit_rate  : ABSENT
stream.duration  : ABSENT
format.bit_rate  : ABSENT
format.duration  : ABSENT
format.size      : 30580        <- only this and sample_rate survived
```

Same missing header, different consumer. I proved it by generating the same
audio twice — once written to a file, once written to a pipe. The file version
had a bitrate in the container; the piped version had nothing. `MediaRecorder`
behaves like the piped version.

**What I rejected here:** pydub and librosa. pydub only wraps the same ffmpeg
subprocess calls I was already making, and librosa pulls in numpy and numba to
recompute what `astats` reports in a single pass. Neither can invent a duration
that is not in the file. I also rejected filling in the codec's nominal bitrate
(64 kbit/s) as a stand-in — the measured container rate was about 81 kbit/s, so
that guess would have been both wrong and invisible.

**How I tested it:** `tests/test_audio_meta.py` generates the audio with ffmpeg
at test time and reads it back through the real binaries — no mocks, because a
mock would only assert that my parser agrees with my own guess about ffmpeg's
output. One test asserts the *premise*: that a pipe-written WebM really does
carry no duration and no bitrate. If a future ffmpeg starts populating those,
that test fails loudly instead of the fallback quietly becoming dead code.

The bitrate is stored with `bitrate_is_derived = true` and a note containing
the actual arithmetic, so nobody later mistakes a computed number for a
declared one.

## 2. Picking the key that deduplicates source 1

Source 1 is the bridge file, so it has to be deduplicated against itself
*before* the other two files attach to it. If one person is still two rows at
that point, both copies collect links and the error spreads into everything
downstream.

Email looked like the obvious key. It is wrong, and the file says so:

```
line 25  R. Verma       rohit.verma13@mailtest.example.org   9000000294
line 31  Rohit Verma    rohit.verma13@mailtest.example.org   9000000294

line 27  Nikhil Chopra  alt.nikhil.chopra70@example.com      09000000103
line 37  Nikhil Chopra      nikhil.chopra70@example.com      09000000103
```

The Verma pair is caught by either key. **The Chopra pair is caught only by
phone** — that `alt.` prefix makes the two email strings genuinely different,
so deduplicating on email leaves him as two people.

**How I worked it:** I set the constraint up front, before any code — names
must never merge people, because I had already spotted two different Arjun
Mehtas and two different Deepak Nairs while reading the files. What I did not
know was which key should deduplicate source 1 against *itself*.

I asked for the merge logic to be written out in plain text before anything
was implemented, so I could check the key choice against the actual rows
rather than discover it afterwards in code. That review is where the `alt.`
prefix surfaced, and it changed the answer — I had assumed email.

**What I rejected:** fuzzy matching on names, which was the first thing
suggested and is the trap this dataset is built around. The data contains
**three different Arjun Mehtas and two different Deepak Nairs**. Any
similarity threshold that merges the real duplicates also merges those, and
the merged record then carries another person's phone number. I also rejected
matching on name plus city as a tie-breaker — Arjun Mehta appears twice in
Noida, so it does not even separate the specific case it was proposed for.

**What worked:** phone catches both pairs; email catches one. So source 1 is
grouped by normalised phone. People who can only be linked by name go into
`person_review_candidate` with `resolution = 'unresolved'` — not merged, not
dropped, and visible as a decision rather than an omission.

**How I tested it:** `tests/test_merge.py` asserts the counts *and their
decomposition* — 60 people is only correct if it is also 40 + 15 + 5, because
the total alone would still pass if source 1 under-deduplicated by one and an
orphan went missing by one. Separate tests assert that three Arjun Mehtas and
two Deepak Nairs survive as distinct people, each with a different key.

## 3. Getting n8n to read a local SQLite file

I had never built anything in n8n before this. I knew roughly what it was, but
I had never wired a flow.

The first wall was immediate: **n8n has no SQLite node.** My data is a local
file, and there is nothing in the standard node set that opens one.

**What I asked the LLM:** first, how to connect an n8n flow to a local SQLite
database. Then, once the design was settled, something more basic —
*"I don't know how n8n works, explain it to me step by step, what I have to
put in there and what I should expect."* I was driving the browser myself, so
I needed the concepts (nodes, items, the trigger, test URL versus production
URL) before the clicking meant anything.

When the flow did not respond I also asked flatly *"app not working, what can
we do"*, which turned out to be the `0.0.0.0` misunderstanding described
below.

**What I rejected:**

- A community SQLite node — an unvetted dependency reaching directly into a
  file the app owns.
- Migrating to Postgres so a database node would exist. That is infrastructure
  added for 102 rows, and it changes Task 1 to suit Task 2.
- Doing the duplicate check in Python and calling it done. The brief scores
  pure-code solutions zero, and it would have been the wrong shape anyway.

**What worked:** a read-only `/api/lookup` endpoint on the FastAPI app I had
already built. It answers exactly one question — *does a person with this phone
or email exist?* — and nothing more. The CSV parsing, the per-row loop, the
branch and the alert are all n8n nodes. The endpoint reuses `normalise_phone`
and `normalise_email` from Phase 2, so there is no second matching rule that
could drift from the one that built the database.

**Then a second wall inside the same problem.** With n8n running in Docker, the
flow could not reach the app. `127.0.0.1` inside a container is the *container*,
not my laptop. That needed two changes: `host.docker.internal` as the host in
the HTTP node, and starting uvicorn with `--host 0.0.0.0` so it accepts
connections from outside the machine's own loopback.

That flag then caused its own small trap: uvicorn prints
`Uvicorn running on http://0.0.0.0:8000`, which looks like a link. It is not —
`0.0.0.0` is a bind address meaning "every interface", not somewhere a browser
can go. I spent a few minutes convinced the app was broken before checking with
`curl` and finding it answered `200` on `127.0.0.1:8000` the whole time.

**How I tested it:** I ran every row of `n8n/test_incoming.csv` through the
endpoint with curl before wiring any nodes, so I knew the answers were right
before debugging the flow. The test CSV is built with a deliberate mix — two
exact duplicates, one row that matches **only** after phone normalisation
(`+91-9000000287`, no email), and two genuinely new people. I also checked the
person count before and after the whole run to confirm the endpoint really is
read-only: 60 people before, 60 after.
