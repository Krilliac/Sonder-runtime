/// Pure formatters for the status strip, live line and answer footer.
///
/// Dart ports of `sonder_runtime/interfaces/repl/style.py`'s
/// `status_line`, `live_line`, `footer`, `compact_count`, `duration_label`
/// and `truncate`, with the same field order and drop order, measured in
/// terminal-style cells. They return plain strings (the app styles the
/// fields itself; see [statusSegments]). `test/status_line_test.dart`
/// asserts exact strings captured from style.py.
///
/// Width is a cell budget: the result fits in `width - 1` cells, exactly as
/// in the REPL, so a caller converting pixels to cells can reuse the same
/// numbers the terminal tests use.
library;

import 'status_vocab.dart';

const _sep = ' · ';
const _ellipsis = '…';

/// Cells a code point occupies: 0 for combining marks and joiners, 2 for
/// East Asian wide/fullwidth and emoji, 1 otherwise (style.py `_char_cells`).
int _runeCells(int rune) {
  if ((rune >= 0x0300 && rune <= 0x036F) ||
      (rune >= 0xFE00 && rune <= 0xFE0F) ||
      rune == 0x200C ||
      rune == 0x200D) {
    return 0;
  }
  if ((rune >= 0x1100 && rune <= 0x115F) ||
      (rune >= 0x2E80 && rune <= 0x303E) ||
      (rune >= 0x3041 && rune <= 0x33FF) ||
      (rune >= 0x3400 && rune <= 0x4DBF) ||
      (rune >= 0x4E00 && rune <= 0x9FFF) ||
      (rune >= 0xA000 && rune <= 0xA4CF) ||
      (rune >= 0xAC00 && rune <= 0xD7A3) ||
      (rune >= 0xF900 && rune <= 0xFAFF) ||
      (rune >= 0xFE30 && rune <= 0xFE4F) ||
      (rune >= 0xFF00 && rune <= 0xFF60) ||
      (rune >= 0xFFE0 && rune <= 0xFFE6) ||
      (rune >= 0x1F300 && rune <= 0x1F64F) ||
      (rune >= 0x1F900 && rune <= 0x1F9FF) ||
      (rune >= 0x20000 && rune <= 0x3FFFD)) {
    return 2;
  }
  return 1;
}

/// Terminal cells [text] occupies (style.py `cell_width`, without ANSI).
int cellWidth(String text) {
  var cells = 0;
  for (final rune in text.runes) {
    cells += _runeCells(rune);
  }
  return cells;
}

/// Cut [text] to [width] cells, ending with `…` (style.py `truncate`).
String truncateCells(String text, int width) {
  if (width < 0) width = 0;
  if (cellWidth(text) <= width) return text;
  final room = width - cellWidth(_ellipsis);
  if (room <= 0) return _ellipsis.substring(0, width.clamp(0, 1));
  final out = StringBuffer();
  var used = 0;
  for (final rune in text.runes) {
    final size = _runeCells(rune);
    if (used + size > room) break;
    out.writeCharCode(rune);
    used += size;
  }
  return '$out$_ellipsis';
}

/// Collapse all whitespace runs to one space and trim (style.py `_clean`).
String cleanText(String? value) => (value ?? '')
    .split(RegExp(r'\s+'))
    .where((part) => part.isNotEmpty)
    .join(' ');

/// Python's `'%.1f' % (numerator / denominator)`, including its
/// round-half-to-even on exactly representable ties (1.25 → "1.2"), which
/// Dart's `toStringAsFixed` rounds up.
String _oneDecimal(num numerator, num denominator) {
  final value = numerator / denominator;
  if (numerator is int && denominator is int) {
    final tenths = numerator * 10;
    final exactTie = (tenths % denominator) * 2 == denominator;
    // Only .25/.75 ties are exact binary fractions; every other decimal tie
    // is stored above or below .5 and both languages then agree.
    final dyadic = (numerator * 4) % denominator == 0;
    if (exactTie && dyadic) {
      final floor = tenths ~/ denominator;
      final even = floor.isEven ? floor : floor + 1;
      final whole = even ~/ 10;
      final frac = even % 10;
      return '$whole.$frac';
    }
  }
  return value.toStringAsFixed(1);
}

