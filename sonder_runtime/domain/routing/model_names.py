"""Pure model-name comparison shared by Ollama admission and conformance."""


def tagged_ollama_model(model: str) -> str:
    """Ollama adds :latest to untagged names, including namespaced models."""
    return model if ":" in model.rsplit("/", 1)[-1] else model + ":latest"
