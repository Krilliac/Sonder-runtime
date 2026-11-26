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

/// The working glyph's column and the gap after it. The glyph starts at
/// the reading column's edge, where an answer's first letter will be.
const _glyphColumn = SonderSpace.lg;
const _glyphGap = SonderSpace.sm;

/// Live streaming speed metrics derived while a turn is emitting output.
class LiveStreamMetrics {
  /// Milliseconds from turn start until the first streamed output arrived.
  final int ttftMs;

  /// Estimated generation rate in tokens/second once enough output and time
  /// have elapsed, or null while still warming up.
  final double? tokensPerSecond;

  const LiveStreamMetrics({
    required this.ttftMs,
    this.tokensPerSecond,
  });

  String get ttftLabel => 'TTFT ${durationLabel(ttftMs)}';

  String? get rateLabel {
    final tps = tokensPerSecond;
    if (tps == null || !tps.isFinite || tps <= 0) return null;
    final formatted = tps >= 100 ? '${tps.round()}' : tps.toStringAsFixed(1);
    return '~$formatted tok/s';
  }
}

/// The live line of an in-flight turn (P1-1, §2.2):
///
/// ```
/// ◈  working · reading files · 12s · sonder:latest              ■ Stop
///    TTFT 420ms  ~34.2 tok/s
///    ! no output for 24s · slow local model? try the fast route
/// ```
///
/// The ◈ leads the line and breathes once a second; the text
/// is the REPL's live line (`status_line.liveLine`) without its glyph. The
/// controller ticks [live] at 1 Hz, so the timer keeps counting with
/// animations off. After [slowTurnSeconds] with no output — no new text
/// ([outputLength]) and no progress from the server ([live] changing within
/// a second) — the glyph turns warn and the stall line opens.
///
/// When [live] clears to `null` (for example after an aborted or cancelled SSE
/// stream), the spinner clears immediately instead of showing a stuck working
/// indicator.
class LiveLineView extends StatefulWidget {
  final ValueListenable<LiveTurn?> live;
  final VoidCallback? onStop;

  /// Characters of streamed output (answer + reasoning) received so far.
  /// Growth counts as progress and drives live TTFT / tok/s metrics.
  final int outputLength;

  /// Clock override for deterministic timing tests; defaults to [DateTime.now].
  final DateTime Function()? clock;

  const LiveLineView({
    super.key,
    required this.live,
    this.onStop,
    this.outputLength = 0,
    this.clock,
  });

  @override
  State<LiveLineView> createState() => _LiveLineViewState();
}

class _LiveLineViewState extends State<LiveLineView> {
  LiveTurn? _turn;
  DateTime? _turnStartedAt;

  /// [LiveTurn.elapsedSeconds] when the turn last showed progress.
  int _lastProgress = 0;

  /// Elapsed milliseconds from turn start when output first became non-empty.
  int? _firstOutputMs;

  DateTime _now() => (widget.clock ?? DateTime.now)();

  int _elapsedMs(LiveTurn turn) {
    final wall = _now().difference(turn.startedAt).inMilliseconds;
    final ticks = turn.elapsedSeconds * 1000;
    final best = wall > ticks ? wall : ticks;
    return best < 0 ? 0 : best;
  }

