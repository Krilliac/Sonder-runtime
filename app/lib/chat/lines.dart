/// Dart ports of the REPL's line formatters (sonder_runtime/interfaces/repl/
/// style.py): `compact_count`, `duration_label`, `status_line`, `live_line`
/// and `footer`, with the same field order and drop order.
///
/// Lane B owns the shared copy of these in `lib/ui/status_line.dart`. Until
/// that lands, chat keeps this private port so the transcript, status strip
/// and live line speak the REPL's words today; swapping the import is the
/// only change needed once the shared one exists. Widths are in character
/// cells, exactly like the terminal: the widgets measure a mono cell and
/// divide.
library;

const sepGlyph = '·';
const markGlyph = '◈';
const arrowGlyph = '→';
const ellipsisGlyph = '…';

String _sep() => ' $sepGlyph ';

/// Python's `'%.1f' % x`: like [double.toStringAsFixed] except that an exact
/// binary tie (x.25, x.75) rounds half to even, as CPython does.
String fixed1(double x) {
  final quarter = x * 4;
  final scaled = x * 10;
  final frac = scaled - scaled.floorToDouble();
  if (quarter == quarter.roundToDouble() && frac == 0.5) {
    final down = scaled.floorToDouble();
    final even = down % 2 == 0 ? down : down + 1;
    return (even / 10).toStringAsFixed(1);
  }
  return x.toStringAsFixed(1);
}

/// `64` -> `64`, `8192` -> `8.2k`, `1250000` -> `1.2M` (style.compact_count).
String compactCount(num value) {
  final n = value.toDouble();
  if (n.abs() < 1000) return '${n.truncate()}';
  String text;
  if (n.abs() < 1000000) {
    text = '${fixed1(n / 1000)}k';
  } else {
    text = '${fixed1(n / 1000000)}M';
  }
  return text.replaceAll('.0k', 'k').replaceAll('.0M', 'M');
}

/// `231` -> `231ms`, `75700` -> `75.7s`, `135000` -> `2m 15s`
/// (style.duration_label).
String durationLabel(int ms) {
  final v = ms < 0 ? 0 : ms;
  if (v < 1000) return '${v}ms';
  if (v < 100000) return '${fixed1(v / 1000)}s';
  final seconds = v ~/ 1000;
  final m = seconds ~/ 60;
  final s = seconds % 60;
  return '${m}m ${s.toString().padLeft(2, '0')}s';
}

/// Seconds as the live line shows them: `12s`, `4m 05s`.
String elapsedLabel(int seconds) {
  final v = seconds < 0 ? 0 : seconds;
  if (v < 60) return '${v}s';
  return '${v ~/ 60}m ${(v % 60).toString().padLeft(2, '0')}s';
}

String _clean(String value) =>
    value.split(RegExp(r'\s+')).where((p) => p.isNotEmpty).join(' ');

/// Cut [text] to [width] cells with a trailing ellipsis (style.truncate).
String truncateCells(String text, int width) {
  if (width <= 0) return '';
  final runes = text.runes.toList();
  if (runes.length <= width) return text;
  if (width == 1) return ellipsisGlyph;
  return '${String.fromCharCodes(runes.take(width - 1))}$ellipsisGlyph';
}

int cellWidth(String text) => text.runes.length;

/// Persistent session state shown under the composer (style.StatusState).
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
  });

  @override
  bool operator ==(Object other) =>
      other is StatusState &&
      other.mode == mode &&
      other.tier == tier &&
      other.model == model &&
      other.ctxUsed == ctxUsed &&
      other.ctxLimit == ctxLimit &&
      other.agents == agents &&
      other.lanes == lanes &&
      other.project == project &&
      other.elevated == elevated &&
      other.elevatedReason == elevatedReason;

  @override
  int get hashCode => Object.hash(mode, tier, model, ctxUsed, ctxLimit, agents,
      lanes, project, elevated, elevatedReason);
}

/// One field of the status line, tagged so the widget can colour the mode
/// word (and only the tier and mode carry colour, as in the REPL).
class StatusField {
  final String text;
  final StatusFieldKind kind;
  const StatusField(this.text, this.kind);

  @override
  String toString() => text;
}

enum StatusFieldKind { tier, model, mode, elevated, other }

