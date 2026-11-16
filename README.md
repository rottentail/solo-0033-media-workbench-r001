# Media Workbench

A trusted-operator HTTP service for assembling sequential media into normalized MP4s. It supports ordered timeline edits, frozen exports, persistent render jobs, verified segment reuse, cancellation and recovery after a worker process disappears. Video is normalized to 640×360 at 25 fps; audio is stereo 48 kHz. Silent clips receive a bounded silent track. Each occurrence retains its own trim and gain even when the source is reused.

## Runtime

Python 3.10+ on Linux or macOS, SQLite and FFmpeg/FFprobe with libx264/AAC support. The segment cache uses POSIX advisory locks. Set `MEDIA_FFMPEG` and `MEDIA_FFPROBE` to executable paths when needed. Start with `python3 -m mediaworkbench.server`; `MEDIA_PORT` (default 8765) and `MEDIA_DATABASE` (default `media-workbench.sqlite3`) configure the service. Keep it bound to loopback: there is no authentication, and the operator may read and write paths accessible to the process.

Existing databases are extended with job and checkpoint tables on opening. Original assets, projects, clips and render records remain readable. Old renders have no resumable job metadata and are available through the original inspection route.

## Timeline and synchronous export

Send JSON objects to these routes:

- `POST /assets`: `{"path":"/absolute/source.mp4"}` probes the media and records its content hash.
- `POST /projects`: `{"name":"Example"}` creates an empty timeline.
- `POST /clips`: `{"project_id":1,"asset_id":1,"start_ms":0,"end_ms":1000,"gain":0.5}` appends an occurrence and increments the revision.
- `POST /projects/1/timeline`: `{"expected_revision":1,"clips":[{"asset_id":1,"start_ms":1000,"end_ms":2000,"gain":1}]}` atomically replaces and reorders the whole timeline. A stale revision or invalid occurrence rejects the entire edit. Up to 512 occurrences are allowed per replacement.
- `GET /projects/1` returns one consistent timeline view.
- `POST /renders`: `{"project_id":1,"destination":"/absolute/new.mp4"}` executes a complete synchronous export.
- `GET /renders/1` returns the stored revision, frozen plan, status and output/error. New jobs also include lease and segment progress.

Clip endpoints use integer milliseconds. Gain is a finite number from 0 to 2. A source must remain unchanged after intake. Existing destinations, including dangling symlinks, are rejected. Rendering never replaces another writer's destination.

## Persistent jobs

`POST /render-jobs` accepts the same project and destination fields as `/renders` and returns immediately with a queued, immutable plan. Later timeline edits affect future exports only. A worker is explicitly driven with `POST /render-jobs/1/run` and an empty object `{}`. That request runs synchronously, while other HTTP threads may continue editing or inspecting the job. There is no automatic background scheduler.

`GET /render-jobs/1` inspects progress. `POST /render-jobs/1/cancel` with `{}` cancels a queued/running job and invalidates the claim; active FFmpeg processes stop when the worker next checks its lease. `POST /render-jobs/1/retry` with `{}` requeues a failed job using the same frozen plan; a separate `/run` request resumes it. Cancellation is terminal and completed exports cannot be cancelled.

A live claim excludes other workers. Claims expire after 30 seconds without renewal; after a process disappears, another `/run` request may take over the expired claim. Each acquisition increments an epoch. Heartbeats, checkpoints, failure updates and final publication all require the current, unexpired epoch and owner. Source hashing, FFmpeg execution and cache-lock waits renew the lease without holding a long SQLite write transaction.

The default cache is `.media-workbench-cache` next to the database. A segment identity includes source content, trim, gain, audio presence and the encoding profile version. Shared segments have process locks, atomic metadata replacement and byte checksums. Every cache hit and resumed checkpoint is revalidated; corrupt or missing segments are rebuilt. Repeated occurrences may reuse the same encoded bytes but keep separate positions in the plan and concatenation list. Final assemblies retain unique hidden directories beside the destination so an interrupted publication can be recognized by inode ownership and resumed. A competing destination is preserved. Public output paths are never automatically unlinked, because another process may replace the name between an ownership check and deletion. If a process already linked its artifact before a failure or cancellation, those bytes are retained and the job exposes its artifact intent. An explicit retry may adopt a valid own link; cancellation never marks that file completed. A corrupt published artifact requires operator inspection/removal before retry, and another writer's file is never replaced.

Retain the cache alongside the database while jobs may need recovery. There is no cache eviction API, automatic retry loop or power-loss durability guarantee across filesystem and database. Recovery covers worker process interruption; it does not cover storage hardware failure or malicious mutation of database/cache files. Completed MP4s remain valid if the cache is later removed.

## Modules and validation

`timeline` owns atomic edits and revisions; `planner` owns frozen occurrence identities; `jobs` owns durable claims and checkpoints; `segment_cache` owns encoded bytes and manifests; `worker` coordinates recovery; `publication` joins database ownership with destination ownership. `encoder`, `assets`, `tools` and `server` implement media processing, intake, bounded subprocesses and HTTP access.

Automated test files remain deferred to the later task-verification stage. Bounded executable workflow checks are provided in `mediaworkbench.check`:

```sh
python3 -m mediaworkbench.check
python3 -m mediaworkbench.check --workflow timeline
python3 -m mediaworkbench.check --workflow render
python3 -m mediaworkbench.check --workflow recovery
```

They generate temporary media and databases, exercise real production operations and inspect the resulting MP4 with FFprobe. Independent concurrency, corruption and process-interruption verification is kept outside the delivered baseline.
