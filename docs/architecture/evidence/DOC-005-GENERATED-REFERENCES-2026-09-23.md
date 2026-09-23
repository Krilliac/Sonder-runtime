# DOC-005 generated-reference verification

DOC-005 requires source-generated tool, command, event, configuration, schema,
and capability references with a freshness gate. PR
[#533](https://github.com/Krilliac/Sonder-runtime/pull/533) added the last two
reference families and a direct CI documentation-authority step. Its exact
head `2e857e3c9ce935b40192dde6925ea9dbfd4ec3e7` passed the required hosted
tests, analysis, integrity, and platform checks. The merged implementation is
`f5944d6921b2a1f00c7f31097293f972c50323e6`.

At that merged SHA, [CI run
35905137216](https://github.com/Krilliac/Sonder-runtime/actions/runs/35905137216)
and [app-build run
35905137354](https://github.com/Krilliac/Sonder-runtime/actions/runs/35905137354)
both completed successfully. On Windows, the same checkout passed
`scripts/generate_documentation_catalogs.py --check`,
`scripts/check_documentation_authority.py`, and the 11 focused tests in
`test_remaining_doc_001_005.py` and `test_document_authority.py`.

The generated `runtime-reference.json` contains 214 tools, 320 commands, 43
events, 208 configuration fields, four schema projections (MCP, OpenAI,
client, and event), and SDK plus operational capability references. The schema
and capability projections share a catalog digest. The artifact hashes its
source files, including the command catalog, server tool source, event and
configuration definitions, schema generator, SDK discovery, and operational
capability projection. Secret configuration defaults are redacted.

The direct `ci.yml` documentation-authority step recomputes and compares the
generated artifacts. A failed runtime tool import/listing now stops generation
instead of emitting an empty but apparently fresh tool catalog. This proof is
for static, source-derived references and their freshness. Runtime
authorization and dynamic provider availability remain runtime-evaluated;
external documentation mirrors are outside this repository contract.