/// `64` → `64`, `8192` → `8.2k`, `1250000` → `1.2M` (style.py `compact_count`).
String compactCount(num value) {
  final n = value;
  if (n.abs() < 1000) return n.truncate().toString();
  final String text;
  if (n.abs() < 1000000) {
    text = '${_oneDecimal(n is int ? n : n.toDouble(), 1000)}k';
  } else {
    text = '${_oneDecimal(n is int ? n : n.toDouble(), 1000000)}M';
  }
  return text.replaceAll('.0k', 'k').replaceAll('.0M', 'M');
}

/// `231` → `231ms`, `75700` → `75.7s`, `135000` → `2m 15s`
/// (style.py `duration_label`).
String durationLabel(int ms) {
  if (ms < 0) ms = 0;
  if (ms < 1000) return '${ms}ms';
  if (ms < 100000) return '${_oneDecimal(ms, 1000)}s';
  final seconds = ms ~/ 1000;
  final minutes = seconds ~/ 60;
  final rest = seconds % 60;
  return '${minutes}m ${rest.toString().padLeft(2, '0')}s';
}

/// `12s`, `1m 15s`: the live line's elapsed-seconds form.
String elapsedLabel(num seconds) {
  final s = seconds < 0 ? 0 : seconds.truncate();
  if (s < 60) return '${s}s';
  return '${s ~/ 60}m ${(s % 60).toString().padLeft(2, '0')}s';
}

String _join(Iterable<String> parts) =>
    parts.where((part) => part.isNotEmpty).join(_sep);

// ---------------------------------------------------------------------------
// Status line
// ---------------------------------------------------------------------------

/// Persistent session state shown under the composer (style.py `StatusState`).
class StatusState {
  final String mode;
  final String tier;
  final String model;
  final int? ctxUsed;
  final int? ctxLimit;
  final int agents;
  final int lanes;
  final String project;
  final bool elevated;
  final String elevatedReason;

  /// App-only: calls waiting for approval ("1 pending approval"). Dropped
  /// with the agent counts; zero is hidden.
  final int pendingApprovals;

  const StatusState({
    this.mode = 'manual',
    this.tier = 'code',
    this.model = '',
    this.ctxUsed,
    this.ctxLimit,
    this.agents = 0,
    this.lanes = 0,
    this.project = 'default',
    this.elevated = false,
    this.elevatedReason = '',
    this.pendingApprovals = 0,
  });
}

/// What a status field is, so a widget can colour it: only the tier (info)
/// and the mode word (its mode role) carry colour in the REPL.
enum StatusField { tier, model, mode, elevated, ctx, agents, project }

/// One field of the status line.
class StatusSegment {
  final StatusField field;
  final String text;
  const StatusSegment(this.field, this.text);

  @override
  String toString() => text;
}

List<StatusSegment> _statusFields(
  StatusState st, {
  required String? model,
  required bool reason,
  required bool ctx,
  required bool tier,
  required bool agents,
  required bool project,
}) {
  final parts = <StatusSegment>[];
  if (tier && st.tier.isNotEmpty) {
    parts.add(StatusSegment(StatusField.tier, cleanText(st.tier)));
  }
  if (model != null && model.isNotEmpty) {
    parts.add(StatusSegment(StatusField.model, model));
  }
  final word = cleanText(st.mode);
  var mode = word.isEmpty ? 'unknown' : word;
  if (st.elevated) {
    mode += ' ELEVATED';
    if (reason && st.elevatedReason.isNotEmpty) {
      mode += ' (${cleanText(st.elevatedReason)})';
    }
  }
  parts.add(StatusSegment(
      st.elevated ? StatusField.elevated : StatusField.mode, mode));
  final limit = st.ctxLimit;
  if (ctx && limit != null && limit != 0) {
    parts.add(StatusSegment(StatusField.ctx,
        'ctx ${compactCount(st.ctxUsed ?? 0)}/${compactCount(limit)}'));
  }
  if (agents) {
    if (st.agents != 0) {
      parts.add(StatusSegment(StatusField.agents,
          '${st.agents} ${st.agents != 1 ? 'agents' : 'agent'}'));
    }
    if (st.lanes != 0) {
      parts.add(StatusSegment(StatusField.agents,
          '${st.lanes} ${st.lanes != 1 ? 'lanes' : 'lane'}'));
    }
    if (st.pendingApprovals != 0) {
      parts.add(StatusSegment(StatusField.agents,
          '${st.pendingApprovals} pending approval${st.pendingApprovals != 1 ? 's' : ''}'));
    }
  }
  if (project && st.project.isNotEmpty && st.project != 'default') {
    parts.add(
        StatusSegment(StatusField.project, 'proj ${cleanText(st.project)}'));
  }
  return parts;
}