  @override
  void initState() {
    super.initState();
    _turn = widget.live.value;
    _turnStartedAt = _turn?.startedAt;
    _lastProgress = _turn?.elapsedSeconds ?? 0;
    if (_turn != null && widget.outputLength > 0) {
      _firstOutputMs = _elapsedMs(_turn!);
    }
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
      if (_turn != null && _firstOutputMs == null) {
        _firstOutputMs = _elapsedMs(_turn!);
      }
    } else if (widget.outputLength == 0 && oldWidget.outputLength > 0) {
      _firstOutputMs = null;
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
      final newTurn = prev == null || next.startedAt != _turnStartedAt;
      if (newTurn) {
        _turnStartedAt = next.startedAt;
        _firstOutputMs =
            widget.outputLength > 0 ? _elapsedMs(next) : null;
      }
      // A status comment is the server explaining the wait, never progress:
      // a change of waitStatus (new text or a clear) is not a server event
      // here. A status that merely repeats the current text never reaches
      // this listener, because the controller drops it without touching
      // [LiveTurn] (a repeat is value-identical to a re-stated phase and
      // could not be told apart here).
      final tick = next.elapsedSeconds != prev?.elapsedSeconds;
      final statusChanged = next.waitStatus != prev?.waitStatus;
      final toolsChanged = !identical(next.toolCalls, prev?.toolCalls);
      final progressed = (!tick && !statusChanged) ||
          next.phase != prev?.phase ||
          next.tokensIn != prev?.tokensIn ||
          toolsChanged;
      if (newTurn || progressed) _lastProgress = next.elapsedSeconds;
    } else {
      _turnStartedAt = null;
      _firstOutputMs = null;
    }
    if (mounted) setState(() => _turn = next);
  }

  LiveStreamMetrics? _metricsFor(LiveTurn turn) {
    final firstMs = _firstOutputMs;
    if (firstMs == null || widget.outputLength <= 0) return null;
    final nowMs = _elapsedMs(turn);
    final genMs = nowMs - firstMs;
    double? tps;
    if (widget.outputLength >= 16 && genMs >= 250) {
      final estTokens = widget.outputLength / 4.0;
      tps = estTokens * 1000.0 / genMs;
    }
    return LiveStreamMetrics(ttftMs: firstMs, tokensPerSecond: tps);
  }

  @override
  Widget build(BuildContext context) {
    final turn = _turn;
    if (turn == null) {
      return const SizedBox.shrink(key: Key('live-line-idle'));
    }
    final tokens = SonderTokens.of(context);
    final seconds = turn.elapsedSeconds;
    final quiet = seconds - _lastProgress < 0 ? 0 : seconds - _lastProgress;
    final awaitingApproval = turn.phase == 'awaiting approval' ||
        turn.toolCalls
            .any((t) => t.isAwaitingApproval || t.isAwaitingClientApproval);
    final stalled = !awaitingApproval && quiet >= slowTurnSeconds;
    final state = LiveState(
      phase: turn.phase,
      elapsedS: seconds,
      model: turn.model,
      tokensIn: turn.tokensIn,
    );
    final tone = stalled ? tokens.warn : StatusKind.running.color(tokens);
    final style = tokens.mono(12, color: tokens.text2);
    final quietText = 'no output for ${elapsedLabel(quiet)}';
    // The server's own reason for the wait, when it gave one, replaces the
    // generic slow-model hint. Plain text only.
    final hint = turn.waitStatus ?? const LiveState().slowHint;
    final metrics = _metricsFor(turn);
    final metricsSpeech = metrics == null
        ? ''
        : ', ${metrics.ttftLabel}'
            '${metrics.rateLabel != null ? ', ${metrics.rateLabel}' : ''}';
    return Semantics(
      key: const Key('live-line'),
      container: true,
      label: 'Sonder Runtime is working, ${state.phase}, '
          '${elapsedLabel(seconds)}$metricsSpeech'
          '${stalled ? ', $quietText, $hint' : ''}',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              SizedBox(
                width: _glyphColumn,
                child: ExcludeSemantics(
                  child: _BreathingGlyph(
                      seconds: seconds, color: tone, still: stalled),
                ),
              ),
              const SizedBox(width: _glyphGap),
              Expanded(
                child: ExcludeSemantics(
                  child: LayoutBuilder(builder: (context, constraints) {
                    // The glyph and its space are drawn beside the text,
                    // so the REPL line gets two extra cells and drops them.
                    final cell = monoCellWidth(context, style);
                    final rawCols = cell <= 0 || !constraints.hasBoundedWidth
                        ? 80
                        : (constraints.maxWidth / cell).floor();
                    final cols = rawCols < 1 ? 1 : rawCols;
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
          if (metrics != null)
            Padding(
              padding: const EdgeInsets.only(
                left: _glyphColumn + _glyphGap,
                top: SonderSpace.xxs,
                bottom: SonderSpace.xs,
              ),
              child: ExcludeSemantics(
                child: Wrap(
                  key: const Key('live-metrics-badge'),
                  spacing: SonderSpace.xs,
                  runSpacing: SonderSpace.xs,
                  children: [
                    _LiveMetricPill(
                      key: const Key('live-ttft-badge'),
                      label: metrics.ttftLabel,
                      color: tokens.muted,
                    ),
                    if (metrics.rateLabel != null)
                      _LiveMetricPill(
                        key: const Key('live-tps-badge'),
                        label: metrics.rateLabel!,
                        color: stalled ? tokens.warn : tokens.info,
                      ),
                  ],
                ),
              ),
            ),
          SonderReveal(
            visible: stalled,
            child: Padding(
              padding: const EdgeInsets.only(
                  left: _glyphColumn + _glyphGap, bottom: SonderSpace.xs),
              child: ExcludeSemantics(
                child: Text.rich(
                  TextSpan(children: [
                    TextSpan(
                        text: '${StatusKind.warn.glyph} $quietText',
                        style: style.copyWith(
                            color: tokens.warn, fontWeight: FontWeight.w500)),
                    TextSpan(
                        text: ' ${statusGlyphs['sep']} $hint',
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

class _LiveMetricPill extends StatelessWidget {
  final String label;
  final Color color;

  const _LiveMetricPill({
    super.key,
    required this.label,
    required this.color,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(
        horizontal: SonderSpace.tight,
        vertical: SonderSpace.xxs,
      ),
      decoration: BoxDecoration(
        color: tokens.sunken,
        borderRadius: BorderRadius.circular(SonderRadius.control),
        border: Border.all(color: tokens.hairline),
      ),
      child: Text(
        label,
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: tokens.mono(11, color: color, weight: FontWeight.w500),
      ),
    );
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
