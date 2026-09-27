# Classification architecture: as implemented

An audit of the EcoGuard classification pipeline against the intended architecture, with the
discrepancies that remain. Written after inspecting the code, not from the design notes.

Known-good baseline that must not regress:

```
SpeciesNet:   5.0.5
Model:        speciesnet-5.0.5-google-speciesnet-pyTorch-v4.0.3b-1-full_image
State:        ready
Prediction:   state=completed species="african elephant" confidence=0.9932 boxes=[]
```

Measured through the running Docker stack (`localhost:8000`, CPU, 2048x1536 fixture):

| Step | Cold | Warm |
| --- | --- | --- |
| Model load, once per process | 30.7 s | — |
| Evidence upload | 0.45 s | 0.45 s |
| `POST /predictions` to result | 5.19 s | 3.30 s |

`/config` reported `ready` within one poll of the load completing, confirming the state is
no longer masked by the response cache.

## 1. Where each concern lives

| Concern | Location |
| --- | --- |
| AI configuration | `app/config.py` (`image_assistance`, `speciesnet_*`, `confidence_threshold`, `model_version`) |
| Enable/disable switch | `app/config.py:50` `Settings.ai_enabled` — `image_assistance != 'disabled'` |
| Model path / reference | `SPECIESNET_MODEL`, `SPECIESNET_LOCAL_DIR`, `SPECIESNET_CACHE_DIR` |
| Model initialisation | `app/ai.py:185` `_load()` — once per process, guarded by a lock |
| Startup warmup | `app/ai.py:229` `warmup()`, started from `app/main.py` lifespan |
| Label → species name | `app/ai.py:58` `_display_label()` |
| Non-species guard | `app/ai.py:45` `NON_SPECIES_LABELS` |
| Threshold decision | `app/ai.py:274` in `infer()` |
| Prediction request | `app/evidence.py:57` `POST /api/v1/predictions` |
| Prediction execution | `app/jobs.py:12` `process_job()`, kind `prediction` |
| Persistence | `app/models.py` `Prediction`; written in `app/jobs.py:23` |
| Idempotency | `app/evidence.py:62` — evidence + `model_version` lookup |
| `/api/v1/config` | `app/workspace.py` `config()` |
| Admin status | `app/workspace.py` `GET /admin/image-assistance` |
| Storage | `app/storage.py`, `media-data` volume |
| Model storage | `model-data` volume at `/app/var/models` (`KAGGLEHUB_CACHE`) |
| System libs | `Dockerfile`: `libgl1`, `libglib2.0-0`, `libgomp1` |
| Python pins | `requirements-ml.txt`, incl. `setuptools==79.0.0` |
| Logging | `app/logging_config.py`, called from `app/main.py` and `app/worker.py` |

## 2. Verified behaviour

- **Frontend never runs the model.** The browser only calls `/api/v1/evidence` and
  `/api/v1/predictions`. There is no model code in the frontend bundle.
- **Loaded once, reused.** `_load()` holds `_lock`, returns the cached instance, and only
  then touches the network or the filesystem. Concurrent first requests block instead of
  loading twice. Covered by `test_the_classifier_is_loaded_only_once`.
- **No per-request download.** `KAGGLEHUB_CACHE` points at a Docker volume, and the model
  directory is pre-seeded. A prediction only reads the cached weights.
- **CPU only.** `SPECIESNET_DEVICE=cpu` and the `+cpu` torch/torchvision wheels. No CUDA.
- **Never fabricates geometry.** `boxes` is `[]` on every result path, including
  `completed`. The frontend has no box overlay.
- **Confidence is a score, not truth.** The API stores the raw probability; wording
  ("candidate", "AI confidence") lives in the UI. `reports.species` is only ever written by
  a human. Covered by `test_report_species_is_never_overwritten_by_the_prediction`.
- **Failures degrade honestly.** Model problems return `unavailable` with a generic message;
  paths, credentials and tracebacks stay in operator logs.

## 3. Discrepancies

### 3.1 Resolved in this change

**a. Startup was silent (the reported problem).** `app/ai.py` called `log.info` on a
successful load, but *nothing configured logging*. The root logger had no handler, and
Python's last-resort handler only emits WARNING and above, so a successful model load
produced no output at all. The `log.info` calls existed and were invisible.

Fixed by `app/logging_config.py`, which attaches a stdout handler to the `app` logger tree
only — the root logger is left alone so boto3/urllib3/matplotlib do not flood the log.
`_load()` now emits a greppable summary, and `warmup()` emits the end state:

```
[AI] loading SpeciesNet whole-image classifier: state=loading model=... variant=full_image device=cpu
[AI] SpeciesNet loaded successfully: model=... checkpoint=4.0.3b labels=2498 device=cpu load_time=24.03s image_assistance=ready
[AI] warmup finished: loaded=True model=... image_assistance=ready
```

Failures log `error=<ExcName> image_assistance=degraded` plus the traceback.

