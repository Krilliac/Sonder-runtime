import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../theme.dart';
import '../ui/kit.dart' show SonderReveal;
import '../ui/status_line.dart';
import '../ui/status_vocab.dart';
import 'controller.dart';

/// Seconds without output after which the live line says so (the stall
/// cue) and suggests a faster route.
const slowTurnSeconds = 20;

/// The transcript's glyph gutter (❯ you, ◈ Sonder) and the gap after it.
/// The live line puts its working glyph in the same column.
const transcriptGutter = 24.0;
const transcriptGutterGap = SonderSpace.md;

/// The live line of an in-flight turn (P1-1, §2.2):
///
/// ```
/// ◈  working · reading files · 12s · sonder:latest              ■ Stop
///    ! no output for 24s · slow local model? try the fast route
/// ```
///
/// The ◈ sits in the transcript gutter and breathes once a second; the text
/// is the REPL's live line (`status_line.liveLine`) without its glyph. The
/// controller ticks [live] at 1 Hz, so the timer keeps counting with
/// animations off. After [slowTurnSeconds] with no output — no new text
/// ([outputLength]) and no progress from the server ([live] changing within
/// a second) — the glyph turns warn and the stall line opens.
class LiveLineView extends StatefulWidget {
  final ValueListenable<LiveTurn?> live;
  final VoidCallback? onStop;

  /// Characters of answer text received so far. Growth is output.
  final int outputLength;

  const LiveLineView({
    super.key,
    required this.live,
    this.onStop,
    this.outputLength = 0,
  });

  @override
  State<LiveLineView> createState() => _LiveLineViewState();
}

class _LiveLineViewState extends State<LiveLineView> {
  LiveTurn? _turn;

  /// [LiveTurn.elapsedSeconds] when the turn last showed progress.
  int _lastProgress = 0;

  @override
  void initState() {
    super.initState();
    _turn = widget.live.value;
    _lastProgress = _turn?.elapsedSeconds ?? 0;
    widget.live.addListener(_onLive);
  }

