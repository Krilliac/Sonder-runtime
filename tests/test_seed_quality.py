import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "curate_seed_data.py"
    spec = importlib.util.spec_from_file_location("curate_seed_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_seed_data_passes_its_own_quality_contract():
    assert _module().inspect() == {
        "vague_packaged_lessons": 0,
        "seed_grounded_true_claims": 0,
    }
