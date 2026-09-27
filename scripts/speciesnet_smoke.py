"""Real SpeciesNet smoke test. Requires installed weights and a real photo.

    python -m scripts.speciesnet_smoke path/to/photo.jpg

Exits 0 only when the model actually ran and produced a mapped result. Prints the raw
classifier top-5 alongside the stored prediction contract. Intended for operators, not CI.

A good known-answer image is SpeciesNet's own test fixture, which is Snapshot Serengeti
material under CDLA-Permissive-1.0:

    curl -L -o elephants.jpg \
      https://raw.githubusercontent.com/google/cameratrapai/main/test_data/african_elephants.jpg
"""
import json
import sys
import time
from pathlib import Path

from app import ai
from app.config import get_settings

ALLOWED = {'.jpg', '.jpeg', '.png', '.webp'}


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    if path.suffix.lower() not in ALLOWED or not path.is_file():
        print('Provide an existing JPEG, PNG or WebP file.')
        return 2

    cfg = get_settings()
    print('image_assistance :', 'enabled' if cfg.ai_enabled else 'disabled')
    print('model_reference  :', cfg.speciesnet_local_dir or cfg.speciesnet_model)
    print('cache_dir        :', cfg.speciesnet_cache_dir)
    print('device           :', cfg.speciesnet_device or 'auto')
    print('threshold        :', cfg.confidence_threshold)
    print('image_bytes      :', path.stat().st_size)

    started = time.perf_counter()
    print('loading classifier, this downloads weights on first run ...')
    classifier = ai._load()
    print('load_seconds     :', round(time.perf_counter() - started, 2))
    print('status           :', json.dumps(ai.status()))
    if classifier is None:
        print('RESULT: model did not load; image assistance is degraded.')
        return 1

    info = classifier.model_info
    print('checkpoint       :', info.version)
    print('variant          :', info.type_)
    print('labels           :', len(classifier.labels))
    print('model_version    :', ai.resolve_model_version())

    from PIL import Image
    with Image.open(path) as handle:
        handle.load()
        image = handle.convert('RGB')
    print('input            :', image.width, 'x', image.height)

    raw = classifier.predict('smoke', classifier.preprocess(image))
    print('raw top-5        :', json.dumps(raw.get('classifications', {}), indent=2))

    started = time.perf_counter()
    result = ai.infer(path.read_bytes())
    print('infer_seconds    :', round(time.perf_counter() - started, 2))
    print('prediction       :', json.dumps(result, indent=2))

    if result['state'] in ('completed', 'unknown'):
        print('RESULT: model ran successfully.')
        return 0
    print('RESULT: model did not produce a prediction.')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