  @override
  void didUpdateWidget(covariant LiveLineView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.live, widget.live)) {
      oldWidget.live.removeListener(_onLive);
      widget.live.addListener(_onLive);
      _onLive();
    }
    if (widget.outputLength > oldWidget.outputLength) {
      _lastProgress = _turn?.elapsedSeconds ?? _lastProgress;
    }
  }

  @override
  void dispose() {
    widget.live.removeListener(_onLive);
    super.dispose();
  }

  /// Every change of [LiveTurn] is either the controller's clock tick (the
  /// elapsed seconds advance) or the server reporting progress (a phase
  /// event, which leaves the seconds alone). Only the second resets the
  /// stall clock.
  void _onLive() {
    final next = widget.live.value;
    final prev = _turn;
    if (next != null) {
      final newTurn = prev == null || next.startedAt != prev.startedAt;
      final progressed = next.elapsedSeconds == prev?.elapsedSeconds ||
          next.phase != prev?.phase ||
          next.tokensIn != prev?.tokensIn;
      if (newTurn || progressed) _lastProgress = next.elapsedSeconds;
    }
    if (mounted) setState(() => _turn = next);
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final turn = _turn;
    final seconds = turn?.elapsedSeconds ?? 0;
    final quiet = seconds - _lastProgress < 0 ? 0 : seconds - _lastProgress;
    final stalled = turn != null && quiet >= slowTurnSeconds;
    final state = LiveState(
      phase: turn?.phase ?? 'working',
      elapsedS: seconds,
      model: turn?.model ?? '',
      tokensIn: turn?.tokensIn,
    );
    final tone = stalled ? tokens.warn : StatusKind.running.color(tokens);
    final style = tokens.mono(12, color: tokens.text2);
    final quietText = 'no output for ${elapsedLabel(quiet)}';
    return Semantics(
      key: const Key('live-line'),
      container: true,
      label: 'Sonder Runtime is working, ${state.phase}, '
          '${elapsedLabel(seconds)}${stalled ? ', $quietText' : ''}',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              SizedBox(
                width: transcriptGutter,
                child: ExcludeSemantics(
                  child: _BreathingGlyph(
                      seconds: seconds, color: tone, still: stalled),
                ),
              ),
              const SizedBox(width: transcriptGutterGap),
              Expanded(
                child: ExcludeSemantics(
                  child: LayoutBuilder(builder: (context, constraints) {
                    // The glyph and its space live in the gutter, so the
                    // REPL line gets two extra cells and drops them.
                    final cell = monoCellWidth(context, style);
                    final cols =
                        cell <= 0 ? 80 : (constraints.maxWidth / cell).floor();
                    var line = liveLine(state, cols + 2);
                    final glyph = '${StatusKind.running.glyph} ';
                    if (line.startsWith(glyph)) {
                      line = line.substring(glyph.length);
                    }
                    return Text.rich(
                      _paint(line, tone, style),
                      key: const Key('live-line-text'),
                      maxLines: 1,
                      overflow: TextOverflow.clip,
                      softWrap: false,
                    );
                  }),
                ),
              ),
              if (widget.onStop != null)
                Padding(
                  padding: const EdgeInsets.only(left: SonderSpace.sm),
                  child: TextButton.icon(
                    key: const Key('live-stop'),
                    onPressed: widget.onStop,
                    icon: const Icon(Icons.stop_rounded, size: 18),
                    label: const Text('Stop'),
                    style: TextButton.styleFrom(
                      minimumSize: const Size(0, 32),
                      padding: const EdgeInsets.symmetric(
                          horizontal: SonderSpace.md),
                      tapTargetSize: MaterialTapTargetSize.padded,
                      foregroundColor: tokens.text2,
                    ),
                  ),
                ),
            ],
          ),
          SonderReveal(
            visible: stalled,
            child: Padding(
              padding: const EdgeInsets.only(
                  left: transcriptGutter + transcriptGutterGap,
                  bottom: SonderSpace.xs),
              child: ExcludeSemantics(
                child: Text.rich(
                  TextSpan(children: [
                    TextSpan(
                        text: '${StatusKind.warn.glyph} $quietText',
                        style: style.copyWith(
                            color: tokens.warn, fontWeight: FontWeight.w500)),
                    TextSpan(
                        text:
                            ' ${statusGlyphs['sep']} ${const LiveState().slowHint}',
                        style: style),
                  ]),
                  key: const Key('live-stall'),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  /// "working" in the live tone, the rest in the secondary text colour.
  static InlineSpan _paint(String line, Color tone, TextStyle style) {
    const head = 'working';
    if (!line.startsWith(head)) return TextSpan(text: line, style: style);
    return TextSpan(children: [
      TextSpan(
          text: head,
          style: style.copyWith(color: tone, fontWeight: FontWeight.w600)),
      TextSpan(text: line.substring(head.length), style: style),
    ]);
  }
}

/// The working glyph. It eases between full and half strength on the
/// controller's 1 Hz tick, so it never schedules frames of its own between
/// ticks; with reduced motion, or once stalled, it holds still.
class _BreathingGlyph extends StatelessWidget {
  final int seconds;
  final Color color;
  final bool still;

  const _BreathingGlyph({
    required this.seconds,
    required this.color,
    required this.still,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final reduce = MediaQuery.maybeDisableAnimationsOf(context) == true;
    final dim = !reduce && !still && seconds.isOdd;
    return AnimatedOpacity(
      key: const Key('live-glyph'),
      opacity: dim ? 0.45 : 1,
      duration: reduce ? Duration.zero : SonderMotion.slow,
      curve: SonderMotion.standard,
      child: Text(StatusKind.running.glyph,
          textAlign: TextAlign.center,
          style: tokens.mono(14, color: color, weight: FontWeight.w600)),
    );
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