String _segmentsText(List<StatusSegment> parts) =>
    _join(parts.map((part) => part.text));

/// The status fields that fit [width] cells, in display order.
///
/// Fields leave in priority order (project, agents/lanes, elevation reason,
/// model shortened then dropped, ctx, tier); the mode word is never dropped.
/// When even the last step overflows, the caller gets the tier-less fields
/// and should ellipsize ([statusLine] does).
List<StatusSegment> statusSegments(StatusState st, int width) {
  if (width < 1) width = 1;
  final limit = width - 1;
  final model = cleanText(st.model);
  String? baseModel = model.isEmpty ? null : model;
  if (width < 40) baseModel = null;
  final baseAgents = width >= 60;
  final baseProject = width >= 80;
  // (project, agents, reason, model: keep|shorten|drop, ctx, tier)
  const steps = <(bool, bool, bool, String, bool, bool)>[
    (true, true, true, 'keep', true, true),
    (false, true, true, 'keep', true, true),
    (false, false, true, 'keep', true, true),
    (false, false, false, 'keep', true, true),
    (false, false, false, 'shorten', true, true),
    (false, false, false, 'drop', true, true),
    (false, false, false, 'drop', false, true),
    (false, false, false, 'drop', false, false),
  ];
  var parts = <StatusSegment>[];
  for (final (project, agents, reason, modelStep, ctx, tier) in steps) {
    String? shownModel = modelStep == 'drop' ? null : baseModel;
    if (modelStep == 'shorten') {
      if (baseModel == null) continue;
      final probe = _segmentsText(_statusFields(st,
          model: '\u0000',
          reason: reason,
          ctx: ctx,
          tier: tier,
          agents: baseAgents && agents,
          project: baseProject && project));
      final room = limit - (cellWidth(probe) - 1);
      if (room < 6) continue;
      shownModel = truncateCells(model, room);
    }
    parts = _statusFields(st,
        model: shownModel,
        reason: reason,
        ctx: ctx,
        tier: tier,
        agents: baseAgents && agents,
        project: baseProject && project);
    if (cellWidth(_segmentsText(parts)) <= limit) return parts;
  }
  return parts;
}

/// `code · sonder:latest · manual · ctx 64/8.2k`, fitting `width - 1` cells
/// (style.py `status_line`).
String statusLine(StatusState st, int width) {
  final line = _segmentsText(statusSegments(st, width));
  return truncateCells(line, (width < 1 ? 1 : width) - 1);
}

// ---------------------------------------------------------------------------
// Live line
// ---------------------------------------------------------------------------

/// What the in-flight turn is doing right now (style.py `LiveState`).
class LiveState {
  final String phase;
  final num elapsedS;
  final String model;
  final int? tokensIn;
  final bool slow;
  final String slowHint;

  /// The cancel hint. The app draws **Stop** as a button beside the line,
  /// so it is empty by default; the REPL's is `Ctrl-C cancels`.
  final String cancelHint;

