# Generated architecture map

Generated; do not edit manually.

Authority: `docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md`

## Package layers

| Layer | Python files |
|---|---:|
| `__pycache__` | 0 |
| `adapters` | 239 |
| `application` | 270 |
| `bootstrap` | 17 |
| `domain` | 146 |
| `interfaces` | 38 |
| `platform` | 33 |

## Composition roots

- `sonder_runtime/__main__.py`
- `sonder_runtime/bootstrap/`
- `sonder_runtime/interfaces/`

## Layer ownership

| Package | State | Public port | Provider | Schema | Lifecycle |
|---|---|---|---|---|---|
| `sonder_runtime.adapters` | `adapters.state` | `adapters.ports` | `adapters.providers` | `adapters.schemas` | `adapters.lifecycle` |
| `sonder_runtime.application` | `application.state` | `application.ports` | `application.providers` | `application.schemas` | `application.lifecycle` |
| `sonder_runtime.bootstrap` | `bootstrap.state` | `bootstrap.ports` | `bootstrap.providers` | `bootstrap.schemas` | `bootstrap.lifecycle` |
| `sonder_runtime.domain` | `domain.state` | `domain.ports` | `domain.providers` | `domain.schemas` | `domain.lifecycle` |
| `sonder_runtime.interfaces` | `interfaces.state` | `interfaces.ports` | `interfaces.providers` | `interfaces.schemas` | `interfaces.lifecycle` |
| `sonder_runtime.platform` | `platform.state` | `platform.ports` | `platform.providers` | `platform.schemas` | `platform.lifecycle` |
