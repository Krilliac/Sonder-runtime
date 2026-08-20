# WP1 Eighty-First Slice: Filesystem Path Boundary

## Scope

`sonder_runtime.adapters.filesystem.file_ops` now consumes the canonical
`sonder_runtime.platform.paths` boundary for default-home resolution. This
removes its direct production import of the root `sonder_paths` module while
preserving the configured roots file, allowed-root containment, and
default-home behavior.

## Compatibility

The platform module remains the compatibility seam for the existing
`sonder_paths` implementation. No filesystem policy, root normalization, or
containment semantics changed.

## Evidence

- Focused path-boundary tests cover default-home delegation and containment.
- Existing file-operation and containment-degradation suites cover the full
  guarded filesystem behavior.
- Compile, architecture, requirement-evidence, and diff checks are required
  before crediting this slice.