  const LiveState({
    this.phase = 'routing',
    this.elapsedS = 0,
    this.model = '',
    this.tokensIn,
    this.slow = false,
    this.slowHint = 'slow local model? try the fast route',
    this.cancelHint = '',
  });
}

/// `◈ working · routing · 12s · sonder:latest`, fitting `width - 1` cells.
///
/// Drop order as style.py `live_line`: model, token count, cancel hint,
/// then the slow hint.
String liveLine(LiveState st, int width) {
  if (width < 1) width = 1;
  final head = '${StatusKind.running.glyph} working';
  final phase = cleanText(st.phase).isEmpty ? 'working' : cleanText(st.phase);
  final when = elapsedLabel(st.elapsedS);
  final model = cleanText(st.model);
  final tokens = st.tokensIn;
  final tok =
      tokens != null && tokens != 0 ? '${compactCount(tokens)} tok in' : '';
  final slow = st.slow ? cleanText(st.slowHint) : '';
  final cancel = cleanText(st.cancelHint);
  final candidates = <List<String>>[
    [head, phase, when, model, tok, slow, cancel],
    [head, phase, when, tok, slow, cancel],
    [head, phase, when, slow, cancel],
    [head, phase, when, slow],
    [head, phase, when, cancel],
    [head, phase, when],
  ];
  var line = '';
  for (final parts in candidates) {
    line = _join(parts);
    if (cellWidth(line) <= width - 1) return line;
  }
  return truncateCells(line, width - 1);
}

// ---------------------------------------------------------------------------
// Footer
// ---------------------------------------------------------------------------

/// Per-turn metrics; the footer is the only place they appear
/// (style.py `FooterState`).
class FooterState {
  final int elapsedMs;
  final bool ok;
  final int? modelCalls;
  final int? tokensIn;
  final int? tokensOut;
  final int? toolCalls;

  /// Shown after a failure as `hint: …`.
  final String hint;

  /// Shown last after a success: the REPL's `rate: /pass /fail`. The app
  /// draws its rating as chips, so it is empty by default.
  final String action;

  const FooterState({
    this.elapsedMs = 0,
    this.ok = true,
    this.modelCalls,
    this.tokensIn,
    this.tokensOut,
    this.toolCalls,
    this.hint = '',
    this.action = '',
  });
}

/// `done 75.7s · 2 model calls · 2.6k→43 tok`, fitting `width - 1` cells
/// with [indent] (style.py `footer`, whose indent is two spaces). Metrics
/// drop before the action; the hint is truncated last.
String footerLine(FooterState st, int width, {String indent = ''}) {
  if (width < 1) width = 1;
  final when = durationLabel(st.elapsedMs);
  final parts = [st.ok ? 'done $when' : 'failed after $when'];
  final optional = <String>[];
  final calls = st.modelCalls;
  if (calls != null && calls != 0) {
    optional.add('$calls model call${calls == 1 ? '' : 's'}');
  }
  final tools = st.toolCalls;
  if (tools != null && tools != 0) {
    optional.add('$tools tool${tools == 1 ? '' : 's'}');
  }
  final tin = st.tokensIn;
  final tout = st.tokensOut;
  if (tin != null && tout != null && (tin != 0 || tout != 0)) {
    optional.add(
        '${compactCount(tin)}${statusGlyphs['arrow']}${compactCount(tout)} tok');
  }
  final tail = <String>[];
  if (!st.ok && st.hint.isNotEmpty) {
    tail.add('hint: ${cleanText(st.hint)}');
  } else if (st.ok && st.action.isNotEmpty) {
    tail.add(st.action);
  }
  String? text;
  for (var count = optional.length; count >= 0; count--) {
    final candidate =
        indent + [...parts, ...optional.take(count), ...tail].join(_sep);
    if (cellWidth(candidate) <= width - 1) {
      text = candidate;
      break;
    }
  }
  text ??= truncateCells(indent + [...parts, ...tail].join(_sep), width - 1);
  if (cellWidth(text) > width - 1) text = truncateCells(text, width - 1);
  return text.trimRight();
}