List<StatusField> _statusFields(
  StatusState st, {
  required String? model,
  required bool reason,
  required bool ctx,
  required bool tier,
  required bool agents,
  required bool project,
}) {
  final parts = <StatusField>[];
  if (tier && st.tier.isNotEmpty) {
    parts.add(StatusField(_clean(st.tier), StatusFieldKind.tier));
  }
  if (model != null && model.isNotEmpty) {
    parts.add(StatusField(model, StatusFieldKind.model));
  }
  final modeWord = _clean(st.mode);
  parts.add(StatusField(
      modeWord.isEmpty ? 'unknown' : modeWord, StatusFieldKind.mode));
  if (st.elevated) {
    var badge = 'ELEVATED';
    if (reason && st.elevatedReason.isNotEmpty) {
      badge += ' (${_clean(st.elevatedReason)})';
    }
    parts.add(StatusField(badge, StatusFieldKind.elevated));
  }
  if (ctx && (st.ctxLimit ?? 0) > 0) {
    parts.add(StatusField(
      'ctx ${compactCount(st.ctxUsed ?? 0)}/${compactCount(st.ctxLimit!)}',
      StatusFieldKind.other,
    ));
  }
  if (agents) {
    if (st.agents > 0) {
      parts.add(StatusField(
          '${st.agents} ${st.agents != 1 ? 'agents' : 'agent'}',
          StatusFieldKind.other));
    }
    if (st.lanes > 0) {
      parts.add(StatusField('${st.lanes} ${st.lanes != 1 ? 'lanes' : 'lane'}',
          StatusFieldKind.other));
    }
  }
  if (project && st.project.isNotEmpty && st.project != 'default') {
    parts.add(StatusField('proj ${_clean(st.project)}', StatusFieldKind.other));
  }
  return parts;
}

/// The mode word and the elevation badge render as one field in the REPL
/// ("manual ELEVATED"); here they are separate fields joined by a space so
/// the widget can badge the elevation.
String joinStatusFields(List<StatusField> fields) {
  final buffer = StringBuffer();
  for (var i = 0; i < fields.length; i++) {
    if (i > 0) {
      buffer.write(fields[i].kind == StatusFieldKind.elevated ? ' ' : _sep());
    }
    buffer.write(fields[i].text);
  }
  return buffer.toString();
}

/// `code · sonder:latest · manual · ctx 2.1k/8.2k · 2 agents`
/// (style.status_line). Fields leave in priority order (project,
/// agents/lanes, elevation reason, model shortened then dropped, ctx, tier);
/// the mode word is never dropped. Fits `width - 1` cells.
List<StatusField> statusLineFields(StatusState st, int width) {
  final w = width < 1 ? 1 : width;
  final limit = w - 1;
  final model = _clean(st.model);
  final baseModel = w < 40 || model.isEmpty ? null : model;
  final steps = <Map<String, Object?>>[
    {},
    {'project': false},
    {'project': false, 'agents': false},
    {'project': false, 'agents': false, 'reason': false},
    {'project': false, 'agents': false, 'reason': false, 'model': 'shorten'},
    {'project': false, 'agents': false, 'reason': false, 'model': null},
    {
      'project': false,
      'agents': false,
      'reason': false,
      'model': null,
      'ctx': false
    },
    {
      'project': false,
      'agents': false,
      'reason': false,
      'model': null,
      'ctx': false,
      'tier': false
    },
  ];
  var line = <StatusField>[];
  for (final step in steps) {
    String? m = step.containsKey('model')
        ? (step['model'] == 'shorten' ? 'shorten' : null)
        : baseModel;
    bool opt(String key, bool fallback) =>
        step.containsKey(key) ? step[key] as bool : fallback;
    final reason = opt('reason', true);
    final ctx = opt('ctx', true);
    final tier = opt('tier', true);
    final agents = opt('agents', w >= 60);
    final project = opt('project', w >= 80);
    if (m == 'shorten') {
      if (baseModel == null) continue;
      final probe = joinStatusFields(_statusFields(st,
          model: '\u0000',
          reason: reason,
          ctx: ctx,
          tier: tier,
          agents: agents,
          project: project));
      final room = limit - (cellWidth(probe) - 1);
      if (room < 6) continue;
      m = truncateCells(model, room);
    }
    line = _statusFields(st,
        model: m,
        reason: reason,
        ctx: ctx,
        tier: tier,
        agents: agents,
        project: project);
    if (cellWidth(joinStatusFields(line)) <= limit) return line;
  }
  return line;
}

String statusLine(StatusState st, int width) {
  final fields = statusLineFields(st, width);
  final text = joinStatusFields(fields);
  final limit = (width < 1 ? 1 : width) - 1;
  return cellWidth(text) <= limit ? text : truncateCells(text, limit);
}

