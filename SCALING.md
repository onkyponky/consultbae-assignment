# If 5,000 gig workers hit this app over one weekend

I wrote this after going back through my own code and timing a few things,
so every point below is about something that is actually in this repo.

## The load isn't 5,000 spread over 48 hours

That was my first wrong assumption. A notification goes out and people
answer in a burst. If 40% land in the first three hours, that's roughly
2,000 submissions. On average that's 0.2 per second, which sounds like
nothing, but the peak is somewhere around 10 to 15 at once. The peak is
what decides whether this survives. The weekend total is irrelevant.

## What breaks first

**1. Memory, and worse than the 25 MB cap makes it look.**
`validate_audio()` calls `upload.file.read()`, which pulls the whole file
into RAM, and only after that does it compare against the limit. So the cap
rejects an oversized file *after* already paying for it. Ten concurrent
25 MB uploads is 250 MB of transient memory sitting on top of the
interpreter, and a 512 MB free tier container is already tight. Someone
uploading a single 500 MB file kills the process before validation even
runs. I'd call this a bug rather than a scaling problem, and it's the first
thing I'd fix.

**2. All the audio work happens inside the request.**
`submit()` calls `extract()`, which spawns three or four ffmpeg
subprocesses: probe, loudness, levels, plus a decode pass for duration
whenever the container doesn't declare one, which is basically every
browser recording. Each one decodes the whole file.

I assumed this was the main bottleneck, so I timed it before writing this
section. A 60 second Opus clip costs 0.43 s of CPU end to end, and `astats`
alone is 0.34 s of that. Much cheaper than I expected, and it changed my
answer. On a normal box this is not what falls over first. On a free tier
with a fraction of a vCPU it still hurts, because 0.43 s of CPU turns into
several seconds of wall time and requests start queuing behind each other.
The real reason to move it out of the request is that it ties every
submission's latency to how busy the machine is, and that latency is what
causes the duplicate problem further down.

**3. SQLite serialises writers.**
The DB is in `journal_mode=delete`, not WAL, so one writer at a time and
readers block while a write is happening. Python's driver waits 5 seconds
and then throws `database is locked`. At 10 concurrent submissions we will
hit this, and every time we do, someone loses a recording they think they
submitted.

**4. The filesystem is the wrong place for the files.**
Uploads land in a local `uploads/` directory. On Render or Railway or any
container host, that disk is ephemeral. One redeploy and the files are gone
while the DB rows happily point at paths that no longer exist. Silent data
loss, and the one I'd least want to explain to 5,000 people.

**5. `/submissions` has no pagination.**
It selects every row and renders an `<audio>` element for each one. At 5,000
rows the page is unusable, and it's the exact page someone from ops opens
first to check whether the launch is working.

**6. Nothing stops abuse.**
No auth, no rate limit, no CAPTCHA. One script can fill the disk, and every
request costs four ffmpeg invocations.

## Duplicates

The brief asks about duplicates and there are three different things
happening here, which took me a while to separate.

The common one is **the same person submitting twice**, and we cause it
ourselves. Submission takes a few seconds, nothing visibly happens, so they
press Submit again. Two files, two rows, one human. There's no idempotency
key, so nothing catches it.

**Two people on one phone** is the opposite failure. A shared family handset
collapses into one person, because phone is my matching key. The audio then
sits on the wrong human.

**A typo invents a person.** Any unrecognised number creates a `Person` with
`source_origin='audio_app'`. At small scale that's the right call. At 5,000
submissions the `person` table becomes mostly app created rows, and one
mistyped digit is indistinguishable from a real new worker.

Underneath all three: a submitted phone number is a claim, not proof.
Nothing checks that the number belongs to the person typing it.

## What I'd change before launch

Reject oversized uploads at the edge, at the web server or proxy, before the
body reaches Python. Cheapest fix on this list and it closes the only
outright bug.

Get ffmpeg out of the request path. Accept the upload, store it, write the
row with `status='pending'`, return. A background worker does the
extraction. The win isn't throughput, 0.43 s is affordable. The win is that
submission latency stops depending on machine load, which is what makes
people double tap Submit. The schema already allows this: every metadata
column is nullable and `probe_error` exists for failures.

Upload straight to object storage with presigned S3 or R2 URLs, so the file
goes browser to bucket without passing through the app. That kills the
memory issue and the bandwidth issue at once, and the storage stops being
ephemeral. The app then stores a key instead of a path.

Move to Postgres. Concurrent writers, and it's a connection URL plus a
migration since the models are already SQLAlchemy. Nothing in the schema is
SQLite specific.

Add an idempotency key: a UUID generated in the browser, sent with the form,
unique constrained. A double tap or a retry after a timeout then lands once.

Paginate `/submissions` and index `created_at`.

Verify the phone with an OTP if identity actually matters for payment. If it
doesn't matter, say so out loud instead of quietly implying the number is
trustworthy.

## Cost

Storage is cheaper than people expect. 5,000 recordings at roughly 1.5 MB
each is about 7.5 GB, which is around $0.17 a month on S3. Ten weekends of
this is lunch money.

What actually costs something:

* **Compute.** At 0.43 s per clip, 5,000 submissions is about 36 CPU
  minutes total. The problem isn't the total, it's that it arrives in a
  spike, so you end up paying for an instance big enough to absorb the peak
  all weekend just to use half an hour of CPU. A queue with workers that
  scale to zero turns that into pennies.
* **Egress.** Storing is cheap, serving it back isn't. S3 egress is around
  $0.09/GB, so if reviewers stream every clip once that's another 7.5 GB.
  Playback is what grows the bill over time, not collection.
* **Retries.** Gig workers are on mobile data with patchy signal. A 25 MB
  upload that fails and gets retried three times costs three times the
  bandwidth and delivers one recording. Chunked or resumable uploads pay for
  themselves here.

## What I'd leave alone

The matching logic. Phone normalisation plus refusing to merge on names is
right at any scale. With 5,000 people, name collisions get *more* likely,
not less, so `person_review_candidate` matters more at launch than it does
today.

The data issues table. One insert per decision, and it's the only record of
why anything happened.

Using ffmpeg instead of a Python audio library. The subprocess calls are the
slow part, but moving them to a worker fixes that. Swapping in a library
would trade a cost I've measured for one I haven't, and it still has to
decode the same bytes.