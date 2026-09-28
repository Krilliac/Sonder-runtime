import 'package:flutter/material.dart';

import '../models.dart';
import '../theme.dart';
import '../ui/status_vocab.dart';

/// The long-context overflow notice on one assistant message.
///
/// Rendered from `sonder_receipt.overflow`, above the answer and never inside
/// it. A switch to the overflow model is a note; staying on the original
/// route because the overflow model was unavailable is a warn. The glyph and
/// word lead, and the server's notice text is shown as sent.
class RouteOverflowChip extends StatelessWidget {
  final RouteOverflow overflow;
  const RouteOverflowChip({super.key, required this.overflow});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final kind = overflow.switched ? StatusKind.note : StatusKind.warn;
    final color = kind.color(tokens);
    return Semantics(
      container: true,
      label: '${kind.word}: ${overflow.notice}',
      child: ExcludeSemantics(
        child: Container(
          key: const Key('route-overflow-chip'),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(SonderRadius.control),
            border: Border.all(color: tokens.hairlineStrong),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(kind.label, style: tokens.mono(11, color: color)),
              const SizedBox(width: 6),
              Flexible(
                child: Text(
                  overflow.notice,
                  key: const Key('route-overflow-notice'),
                  style: tokens.mono(11, color: tokens.muted),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
