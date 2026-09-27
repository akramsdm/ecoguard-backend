"""Image-assistance contract tests. No model weights, no network, no torch required."""
import importlib.machinery
import io
import json
import logging
import sys
import types

import pytest
from PIL import Image

from app import ai
from app.config import get_settings
from app.logging_config import configure as configure_logging

# Real SpeciesNet label shapes: <uuid>;<taxonomic path>;<common name>.
ELEPHANT = ('55631055-3e0e-4b7a-9612-dedebe9f78b0;mammalia;proboscidea;elephantidae;'
            'loxodonta;africana;african elephant')
BUFFALO = ('9f689929-883d-4dae-958c-3d57ab5b6c16;mammalia;artiodactyla;bovidae;'
           'syncerus;caffer;african buffalo')
BLANK = 'f1856211-cfb7-4a5b-9158-c0f72fd09ee6;;;;;;blank'
VEHICLE = 'e2895ed5-780b-48f6-8a11-9e27cb594511;;;;;;vehicle'
HUMAN = ('990ae9dd-7a59-4344-afcb-1b7b21368000;mammalia;primates;hominidae;homo;'
         'sapiens;human')


class _FakeInferenceMode:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class FakeClassifier:
    def __init__(self, classes, scores, failures=None):
        self.classes = classes
        self.scores = scores
        self.failures = failures
        self.device = 'cpu'
        self.labels = {0: 'x'}
        self.model_info = types.SimpleNamespace(version='4.0.3b', type_='full_image')

    def preprocess(self, image, bboxes=None, resize=True):
        return {'shape': (image.width, image.height)}

    def predict(self, filepath, img):
        result = {'filepath': filepath,
                  'classifications': {'classes': self.classes, 'scores': self.scores}}
        if self.failures:
            result['failures'] = self.failures
        return result


def jpeg_bytes(colour=(12, 90, 40)):
    buffer = io.BytesIO()
    Image.new('RGB', (96, 72), colour).save(buffer, format='JPEG')
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    fake_torch = types.ModuleType('torch')
    fake_torch.inference_mode = _FakeInferenceMode
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()
    ai._classifier = None
    ai._load_error = None
    yield
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()
    ai._classifier = None
    ai._load_error = None


