/// The shared status vocabulary as the Runtime screen uses it.
///
/// Runtime speaks lane B's [StatusKind] (`lib/ui/status_vocab.dart`, pinned
/// against `sonder_runtime/interfaces/repl/style.py`) and draws marks with
/// lane B's [StatusMark]. The only Runtime-specific choices live here: the
/// "off" synonym for [StatusKind.skipped] (plan §2.4, `– off  Autopilot`)
/// and which kinds count toward the operator's attention.
library;

import 'package:flutter/material.dart';

import '../ui/status_row.dart';
import '../ui/status_vocab.dart';

export '../ui/status_vocab.dart' show StatusKind;

extension RuntimeStatusWords on StatusKind {
  /// The word Runtime shows by default: the shared word, except that
  /// off-by-design reads "off" (a synonym from the same vocabulary row).
  String get runtimeWord => this == StatusKind.skipped ? 'off' : word;

  /// A problem the operator should look at (counts toward attention).
  bool get isProblem => const {
        StatusKind.fail,
        StatusKind.refused,
        StatusKind.warn,
        StatusKind.ask,
      }.contains(this);
}

/// `✓ ok` as one fixed-width cell: lane B's [StatusMark] with Runtime's
/// default word. Screen readers hear the word, never the glyph.
class RuntimeStatusWord extends StatelessWidget {
  final StatusKind status;
  final String? word;
  final double width;

  const RuntimeStatusWord(this.status, {super.key, this.word, this.width = 92});

  @override
  Widget build(BuildContext context) => StatusMark(status,
      word: word ?? status.runtimeWord, size: 12.5, width: width);
}
