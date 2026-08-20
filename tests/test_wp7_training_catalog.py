from sonder_runtime.application.training.adapter_catalog import AdapterCatalog, AdapterKind, AdapterSpec
from sonder_runtime.domain.training.reproducible import (
    BaseModelManifest, DatasetManifest, DependencyManifest, EvaluationManifest,
    Provenance, ReproducibleTrainingManifest,
)


def provenance(source="registry", revision="r1"):
    return Provenance.from_mapping(source, revision, metadata={"owner": "test"})


def test_reproducible_manifest_is_immutable_and_digest_stable():
    dataset = DatasetManifest("ds", "data-digest", 10, "jsonl-v1", provenance("data", "d1"))
    model = BaseModelManifest("model", "m1", "weights", "tokenizer", provenance("model", "m1"))
    evaluation = EvaluationManifest.from_mapping("suite", "1", "data-digest", {"accuracy": 0.9}, provenance("eval", "e1"))
    manifest = ReproducibleTrainingManifest(dataset, model, (DependencyManifest("torch", "2", "pypi", "torch-digest"),), evaluation)
    assert manifest.digest == ReproducibleTrainingManifest(dataset, model, manifest.dependencies, evaluation).digest
    try:
        manifest.dataset = dataset
    except Exception as exc:
        assert isinstance(exc, AttributeError)


def test_manifest_rejects_evaluation_for_different_snapshot():
    dataset = DatasetManifest("ds", "data-digest", 1, "v1", provenance())
    model = BaseModelManifest("model", "m1", "weights", "tokens", provenance())
    evaluation = EvaluationManifest.from_mapping("suite", "1", "other", {}, provenance())
    try:
        ReproducibleTrainingManifest(dataset, model, (), evaluation)
    except ValueError as exc:
        assert "locked snapshot" in str(exc)
    else:
        raise AssertionError("mismatched evaluation snapshot was accepted")


def test_adapter_catalog_covers_three_scopes_and_is_order_independent():
    adapters = [
        AdapterSpec("personal", AdapterKind.PERSONALIZATION, "model", "a3", "c3", provenance("p", "1")),
        AdapterSpec("task", AdapterKind.TASK, "model", "a1", "c1", provenance("t", "1")),
        AdapterSpec("project", AdapterKind.PROJECT, "model", "a2", "c2", provenance("j", "1")),
    ]
    left = AdapterCatalog.from_adapters(adapters)
    right = AdapterCatalog.from_adapters(reversed(adapters))
    assert tuple(item.kind for item in left.adapters) == (AdapterKind.PERSONALIZATION, AdapterKind.PROJECT, AdapterKind.TASK)
    assert left.digest == right.digest
    assert left.compatible("task", "model")
    assert not left.compatible("task", "other")


def test_catalog_registration_is_copy_on_write_and_rejects_duplicates():
    adapter = AdapterSpec("task", AdapterKind.TASK, "model", "a", "c", provenance())
    catalog = AdapterCatalog().register(adapter)
    assert not AdapterCatalog().adapters
    try:
        catalog.register(adapter)
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate adapter was accepted")
