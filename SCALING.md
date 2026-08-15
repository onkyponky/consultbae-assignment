# Launching to 5,000 gig workers over one weekend

What breaks, in the order it breaks, and what I would change first. Every
item below refers to code that is actually in this repo.

## The load is not 5,000 over 48 hours

A notification goes out and people respond in a burst. Assume 40% arrive in
the first three hours: ~2,000 submissions, averaging 0.2/second but peaking
at 10–15 concurrent. That peak is what decides whether this survives, not
the weekend total.

---

## What breaks first

**1. Memory, on a sharper edge than the 25 MB cap suggests.**
`validate_audio()` runs `upload.file.read()` — the entire file into RAM — and
only *then* compares against the limit. The cap rejects an oversized file
**after** paying its full memory cost. Ten concurrent 25 MB uploads is 250 MB
of transient RAM on top of the interpreter, which is already past a 512 MB
free-tier container. A single deliberate 500 MB upload kills the process
before validation runs at all. This is a correctness bug rather than a
scaling one, and it is the first thing I would fix.

**2. The request handler does the audio work inline.** `submit()` calls
`extract()`, which spawns **three to four ffmpeg subprocesses** — probe,
loudness, levels, and a decode pass for duration when the container declares
none, which is every browser recording. Each decodes the whole file.

I measured this rather than guessing: a 60-second Opus clip costs **0.43 s**
of CPU end to end, and `astats` alone is 0.34 s of that. That is far cheaper
than I assumed before measuring, and it changes the conclusion — this is not
what falls over first on a normally-provisioned box. It still matters on a
free tier with a fraction of a vCPU, where 0.43 s of CPU becomes several
seconds of wall time and requests queue behind each other. The reason to move
it out of the request is that it makes every submission's latency depend on
machine load, and that latency is what causes the duplicate-submission
problem described below.

**3. SQLite serialises writers.** The database is in `journal_mode=delete`,
not WAL, so one writer at a time and readers block during writes. Python's
driver waits 5 seconds then raises `database is locked`. Concurrent
submissions will hit this, and every one that does loses a recording the
worker believes they submitted.

**4. The filesystem is the wrong place for the files.** Uploads go to a local
`uploads/` directory. On Render, Railway or any container host that is
**ephemeral** — a redeploy or restart discards it. The database rows survive
and point at files that no longer exist. That is silent data loss, and it is
the failure I would least like to explain to 5,000 people.

**5. `/submissions` has no pagination.** It selects every row and renders an
`<audio>` element per submission. At 5,000 rows the page is unusable, and
it is the page an ops person would open first to check whether the launch is
working.

**6. Nothing stops abuse.** No auth, no rate limit, no CAPTCHA. One script
can fill the disk, and every request costs four ffmpeg invocations.

---

## Duplicates specifically

The brief asks about duplicates, and there are three different kinds here.

**The same person submitting twice** is the common one, and it is caused by
our own latency: a submission takes seconds, the user sees nothing happening,
they press Submit again. Two files, two rows, one person. There is no
idempotency key, so nothing detects it.

**Two people sharing a phone** — a shared family handset — merge into one
person, because phone is our matching key. The audio then attaches to the
wrong human.

**A typo creates a person.** Every unrecognised phone creates a `Person` with
`source_origin='audio_app'`. That is deliberate and right at small scale, but
at 5,000 submissions the `person` table becomes mostly app-created rows, and
a mistyped digit is indistinguishable from a genuinely new worker.

Underneath all three: **a submitted phone number is a claim, not proof.**
Nothing verifies the number belongs to the person typing it.

---

## What I would change before launch

**Enforce the size limit before the body is read**, at the web server or proxy,
so an oversized upload is rejected at the edge instead of in Python after it
has already been loaded into memory. This is the cheapest fix on the list and
closes the only outright bug.

**Take ffmpeg out of the request.** Accept the upload, store it, write the row
with `status='pending'`, return immediately. A background worker does the
extraction. The gain is not raw throughput — 0.43 s is affordable — it is that
submission latency stops depending on machine load, which is what drives
users to press Submit twice. The schema already tolerates it: every metadata
column is nullable and `probe_error` exists for failures.

**Upload straight to object storage.** Presigned S3/R2 URLs so the file goes
browser → bucket without transiting the app. That removes the memory problem
and the bandwidth bottleneck together, and makes storage durable instead of
ephemeral. The app then stores a key, not a path.

**Move to Postgres.** Concurrent writers, and the change is a connection URL
plus a migration because the models are already SQLAlchemy. Nothing in the
schema is SQLite-specific.

**Add an idempotency key.** A UUID generated in the browser, sent with the
form, unique-constrained. A double-tap or a retry after a timeout then lands
once instead of twice.

**Paginate `/submissions`**, and put an index on `created_at`.

**Verify the phone with an OTP** if identity actually matters for payment. If
it does not, say so explicitly rather than implying the number is trustworthy.

---

## Cost

Storage is not the problem people expect it to be. 5,000 recordings averaging
1.5 MB is about **7.5 GB** — roughly **$0.17/month** on S3. Even ten weekends
of this is lunch money.

The costs that actually matter:

- **Compute.** At the measured 0.43 s per clip, 5,000 submissions is about
  **36 CPU-minutes** in total — genuinely cheap. The cost is not the total,
  it is that it arrives in a spike: paying for enough instance to absorb the
  peak, all weekend, to use half an hour of CPU. A queue with workers that
  scale to zero turns that into pennies.
- **Egress.** Storage is cheap, serving it back is not — S3 egress runs about
  $0.09/GB. If reviewers stream every clip once, that is another 7.5 GB.
  Playback, not collection, is what grows the bill over time.
- **Retries.** Gig workers are on mobile data with flaky connections. A failed
  25 MB upload that the user retries three times costs three times the
  bandwidth and delivers one recording. Resumable or chunked uploads pay for
  themselves here.

---

## What I would leave alone

The **matching logic**. Phone normalisation and the refusal to merge on names
are correct at any scale — 5,000 people make name collisions *more* likely,
not less, so `person_review_candidate` matters more at launch than it does
now.

The **data issues table**. It costs one insert per decision and it is the only
record of why anything was done.

The **ffmpeg-over-a-library choice**. The subprocess calls are the bottleneck,
but moving them to a background worker fixes that. Swapping in a Python audio
library would trade a well-understood cost for a less-understood one and still
decode the same bytes.