/// What the in-flight turn is doing right now (style.LiveState).
class LiveState {
  final String phase;
  final int elapsedSeconds;
  final String model;
  final int? tokensIn;
  final bool slow;
  final String slowHint;

  const LiveState({
    this.phase = 'routing',
    this.elapsedSeconds = 0,
    this.model = '',
    this.tokensIn,
    this.slow = false,
    this.slowHint = defaultSlowHint,
  });

  /// The app cannot type `/model fast`, so it names the action instead.
  static const defaultSlowHint = 'slow local model? try the fast route';
}

/// `◈ working · routing · 12s · sonder:latest` (style.live_line), without
/// the terminal's "Ctrl-C cancels" hint: the app renders Stop as a button
/// next to the line. Drop order: model, token count, slow hint.
String liveLine(LiveState st, int width) {
  final w = width < 1 ? 1 : width;
  const head = '$markGlyph working';
  final phase = _clean(st.phase).isEmpty ? 'working' : _clean(st.phase);
  final when = elapsedLabel(st.elapsedSeconds);
  final model = _clean(st.model);
  final tok =
      (st.tokensIn ?? 0) > 0 ? '${compactCount(st.tokensIn!)} tok in' : '';
  final slow = st.slow ? _clean(st.slowHint) : '';
  final candidates = <List<String>>[
    [head, phase, when, model, tok, slow],
    [head, phase, when, tok, slow],
    [head, phase, when, slow],
    [head, phase, when],
  ];
  var line = '';
  for (final parts in candidates) {
    line = parts.where((p) => p.isNotEmpty).join(_sep());
    if (cellWidth(line) <= w - 1) return line;
  }
  return truncateCells(line, w - 1);
}

/// Per-turn metrics; the footer is the only place they appear
/// (style.FooterState).
class FooterState {
  final int elapsedMs;
  final bool ok;
  final int? modelCalls;
  final int? tokensIn;
  final int? tokensOut;
  final int? toolCalls;
  final String hint;

  /// `full` -> `rate: /pass /fail`, `short` -> `/pass /fail`, `` -> none.
  /// The app renders its rating controls as buttons, so it passes ``.
  final String rate;

  const FooterState({
    this.elapsedMs = 0,
    this.ok = true,
    this.modelCalls,
    this.tokensIn,
    this.tokensOut,
    this.toolCalls,
    this.hint = '',
    this.rate = '',
  });
}

/// `done 75.7s · 2 model calls · 2.6k→43 tok` (style.footer, without the
/// two-cell indent: the transcript's reading column already indents it).
/// Metrics drop before the action; the hint is truncated last.
String footerLine(FooterState st, {int width = 1 << 20}) {
  final w = width < 1 ? 1 : width;
  final when = durationLabel(st.elapsedMs);
  final parts = [st.ok ? 'done $when' : 'failed after $when'];
  final optional = <String>[];
  final mc = st.modelCalls ?? 0;
  if (mc > 0) optional.add('$mc model call${mc == 1 ? '' : 's'}');
  final tc = st.toolCalls ?? 0;
  if (tc > 0) optional.add('$tc tool${tc == 1 ? '' : 's'}');
  if (st.tokensIn != null &&
      st.tokensOut != null &&
      (st.tokensIn! > 0 || st.tokensOut! > 0)) {
    optional.add(
        '${compactCount(st.tokensIn!)}$arrowGlyph${compactCount(st.tokensOut!)} tok');
  }
  final tail = <String>[];
  if (!st.ok && st.hint.isNotEmpty) {
    tail.add('hint: ${_clean(st.hint)}');
  } else if (st.ok && st.rate == 'full') {
    tail.add('rate: /pass /fail');
  } else if (st.ok && st.rate == 'short') {
    tail.add('/pass /fail');
  }
  // The REPL's two-cell indent is part of its width budget; keep the budget
  // identical so a port test at a given width drops the same fields.
  String? text;
  for (var count = optional.length; count >= 0; count--) {
    final candidate =
        '  ${[...parts, ...optional.take(count), ...tail].join(_sep())}';
    if (cellWidth(candidate) <= w - 1) {
      text = candidate;
      break;
    }
  }
  text ??= truncateCells('  ${[...parts, ...tail].join(_sep())}', w - 1);
  if (cellWidth(text) > w - 1) text = truncateCells(text, w - 1);
  return text.trimRight().replaceFirst(RegExp(r'^  '), '');
}
