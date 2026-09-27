# Image assistance

EcoGuard's image assistance uses **SpeciesNet**, Google's camera-trap wildlife classifier
(released under Apache-2.0 via the `speciesnet` Python package). It produces a *candidate
identification only*. It never decides a report, never writes to `reports.species`, and
never replaces human review.

## What runs

Only the SpeciesNet **image classifier** runs. No object detector is loaded or executed.

| Setting | Value | Meaning |
| --- | --- | --- |
| Package | `speciesnet==5.0.5` | Pin in `requirements-ml.txt` |
| Reference | `kaggle:google/speciesnet/pyTorch/v4.0.3b/1` | Kaggle model id, revision included |
| Checkpoint | `4.0.3b`, `type: full_image` | `info.json` of the downloaded artefact |
| Classifier file | `full_image_88545560_22x8_v12_epoch_00153.pt` | 2,498 labels |
| Backend | EfficientNet V2 M (`22x8`), PyTorch | >2000 labels, trained on 65M+ images |
| Device | CPU by default | `SPECIESNET_DEVICE` overrides |

The `v4.0.3b` variant is the **whole-image** classifier. It needs no bounding boxes, so no
detector stage is involved. The older `v4.0.3a` variant is an *always-crop* model that
expects detector crops and is therefore not used here.

## Measured footprint and speed

Measured on the development host (Windows, CPU only, Python 3.13, `speciesnet==5.0.5`).
These are observations from one machine, not published vendor figures.

| Item | Size |
| --- | --- |
| Classifier weights | 224,391,777 bytes (224.4 MB / 214.0 MiB) |
| Labels, taxonomy, geofence files | ~0.2 MB |
| MegaDetector weights (see note) | 280,767,041 bytes (280.8 MB / 267.8 MiB) |
| **Total model directory** | **512,037,265 bytes (512.0 MB / 488.3 MiB)** |
| Virtual environment with ML extras | ~1,220 MiB |
| `torch` 2.9.1+cpu | 432.6 MiB |
| `opencv-python` (transitive) | 112.4 MiB |

| Step | CPU time |
| --- | --- |
| `import speciesnet` (cold) | ~106 s, first run only (font and label caches) |
| Classifier load, weights already cached | ~49–54 s (host), ~24 s (Linux container) |
| First load including the weight download | ~131 s |
| Inference, 2048 × 1536 JPEG | 3.2–3.9 s (host), 3.4 s (container) |
| Inference, 96 × 72 JPEG | ~2.2 s |

`SPECIESNET_WARMUP=true` moves the ~50 s load off the first request. Because model load is
serialised behind a lock, a burst of concurrent first requests blocks rather than loading
the weights repeatedly.

The container image was built clean and verified end to end: `import speciesnet` resolves
`cv2`, `torch` and `speciesnet`; `python -m scripts.speciesnet_smoke` on the same fixture
returns `african elephant` at `0.99382` from the mounted `model-data` volume with no
re-download; `/health` and `/api/v1/config` answer correctly, and the public config response
carries `image_assistance` and `ai_model_version` but no operator reason.

## Readiness states

`GET /api/v1/config` reports the true state of image assistance, not just whether a path
happens to be set:

| State | Meaning |
| --- | --- |
| `ready` | Classifier is loaded in this process and usable |
| `degraded` | Enabled but not currently usable |
| `not_configured` | `IMAGE_ASSISTANCE=disabled` |

`degraded` is reported for a cold process, a previous load failure, a missing runtime, a
missing or incomplete model folder, and weights that are not in the cache. The specific
reason is **not** in the public `/config` response; administrators read it from
`GET /api/v1/admin/image-assistance`, which also returns the package version and licence.

`ready` requires a loaded classifier, so a placeholder or nonexistent `SPECIESNET_LOCAL_DIR`
can never present as configured.

The image-assistance state is read live on every `/config` call; only the deployment's static
fields are cached. A cached `degraded` must never mask a model that has just become ready.

Model load and readiness are logged. See `app/logging_config.py` and
[CLASSIFICATION.md](CLASSIFICATION.md).

## How results map to the API

