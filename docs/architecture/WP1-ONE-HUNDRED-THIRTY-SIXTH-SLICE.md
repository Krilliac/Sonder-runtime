# WP1 One-Hundred-Thirty-Sixth Slice

## Evaluation-history reader ownership

The composition root now constructs `EvaluationHistoryReaderAdapter` from the
canonical packaged module `sonder_runtime.adapters.evaluation_history_reader`.
The former `eval_history_reader` module remains only as an identity-preserving
compatibility shim exposing `LegacyEvaluationHistoryReader`. The adapter keeps
the evaluation-history store import lazy, so importing or building the graph
does not initialize persistence prematurely.

## Verification

- Focused evaluation-history and composition tests pass.
- The architecture, requirement-evidence, compile, and diff gates pass.
- No `server.py`, repository/tool/event/gateway/UnitOfWork/preference/workflow
  files were changed by this slice.
