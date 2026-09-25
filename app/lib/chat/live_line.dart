import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../theme.dart';
import 'controller.dart';
import 'lines.dart';

/// Seconds after which the live line suggests a faster route.
const slowTurnSeconds = 20;

/// `◈ working · routing · 12s · sonder:latest   [Stop]` (P1-1, §2.2).
///
/// The controller ticks [live] at 1 Hz, so the elapsed seconds rebuild this
/// line and never the transcript around it.
class LiveLineView extends StatelessWidget {
  final ValueListenable<LiveTurn?> live;
  final VoidCallback? onStop;

  const LiveLineView({super.key, required this.live, this.onStop});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return ValueListenableBuilder<LiveTurn?>(
      valueListenable: live,
      builder: (context, turn, _) {
        final seconds = turn?.elapsedSeconds ?? 0;
        final state = LiveState(
          phase: turn?.phase ?? 'working',
          elapsedSeconds: seconds,
          model: turn?.model ?? '',
          tokensIn: turn?.tokensIn,
          slow: seconds >= slowTurnSeconds,
        );
        final style = tokens.mono(12, color: tokens.text2);
        return LayoutBuilder(builder: (context, constraints) {
          // Leave room for the Stop button, then fit the REPL's line to the
          // cells that remain.
          final cell = _cellWidth(context, style);
          final room = constraints.maxWidth - (onStop == null ? 0 : 88);
          final cols = cell <= 0 ? 80 : (room / cell).floor();
          final text = liveLine(state, cols);
          final slowHint = state.slow ? LiveState.defaultSlowHint : '';
          return Semantics(
            key: const Key('live-line'),
            container: true,
            label: 'Sonder Runtime is working, ${state.phase}, '
                '${elapsedLabel(seconds)}',
            child: Row(
              children: [
                Expanded(
                  child: ExcludeSemantics(
                    child: Text.rich(
                      _paint(text, slowHint, tokens, style),
                      maxLines: 1,
                      overflow: TextOverflow.clip,
                      softWrap: false,
                    ),
                  ),
                ),
                if (onStop != null)
                  Padding(
                    padding: const EdgeInsets.only(left: 8),
                    child: OutlinedButton(
                      key: const Key('live-stop'),
                      onPressed: onStop,
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(72, 40),
                        visualDensity: VisualDensity.compact,
                      ),
                      child: const Text('Stop'),
                    ),
                  ),
              ],
            ),
          );
        });
      },
    );
  }

  /// Head in accent, the slow hint in warn, the rest muted.
  static InlineSpan _paint(
      String text, String slowHint, SonderTokens tokens, TextStyle style) {
    const head = '$markGlyph working';
    final spans = <InlineSpan>[];
    var rest = text;
    if (rest.startsWith(head)) {
      spans.add(TextSpan(
          text: head,
          style: style.copyWith(
              color: tokens.accent, fontWeight: FontWeight.w600)));
      rest = rest.substring(head.length);
    }
    final i = slowHint.isEmpty ? -1 : rest.indexOf(slowHint);
    if (i >= 0) {
      spans.add(TextSpan(text: rest.substring(0, i), style: style));
      spans.add(
          TextSpan(text: slowHint, style: style.copyWith(color: tokens.warn)));
      spans.add(
          TextSpan(text: rest.substring(i + slowHint.length), style: style));
    } else {
      spans.add(TextSpan(text: rest, style: style));
    }
    return TextSpan(children: spans);
  }
}

double _cellWidth(BuildContext context, TextStyle style) {
  final painter = TextPainter(
    text: TextSpan(text: 'MMMMMMMMMM', style: style),
    textDirection: TextDirection.ltr,
    textScaler: MediaQuery.textScalerOf(context),
  )..layout();
  final w = painter.width / 10;
  painter.dispose();
  return w;
}

/// Mono cell width for [style] at the current text scale.
double monoCellWidth(BuildContext context, TextStyle style) =>
    _cellWidth(context, style);