`Prediction.boxes` is **always an empty list**. This classifier emits no localisation, and
the service does not fabricate geometry to fill the field. The client therefore shows a
species, a score and an explanation, and no box overlay.

| SpeciesNet output | `state` | `species` | `confidence` |
| --- | --- | --- | --- |
| Top class at or above `CONFIDENCE_THRESHOLD` | `completed` | top class | top score |
| Top class below the threshold | `unknown` | `null` | `null` |
| Top class is `blank`, `vehicle`, `human`, `person` | `unknown` | `null` | `null` |
| No classifications returned | `unknown` | `null` | `null` |
| Model missing, unloadable, or disabled | `unavailable` | `null` | `null` |

`blank` and `vehicle` are never reported as species. Anything at or below the threshold
becomes `unknown`, because *"unknown" is a valid, honest answer*. A human reviewer can
still record a species.

SpeciesNet labels are `<uuid>;<taxonomic path>;<common name>`. The stored `species` is the
trailing common name that the model itself supplies (`african elephant`), never the raw
UUID taxonomy string. For `unknown` results the explanation still lists the model's actual
top candidates above 0.1%, clearly labelled as candidates.

`model_version` is derived without loading any weights, and never from the weight cache, so
it is identical on a cold and a warm process and is safe to use as an idempotency key. It
includes the whole model reference including its revision, so republished weights change
the string:

```
speciesnet-5.0.5-google-speciesnet-pyTorch-v4.0.3b-1-full_image
```

Set `MODEL_VERSION` to override it deliberately; changing it re-runs assistance for existing
evidence.

## Uganda geographic context is not applied

SpeciesNet supports geographic filtering, but **only inside the ensemble step**, which is
gated on detector output. This deployment uses the classifier alone, so no country, geofence
or ISO code is passed and no geofencing or label rollup occurs. `SpeciesNetClassifier`
accepts no country argument, so `country=UGA` cannot be supplied to it. Consequences:

- Taxonomic rollup does not run, so a result may be a higher taxon (`felidae`,
  `mammalia`) rather than a species. That label is reported as returned.
- Species that are impossible in Uganda are not filtered out.
- A reviewer's judgement remains the only geographic correction.

Adding geofencing later means adopting the full ensemble, which brings the MegaDetector
detector with it. That is a deliberate scope decision, not an oversight.

## Deployment and model caching

Weights are downloaded once from Kaggle into a cache directory and reused. In Docker that
directory is a named volume, so the image stays small and restarts do not re-download.

```
model-data:/app/var/models      # KAGGLEHUB_CACHE
media-data:/app/var/media
```

First boot downloads the checkpoint and takes noticeably longer. Later boots load from the
volume with no network access.

To run fully offline, stage the model folder on the host and mount it, then set
`SPECIESNET_LOCAL_DIR`. A local folder is read directly, and its `info.json` supplies the
version used in `model_version`. This also avoids the detector-weight fetch described
below.

> **Unavoidable extra download.** The `speciesnet` package's `ModelInfo` resolves
> `info["detector"]` unconditionally, so the first download also fetches the MegaDetector
> weights (280,767,041 bytes) even though no detector is ever run. This is a property of the
> official package, not of this integration, and there is no supported switch to skip it.
> Supplying `SPECIESNET_LOCAL_DIR` with the classifier files is the way to avoid it. The
> file is never loaded and never executed.

> **Unavoidable extra dependencies.** `import speciesnet` pulls in the package's detector,
> display and ensemble modules, so `yolov5`, `opencv-python`, `pandas` and `matplotlib` are
> installed and imported even though only the classifier is used. There is no supported way
> to install the classifier alone from this package. `torchvision` is required by
> `speciesnet.classifier` for image transforms; it is **not** a Faster R-CNN dependency and
> no detector model is created from it. `requirements-ml.txt` pins `setuptools==79.0.0`
> because setuptools 80+ warns that `pkg_resources`, which the YOLOv5 chain imports, is
> going away.

A Celery worker that executes `prediction` jobs must mount the same `model-data` volume and
use the same `SPECIESNET_*` settings, or it will load its own copy.

## Privacy and error handling

- Evidence handling is unchanged: 10 MB cap, JPEG/PNG/WebP only, 20 MP guard, EXIF
  orientation applied, thumbnail re-encoded without metadata.