**b. `/api/v1/config` could report a stale state.** The handler computed `ai.status()` and
then threw the result away: the whole payload was wrapped in `cache.cached('config', 15, ...)`,
so for up to 15 s after the model became ready the endpoint still said `degraded`. A related
detail: `cache.touch()` invalidates the prefix `config:`, but the key is `config`, so the
entry was never actually invalidated. Now only the static fields are cached and the
image-assistance state is read live on every call.

**c. `status()` could raise.** `_runtime_installed()` called `importlib.util.find_spec`,
which raises `ValueError` when the module is in `sys.modules` without a `__spec__`. That
turned a status read into a 500 on `/api/v1/config` and `/api/v1/predictions`. The probe is
now defensive.

**d. The result was never audited.** `prediction.requested` was audited
(`app/evidence.py:67`), but nothing recorded the outcome, so the trail showed that
assistance had been asked for without showing what it returned — despite "every prediction
request *and result* is auditable" being a requirement. `app/jobs.py` now writes
`prediction.completed` on success and `prediction.failed` on terminal failure, following the
existing `audit(db, user, action, target_id)` pattern.

Two deliberate choices:

- **The audit row references the `Prediction`; it does not copy the result.** `state`,
  `species`, `confidence` and `model_version` stay in one place, matching how
  `evidence.uploaded` already works. Copying them would let the trail and the record drift.
- **Only the terminal failure is audited.** `process_job` retries up to 5 times, so auditing
  each attempt would leave up to five rows describing one prediction. Intermediate retries
  write nothing. Covered by `test_a_terminal_failure_is_audited_once_and_retries_are_not`,
  which also asserts the exception text never reaches `outbox.last_error`.

The actor is the evidence owner — the user on whose behalf the classification ran — so the
trail is queryable per user. Background jobs have no session, so attribution has to be
resolved from the evidence row.

### 3.2 Reviewed and accepted as-is

These three were raised in the audit and reviewed on 2026-09-27. The decision was to keep
the current behaviour in all three cases, so they are recorded here as known limitations
rather than as pending work. They are not defects.

**e. State vocabulary differs from the spec.** The spec lists
`disabled | not_configured | loading | ready | error`. The API exposes three:

| API state | Covers |
| --- | --- |
| `not_configured` | `IMAGE_ASSISTANCE=disabled` |
| `ready` | a classifier is loaded in this process |
| `degraded` | loading, weights absent, runtime absent, or a previous load failure |

`degraded` therefore conflates `loading` with `error`, which is why the operator reason lives
in `GET /admin/image-assistance` rather than in `/config`. Splitting `degraded` into
`loading` and `error` is a public contract change that would need coordinated frontend work
and would break the `ready`/`degraded` checks already in `Community.tsx` and
`Workspace.tsx`. **Accepted as-is.** The `loading` phase is visible in logs, the reason is
one authenticated call away, and the frontend already renders `degraded` correctly.

*Revisit if* a consumer needs to distinguish "still warming up" from "genuinely broken"
programmatically — for example, a monitoring check that should not page during the 30 s
startup window.

**f. There is no EcoGuard species registry or Uganda validation layer.** The spec asks for
a controlled mapping between SpeciesNet output and "EcoGuard supported species", kept in one
place. What exists today is deliberately thin:

```
label "<uuid>;<path>;<common name>"  ->  _display_label()  ->  trailing common name
                                    ->  NON_SPECIES_LABELS ->  unknown, never a species
```

That is one controlled location (`app/ai.py`) and it is honest — nothing outside SpeciesNet's
vocabulary is invented — but there is **no allow-list of Ugandan/African species**. A
SpeciesNet label such as `red fox` or `coyote` would be stored verbatim as a candidate,
because the model is a global camera-trap classifier. Nothing is silently discarded, and a
reviewer must confirm, so this is not a correctness bug; it is a missing domain constraint.
Adding a registry means curating a species list, deciding on a fallback for unlisted
labels, and deciding whether an unlisted high-confidence result is `completed` or `unknown`.

**Accepted as-is** — no authoritative Ugandan species list is available yet, and inventing
one would be worse than not having it. The safety property holds: the model cannot state a
species as fact, and a human confirms every report.

*Revisit if* a curated species list becomes available, or if non-Ugandan results start
reaching submitted reports. When it does, the single place to change is
`NON_SPECIES_LABELS`/`_display_label` in `app/ai.py`.

**g. Predictions are not linked to reports.** `Prediction.evidence_id` links to evidence,
and evidence can attach to a report, so traceability exists transitively, but there is no
`prediction_id` on `Report`. A report cannot show which exact model run informed it.

**Accepted as-is** — transitive traceability satisfies the audit requirement, and this would
be a schema migration against a live database for a reporting convenience.

*Revisit if* a report ever needs to display the model output that informed it, e.g. an
audit view answering "what did the AI suggest for this report, and did the reviewer agree?"
