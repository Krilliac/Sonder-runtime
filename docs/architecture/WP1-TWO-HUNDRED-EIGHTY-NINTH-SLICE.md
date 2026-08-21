# WP1 Two-Hundred-Eighty-Ninth Slice — typed multimodal vision port

## Boundary

Added `VisionRequest`, `VisionResponse`, and `VisionGateway` under the
application ports. The request carries validated image bytes and an explicit
media type, not a filesystem path; it bounds image size and prompt length and
accepts only PNG/JPEG/BMP. This gives the future local vision adapter a typed
contract without routing pixels through the text `ModelGateway`.

An injected `VisionGateway` adapter now enforces deadline, cancellation, and
local-only consent before invoking a provider-owned callable. It does not
choose a provider or import the legacy server.

`VisionService` now joins that gateway to a guarded `FileVisionInputProvider`:
paths are resolved and authorized at the filesystem boundary, inspected for
supported raster metadata and dimensions, then rehashed after reading before
bytes cross into the gateway.

The concrete packaged Ollama adapter and composition root now implement that
gateway. It resolves the configured vision tier, refuses cloud/remote
contexts and non-loopback endpoints, and sends the image only in the bounded
local `/api/chat` request.

## Evidence

- Port validation and provider-output checks pass: **6 passed**.
- Injected adapter contract checks pass: **3 passed**.
- The application service boundary check passes: **1 passed**.
- Packaged Ollama vision transport checks pass: **2 passed**.
- Native MCP vision routing check passes: **1 passed**.
- Existing server vision regressions remain green: **14 passed**.
- Native MCP now exposes `vision_analyze` through `application.vision` with a
  path/prompt-only schema; legacy token, approval, and bypass arguments remain
  excluded. A live local-model request is still outside the offline evidence.

## Remaining migration work

The remaining evidence is a live local-model smoke test; the offline route and
all consent boundaries are now covered.