- Model input is the already-sanitised stored image. Raw uploads are never sent anywhere.
- A model failure returns a generic message. Filesystem paths, credentials, tracebacks
  and upstream error text are never returned to a client; details go to protected operator
  logs only.
- Assistance failures set `Prediction.state = 'failed'` and the outbox retries up to 5
  times. Reporting is never blocked by image assistance.

## Configuration reference

| Variable | Default | Notes |
| --- | --- | --- |
| `IMAGE_ASSISTANCE` | `auto` | `disabled` turns assistance off entirely |
| `SPECIESNET_MODEL` | `kaggle:google/speciesnet/pyTorch/v4.0.3b/1` | Kaggle, HuggingFace, or local path |
| `SPECIESNET_LOCAL_DIR` | unset | Offline folder; takes priority over the model id |
| `SPECIESNET_CACHE_DIR` | `./var/models` | Weight download cache |
| `SPECIESNET_DEVICE` | `cpu` | `cpu`, `cuda`, `mps`, or unset for auto |
| `SPECIESNET_WARMUP` | `true` | Preload on startup |
| `CONFIDENCE_THRESHOLD` | `0.75` | Below this, the result is `unknown` |
| `MODEL_VERSION` | derived | Explicit override for the provenance string |

## Tests

```
python -m pytest tests -q
```

Both test files run with no weights, no network and no PyTorch installed:

- `tests/test_ai.py` covers version derivation (including revision changes), readiness
  states and their operator reasons, threshold behaviour, non-species labels, empty
  classifier output, disabled mode, leakage-free degradation, and the undecodable-image
  path.
- `tests/test_predictions_api.py` covers the request contract end to end: non-image
  rejection, metadata stripping, per-evidence idempotency, re-running on a model-version
  change, authentication and ownership isolation, the admin-only status endpoint, and the
  rule that a completed suggestion never sets `reports.species`.

Real inference, which does need weights:

```
python -m scripts.speciesnet_smoke path/to/photo.jpg
```

A good known-answer image is SpeciesNet's own test fixture, which is Snapshot Serengeti
material under `CDLA-Permissive-1.0`:

```
curl -L -o elephants.jpg \
  https://raw.githubusercontent.com/google/cameratrapai/main/test_data/african_elephants.jpg
python -m scripts.speciesnet_smoke elephants.jpg
```

The script prints the checkpoint version, variant, label count, model version, load and
inference timings, the raw classifier top-5 and the mapped prediction, and exits non-zero
if the model failed to load. Verified result for that fixture on CPU: `african elephant`,
score `0.9938`, `state: completed`, `boxes: []`, 3.2–3.9 s.

## Citation

```
@article{gadot2024crop,
  title={To crop or not to crop: Comparing whole-image and cropped classification
         on a large dataset of camera trap images},
  author={Gadot, Tomer and Istrate, \c{S}tefan and Kim, Hyungwon and Morris, Dan
          and Beery, Sara and Birch, Tanya and Ahumada, Jorge},
  journal={IET Computer Vision},
  year={2024},
  publisher={Wiley}
}
```

Project: <https://github.com/google/cameratrapai> · Model card:
<https://www.kaggle.com/models/google/speciesnet>

## Known limitations

- No Uganda geofencing or taxonomic rollup (see above).
- No bounding boxes; the `boxes` field is always empty.
- No accuracy evaluation has been run for Uganda deployment. SpeciesNet is a
  geographically diverse global model, so per-species precision on Ugandan wildlife is
  **unmeasured**. `CONFIDENCE_THRESHOLD=0.75` is a conservative default, not a calibrated
  operating point, and the dashboard deliberately publishes no accuracy claims.
- A single extra model file (MegaDetector, 280.8 MB) and the detector, display and ensemble
  Python dependencies arrive as an unavoidable consequence of the upstream package layout.
  They are never executed.
- CPU inference is single-image and sequential through the outbox; large backlogs will be
  slow, and one inference takes 2–4 s on the development host. A GPU is supported via
  `SPECIESNET_DEVICE` but untested here.
- No fine-tuning or custom training is performed.