def use(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()


# --- provenance ---------------------------------------------------------------

def test_model_version_is_derived_without_loading_weights(monkeypatch):
    use(monkeypatch)
    assert ai.resolve_model_version() == 'speciesnet-5.0.5-google-speciesnet-pyTorch-v4.0.3b-1-full_image'


def test_model_version_changes_when_the_checkpoint_changes(monkeypatch):
    use(monkeypatch, SPECIESNET_MODEL='kaggle:google/speciesnet/pyTorch/v4.0.3a/1')
    assert ai.resolve_model_version() == 'speciesnet-5.0.5-google-speciesnet-pyTorch-v4.0.3a-1-always_crop'


def test_model_version_changes_when_the_published_revision_changes(monkeypatch):
    use(monkeypatch)
    first = ai.resolve_model_version()
    use(monkeypatch, SPECIESNET_MODEL='kaggle:google/speciesnet/pyTorch/v4.0.3b/2')
    assert ai.resolve_model_version() != first
    assert ai.resolve_model_version().endswith('v4.0.3b-2-full_image')


def test_model_version_honours_explicit_override(monkeypatch):
    use(monkeypatch, MODEL_VERSION='speciesnet-5.0.5-v4.0.3b-full_image')
    assert ai.resolve_model_version() == 'speciesnet-5.0.5-v4.0.3b-full_image'


def test_model_version_reads_local_model_folder(monkeypatch, tmp_path):
    (tmp_path / 'info.json').write_text(json.dumps({'version': '4.0.3b', 'type': 'full_image'}))
    use(monkeypatch, SPECIESNET_LOCAL_DIR=str(tmp_path))
    assert ai.resolve_model_version() == 'speciesnet-5.0.5-4.0.3b-full_image'


# --- readiness ----------------------------------------------------------------

def test_status_not_configured_when_disabled(monkeypatch):
    use(monkeypatch, IMAGE_ASSISTANCE='disabled')
    assert ai.status()['state'] == 'not_configured'
    assert ai.status()['model_version'] == 'not-configured'


def test_status_ready_only_after_successful_load(monkeypatch):
    use(monkeypatch, CONFIDENCE_THRESHOLD='0.75')
    assert ai.status()['state'] == 'degraded'
    ai._classifier = FakeClassifier([ELEPHANT], [0.9])
    assert ai.status()['state'] == 'ready'


def test_dummy_model_path_is_never_reported_as_ready(monkeypatch, tmp_path):
    use(monkeypatch, SPECIESNET_LOCAL_DIR=str(tmp_path / 'no-such-model'))
    monkeypatch.setattr(ai, '_runtime_installed', lambda: True)
    state = ai.status()
    assert state['state'] == 'degraded'
    assert 'missing' in state['detail'].lower()


def test_incomplete_local_model_folder_is_degraded(monkeypatch, tmp_path):
    (tmp_path / 'info.json').write_text(json.dumps({'version': '4.0.3b', 'type': 'full_image',
        'classifier': 'model.pt', 'classifier_labels': 'model.labels.txt'}))
    use(monkeypatch, SPECIESNET_LOCAL_DIR=str(tmp_path))
    monkeypatch.setattr(ai, '_runtime_installed', lambda: True)
    state = ai.status()
    assert state['state'] == 'degraded'
    assert 'weights' in state['detail'].lower()


def test_missing_runtime_is_degraded_not_ready(monkeypatch):
    use(monkeypatch)
    monkeypatch.setattr(ai, '_runtime_installed', lambda: False)
    state = ai.status()
    assert state['state'] == 'degraded'
    assert 'not installed' in state['detail']


def test_uncached_weights_are_reported_as_degraded(monkeypatch, tmp_path):
    use(monkeypatch, SPECIESNET_CACHE_DIR=str(tmp_path),
        SPECIESNET_MODEL='kaggle:google/speciesnet/pyTorch/v4.0.3b/1')
    monkeypatch.setattr(ai, '_runtime_installed', lambda: True)
    state = ai.status()
    assert state['state'] == 'degraded'
    assert 'not in the cache' in state['detail']


def test_previous_load_failure_is_degraded_with_a_reason(monkeypatch):
    use(monkeypatch)
    monkeypatch.setattr(ai, '_runtime_installed', lambda: True)
    ai._load_error = 'OSError'
    state = ai.status()
    assert state['state'] == 'degraded'
    assert 'failed to load' in state['detail']


# --- prediction mapping -------------------------------------------------------

def test_completed_prediction_never_invents_boxes(monkeypatch):
    use(monkeypatch, CONFIDENCE_THRESHOLD='0.75')
    ai._classifier = FakeClassifier([ELEPHANT, BUFFALO], [0.91, 0.04])
    result = ai.infer(jpeg_bytes())
    assert result['state'] == 'completed'
    assert result['species'] == 'african elephant'
    assert result['confidence'] == pytest.approx(0.91)
    assert result['boxes'] == []
    assert result['model_version'] == 'speciesnet-5.0.5-google-speciesnet-pyTorch-v4.0.3b-1-full_image'
    assert 'reviewer' in result['explanation']
    assert 'african buffalo' in result['explanation']


def test_low_confidence_yields_unknown_not_a_species(monkeypatch):
    use(monkeypatch, CONFIDENCE_THRESHOLD='0.75')
    ai._classifier = FakeClassifier([ELEPHANT, BUFFALO], [0.42, 0.11])
    result = ai.infer(jpeg_bytes())
    assert result['state'] == 'unknown'
    assert result['species'] is None
    assert result['confidence'] is None
    assert result['boxes'] == []
    # SpeciesNet's own top-N is still shown, clearly as candidates.
    assert 'african elephant' in result['explanation']


def test_threshold_is_configurable(monkeypatch):
    use(monkeypatch, CONFIDENCE_THRESHOLD='0.30')
    ai._classifier = FakeClassifier([ELEPHANT], [0.42])
    assert ai.infer(jpeg_bytes())['state'] == 'completed'


@pytest.mark.parametrize('label', [BLANK, VEHICLE, HUMAN])
def test_non_species_labels_are_never_reported_as_a_species(monkeypatch, label):
    use(monkeypatch, CONFIDENCE_THRESHOLD='0.75')
    ai._classifier = FakeClassifier([label, ELEPHANT], [0.99, 0.01])
    result = ai.infer(jpeg_bytes())
    assert result['state'] == 'unknown'
    assert result['species'] is None
    assert label.rsplit(';', 1)[-1] in result['explanation']


def test_empty_classifications_yield_unknown(monkeypatch):
    use(monkeypatch, CONFIDENCE_THRESHOLD='0.75')
    ai._classifier = FakeClassifier([], [])
    assert ai.infer(jpeg_bytes())['state'] == 'unknown'


def test_component_failure_raises_for_job_retry(monkeypatch):
    use(monkeypatch)
    ai._classifier = FakeClassifier([], [], failures=['CLASSIFIER'])
    with pytest.raises(RuntimeError):
        ai.infer(jpeg_bytes())


def test_disabled_assistance_is_unavailable(monkeypatch):
    use(monkeypatch, IMAGE_ASSISTANCE='disabled')
    result = ai.infer(jpeg_bytes())
    assert result['state'] == 'unavailable'
    assert result['species'] is None
    assert 'disabled' in result['explanation']


def test_unavailable_when_the_model_cannot_load(monkeypatch):
    use(monkeypatch)
    monkeypatch.setattr(ai, '_load', lambda: None)
    ai._load_error = 'OSError'
    result = ai.infer(jpeg_bytes())
    assert result['state'] == 'unavailable'
    assert result['species'] is None
    assert result['confidence'] is None
    assert result['boxes'] == []


# --- startup observability ------------------------------------------------------

def install_fake_speciesnet(monkeypatch, factory):
    module = types.ModuleType('speciesnet')
    module.SpeciesNetClassifier = factory
    # A real imported module always has a spec; without one find_spec() raises ValueError.
    module.__spec__ = importlib.machinery.ModuleSpec('speciesnet', loader=None)
    monkeypatch.setitem(sys.modules, 'speciesnet', module)


def test_status_survives_a_module_without_an_import_spec(monkeypatch):
    """A shimmed speciesnet must degrade, not 500 /api/v1/config."""
    broken = types.ModuleType('speciesnet')
    monkeypatch.setitem(sys.modules, 'speciesnet', broken)
    use(monkeypatch)
    assert ai._runtime_installed() is True
    assert ai.status()['state'] == 'degraded'


def test_startup_log_reports_the_model_load_summary(monkeypatch, caplog):
    install_fake_speciesnet(
        monkeypatch, lambda name, device=None: FakeClassifier([ELEPHANT], [0.9]))
    use(monkeypatch)
    configure_logging()
    with caplog.at_level(logging.INFO, logger='app.ai'):
        assert ai._load() is not None
    text = '\n'.join(record.getMessage() for record in caplog.records)
    assert '[AI] loading SpeciesNet whole-image classifier' in text
    assert 'state=loading' in text
    assert '[AI] SpeciesNet loaded successfully' in text
    assert 'model=speciesnet-5.0.5-google-speciesnet-pyTorch-v4.0.3b-1-full_image' in text
    assert 'checkpoint=4.0.3b' in text
    assert 'load_time=' in text and 's image_assistance=ready' in text


def test_warmup_logs_the_final_state(monkeypatch, caplog):
    install_fake_speciesnet(
        monkeypatch, lambda name, device=None: FakeClassifier([ELEPHANT], [0.9]))
    use(monkeypatch, SPECIESNET_WARMUP='true')
    configure_logging()
    with caplog.at_level(logging.INFO, logger='app.ai'):
        ai.warmup()
    text = '\n'.join(record.getMessage() for record in caplog.records)
    assert '[AI] warmup finished: loaded=True' in text
    assert 'image_assistance=ready' in text


def test_warmup_skipped_is_logged_when_warmup_is_off(monkeypatch, caplog):
    install_fake_speciesnet(
        monkeypatch, lambda name, device=None: FakeClassifier([ELEPHANT], [0.9]))
    use(monkeypatch, SPECIESNET_WARMUP='false')
    configure_logging()
    with caplog.at_level(logging.INFO, logger='app.ai'):
        ai.warmup()
    text = '\n'.join(record.getMessage() for record in caplog.records)
    assert '[AI] warmup skipped' in text
    assert 'SPECIESNET_WARMUP is off' in text
    # A skipped warmup must not have loaded anything.
    assert ai._classifier is None


def test_load_failure_is_logged_clearly(monkeypatch, caplog):
    def boom(name, device=None):
        raise OSError('weights are not readable')

    install_fake_speciesnet(monkeypatch, boom)
    use(monkeypatch)
    configure_logging()
    with caplog.at_level(logging.INFO, logger='app.ai'):
        assert ai._load() is None
    text = '\n'.join(record.getMessage() for record in caplog.records)
    assert '[AI] SpeciesNet failed to load' in text
    assert 'error=OSError' in text
    assert 'image_assistance=degraded' in text
    assert ai.status()['state'] == 'degraded'


def test_warmup_skipped_is_logged_when_disabled(monkeypatch, caplog):
    use(monkeypatch, IMAGE_ASSISTANCE='disabled')
    configure_logging()
    with caplog.at_level(logging.INFO, logger='app.ai'):
        ai.warmup()
    text = '\n'.join(record.getMessage() for record in caplog.records)
    assert '[AI] warmup skipped' in text
    assert 'IMAGE_ASSISTANCE=disabled' in text


def test_the_classifier_is_loaded_only_once(monkeypatch):
    calls = []

    def factory(name, device=None):
        calls.append(name)
        return FakeClassifier([ELEPHANT], [0.9])

    install_fake_speciesnet(monkeypatch, factory)
    use(monkeypatch)
    configure_logging()
    first = ai._load()
    second = ai._load()
    assert first is second
    assert len(calls) == 1
    ai.infer(jpeg_bytes())
    assert len(calls) == 1


def test_infrastructure_failure_never_leaks_internals(monkeypatch):
    use(monkeypatch)

    def boom():
        ai._load_error = 'OSError'
        return None

    monkeypatch.setattr(ai, '_load', boom)
    text = json.dumps(ai.infer(jpeg_bytes())).lower()
    for leak in ('traceback', 'modulenotfound', 'kaggle', '/app', 'c:\\', 'oserror',
                 'exception', 'var/models', 'pip'):
        assert leak not in text


def test_undecodable_evidence_raises_for_job_retry(monkeypatch):
    use(monkeypatch)
    ai._classifier = FakeClassifier([ELEPHANT], [0.9])
    with pytest.raises(ValueError):
        ai.infer(b'not-an-image')
