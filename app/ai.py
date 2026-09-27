"""Wildlife image classification backed by the official Google SpeciesNet classifier.

Only the SpeciesNet *whole-image* classifier (checkpoint ``v4.0.3b``, ``info["type"] ==
"full_image"``) is loaded and executed. No object detector runs, so this module never
produces bounding boxes; ``boxes`` is always an empty list rather than fabricated geometry.

SpeciesNet labels are taxonomic paths of the form::

    <uuid>;<kingdom>;<class>;<order>;<family>;<genus>;<species>;<common name>

The trailing common name is SpeciesNet's own name for the class and is what gets stored.
The full label is never invented or re-mapped; nothing outside SpeciesNet's vocabulary is
introduced.

Every failure degrades to an honest ``unavailable`` result with a generic message. No
filesystem path, credential, or model error text is ever returned to a client. Predictions
are candidates only: an authorised human reviewer remains the authority for a report.
See docs/AI.md for provenance, deployment and limitations.
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import re
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .config import get_settings

log = logging.getLogger(__name__)

# Recorded for provenance. Kept in step with the pin in requirements-ml.txt.
SPECIESNET_PACKAGE_VERSION = '5.0.5'
SPECIESNET_LICENCE = 'Apache-2.0'
SPECIESNET_SOURCE = 'https://github.com/google/cameratrapai'

# SpeciesNet reserves these classes for "not a species" outcomes. They are reported as
# ``unknown``, never as a wildlife identification.
NON_SPECIES_LABELS = frozenset({'blank', 'vehicle', 'human', 'person', 'no cv result'})
MAX_CANDIDATES_SHOWN = 3
# Candidates below this probability are not worth showing to a human reader.
CANDIDATE_MIN_SCORE = 0.001

UNAVAILABLE_EXPLANATION = ('Image assistance is currently unavailable. You can still '
    'submit this image with your report; an authorised reviewer will assess it.')

_lock = threading.Lock()
_classifier = None
_load_error: str | None = None


def _display_label(label: str) -> str:
    """SpeciesNet's own common name for a label, taken from the label itself.

    The classifier returns a semicolon-separated taxonomic path. The final segment is
    the common name; that is the model's own wording, so no external taxonomy, mapping
    or normalisation is applied.
    """
    return str(label).rsplit(';', 1)[-1].strip() or str(label).strip()


def _variant_for(name: str) -> str:
    if 'v4.0.3b' in name:
        return 'full_image'
    if 'v4.0.3a' in name:
        return 'always_crop'
    return 'unspecified'


def _local_model_info(directory: str) -> dict | None:
    info = Path(directory) / 'info.json'
    try:
        if info.is_file():
            data = json.loads(info.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        log.warning('SpeciesNet local info.json could not be read.')
    return None


def _local_model_complete(directory: str) -> bool:
    """True when a configured local model folder holds the classifier and its labels."""
    info = _local_model_info(directory)
    if not info:
        return False
    base = Path(directory)
    return all((base / info.get(key, '')).is_file()
               for key in ('classifier', 'classifier_labels'))


def _cached_model_dir() -> Path | None:
    """Where kagglehub keeps an already-downloaded Kaggle model, or None if unknown.

    kagglehub stores ``kaggle:owner/name/framework/version`` under ``models/``. This is
    only used to tell an operator that weights are absent; if the layout ever changes
    the answer degrades to "not confirmed", never to "ready".
    """
    ref = get_settings().speciesnet_model
    if not ref.startswith('kaggle:'):
        return None
    parts = [p for p in ref[len('kaggle:'):].split('/') if p]
    if len(parts) < 2:
        return None
    return Path(get_settings().speciesnet_cache_dir).joinpath('models', *parts)


def _runtime_installed() -> bool:
    """True when the speciesnet package is importable.

    ``find_spec`` raises ValueError for a module that is already in ``sys.modules``
    without a ``__spec__`` (import shims, and some test doubles). That must not turn a
    status read into a 500, so the probe is defensive and never raises.
    """
    try:
        return importlib.util.find_spec('speciesnet') is not None
    except (ImportError, ValueError, AttributeError):
        return 'speciesnet' in sys.modules


@lru_cache(maxsize=1)
def resolve_model_version() -> str:
    """Stable provenance string for the active checkpoint.

    Derived without loading weights so that it is safe to use for request
    idempotency. ``MODEL_VERSION`` overrides the derived value when set.

    The whole model reference is included, not just the checkpoint name, because a
    Kaggle reference carries a revision (``.../v4.0.3b/1``). Publishing new weights
    under the same checkpoint name produces a new revision, and the provenance string
    has to change with it or assistance results would be silently reused. Nothing here
    reads the weight cache, so the value is identical on a cold and a warm process.
    """
    cfg = get_settings()
    if cfg.model_version:
        return cfg.model_version
    name = cfg.speciesnet_local_dir or cfg.speciesnet_model
    info = _local_model_info(name) if cfg.speciesnet_local_dir else None
    if info and info.get('version'):
        return f"speciesnet-{SPECIESNET_PACKAGE_VERSION}-{info['version']}-{info.get('type', 'unspecified')}"
    prefix = 'speciesnet-' + SPECIESNET_PACKAGE_VERSION
    variant = _variant_for(name)
    reference = re.sub(r'[^A-Za-z0-9._-]+', '-', str(name).strip('/').removeprefix('kaggle:'))
    return f'{prefix}-{reference}-{variant}' if reference else f'{prefix}-{variant}'


def status() -> dict:
    """Readiness of image assistance.

    ``ready`` is reported only when a classifier is actually loaded in this process, so
    a placeholder or nonexistent model path can never present as configured. ``detail``
    is an operator-facing reason and is not part of the public ``/config`` response.
    """
    cfg = get_settings()
    if not cfg.ai_enabled:
        return {'state': 'not_configured', 'model_version': 'not-configured',
                'detail': 'Image assistance is switched off (IMAGE_ASSISTANCE=disabled).'}
    version = resolve_model_version()
    if _classifier is not None:
        return {'state': 'ready', 'model_version': version,
                'detail': 'Classifier is loaded and able to run inference.'}
    if not _runtime_installed():
        return {'state': 'degraded', 'model_version': version,
                'detail': 'The SpeciesNet runtime is not installed in this environment. '
                          'Install requirements-ml.txt; predictions return "unavailable".'}
    if _load_error is not None:
        return {'state': 'degraded', 'model_version': version,
                'detail': 'The classifier failed to load on an earlier attempt. See '
                          'protected operator logs; predictions return "unavailable".'}
    if cfg.speciesnet_local_dir:
        if not _local_model_complete(cfg.speciesnet_local_dir):
            return {'state': 'degraded', 'model_version': version,
                    'detail': 'The configured SpeciesNet model folder is missing its '
                              'weights, labels or info.json.'}
        return {'state': 'degraded', 'model_version': version,
                'detail': 'Enabled, weights present, not loaded yet; the classifier loads '
                          'on first use or during startup warmup.'}
    cached = _cached_model_dir()
    if cached is not None and not _local_model_complete(str(cached)):
        return {'state': 'degraded', 'model_version': version,
                'detail': 'Model weights are not in the cache yet. They download once on '
                          'first use, then predictions reuse the cached copy.'}
    return {'state': 'degraded', 'model_version': version,
            'detail': 'Enabled and not loaded yet; the classifier loads on first use or '
                      'during startup warmup.'}


def _load():
    """Load the classifier once per process. Returns None on any failure."""
    global _classifier, _load_error
    cfg = get_settings()
    if not cfg.ai_enabled:
        return None
    with _lock:
        if _classifier is not None:
            return _classifier
        name = cfg.speciesnet_local_dir or cfg.speciesnet_model
        started = time.perf_counter()
        log.info('[AI] loading SpeciesNet whole-image classifier: state=loading '
                 'model=%s variant=%s device=%s',
                 resolve_model_version(), _variant_for(name), cfg.speciesnet_device or 'auto')
        try:
            os.environ.setdefault('KAGGLEHUB_CACHE', cfg.speciesnet_cache_dir)
            from speciesnet import SpeciesNetClassifier
            _classifier = SpeciesNetClassifier(name, device=cfg.speciesnet_device)
            _load_error = None
            log.info('[AI] SpeciesNet loaded successfully: model=%s checkpoint=%s '
                     'labels=%d device=%s load_time=%.2fs image_assistance=%s',
                     resolve_model_version(), _classifier.model_info.version,
                     len(_classifier.labels), _classifier.device,
                     time.perf_counter() - started, status()['state'])
            return _classifier
        except Exception as exc:
            _load_error = type(exc).__name__
            log.error('[AI] SpeciesNet failed to load: model=%s error=%s '
                      'image_assistance=%s detail=%s',
                      resolve_model_version(), _load_error, status()['state'],
                      'see exception below; predictions return "unavailable"')
            log.exception('SpeciesNet classifier could not be loaded; image assistance '
                          'is degraded.')
            return None


def warmup() -> None:
    """Preload the classifier so the first prediction does not pay model load time."""
    cfg = get_settings()
    if not cfg.ai_enabled:
        log.info('[AI] warmup skipped: image assistance is disabled '
                 '(IMAGE_ASSISTANCE=disabled)')
        return
    if not cfg.speciesnet_warmup:
        log.info('[AI] warmup skipped: SPECIESNET_WARMUP is off, the classifier loads on '
                 'first prediction. model=%s image_assistance=%s',
                 resolve_model_version(), status()['state'])
        return
    loaded = _load() is not None
    log.info('[AI] warmup finished: loaded=%s model=%s image_assistance=%s',
             loaded, resolve_model_version(), status()['state'])


def _unavailable(explanation: str) -> dict:
    return {'state': 'unavailable', 'species': None, 'confidence': None, 'boxes': [],
            'model_version': resolve_model_version(), 'explanation': explanation}


def _unknown(explanation: str) -> dict:
    return {'state': 'unknown', 'species': None, 'confidence': None, 'boxes': [],
            'model_version': resolve_model_version(), 'explanation': explanation}


def _candidates(classes: list, scores: list, count: int = MAX_CANDIDATES_SHOWN) -> str:
    """SpeciesNet's own top-N, rendered for a human reader. Not an identification.

    Near-zero scores are dropped so the sentence does not read as "vehicle (0.0)".
    """
    parts = [f'{_display_label(label)} ({round(float(score) * 100, 1)}%)'
             for label, score in zip(classes, scores)
             if float(score) >= CANDIDATE_MIN_SCORE]
    return ', '.join(parts[:count]) or 'no other candidate above 0.1%'


def infer(data: bytes) -> dict:
    """Classify one stored evidence image. Never raises for model problems."""
    cfg = get_settings()
    if not cfg.ai_enabled:
        return _unavailable('Image assistance is disabled for this deployment. '
            'Submit your observation with the image; human review is required.')
    classifier = _load()
    if classifier is None:
        return _unavailable(UNAVAILABLE_EXPLANATION)
    try:
        with Image.open(io.BytesIO(data)) as handle:
            handle.load()
            image = handle.convert('RGB')
    except Exception as exc:
        raise ValueError('Stored evidence could not be decoded as an image.') from exc

    import torch
    with torch.inference_mode():
        preprocessed = classifier.preprocess(image)
        result = classifier.predict('evidence', preprocessed)

    if result.get('failures'):
        raise RuntimeError('SpeciesNet reported a component failure for this image.')

    classifications = result.get('classifications') or {}
    classes = classifications.get('classes') or []
    scores = classifications.get('scores') or []
    if not classes or not scores:
        return _unknown('The classifier returned no candidate labels for this image. '
            'Human review is required.')

    label = str(classes[0])
    name = _display_label(label)
    score = float(scores[0])
    if name.lower() in NON_SPECIES_LABELS:
        return _unknown(f'No wildlife species was identified in this image (the classifier '
            f'returned "{name}"). Submit your observation; human review is required.')
    if score < cfg.confidence_threshold:
        return _unknown(f'No candidate reached the configured confidence threshold of '
            f'{cfg.confidence_threshold}. SpeciesNet\'s leading candidates were: '
            f'{_candidates(classes, scores)}. Unknown is a valid result; submit your '
            'observation for human review.')
    others = _candidates(classes[1:], scores[1:], MAX_CANDIDATES_SHOWN - 1)
    explanation = ('SpeciesNet candidate identification only, from the whole-image '
                   'classifier. This classifier produces no detection boxes. An '
                   'authorised reviewer must confirm the report species.')
    if others:
        explanation += f' Other candidates the model considered: {others}.'
    return {'state': 'completed', 'species': name, 'confidence': score, 'boxes': [],
            'model_version': resolve_model_version(), 'explanation': explanation}
