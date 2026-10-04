/// What an in-flight turn is doing, from what the server actually reports.
///
/// The chat SSE stream carries no phase events: it commits early (response
/// headers, then `: keep-alive` comments) and then sends answer deltas. The
/// server's activity tracker, which the chat status poll already reads
/// once a second during a turn (`GET /v1/sonder/status`, local owner and
/// administrators), records the turn's span: model calls, tool calls,
/// escalations. This combines both, with the REPL's live-line rules
/// (`interfaces/repl/repl.py` `_WorkingIndicator.state`):
///
/// * before the server answers: `routing`;
/// * the server committed to the turn, or its span exists: `thinking`;
/// * the last span event was a tool call: the tool's name;
/// * a model call has returned: `model call N+1` (the next one runs);
/// * answer text is streaming: `writing`.
///
/// Without activity (another account's server, an older runtime) the phase
/// follows the stream alone.
library;

import '../api.dart' show ActivityResponse, ActivityStatus;

/// Activity kinds with a clearer word than the REPL's fallback.
const _namedPhases = <String, String>{
  'model_escalation': 'escalating',
  'model_retry': 'retrying',
  'model_context_compaction': 'compacting context',
  'work_plan': 'planning',
};

/// The kind of [span]'s newest event (`tool_call`, `model_call`, …), or
/// empty. The app's activity model keeps events as `+120ms kind detail`.
String lastActivityKind(ActivityResponse span) {
  if (span.events.isEmpty) return '';
  final parts = span.events.last.trim().split(RegExp(r'\s+'));
  return parts.length >= 2 ? parts[1] : '';
}

/// The live-line phase for [span], as the REPL words it.
String activityPhase(ActivityResponse span) {
  final kind = lastActivityKind(span);
  if (kind == 'tool_call' || kind == 'tool_result') {
    final tool = span.actions.isEmpty ? '' : span.actions.last.tool.trim();
    return tool.isEmpty ? 'tool' : tool;
  }
  final named = _namedPhases[kind];
  if (named != null) return named;
  if (span.modelCalls > 0 || kind == 'model_call') {
    return 'model call ${span.modelCalls + 1}';
  }
  return 'thinking';
}

/// Tracks one turn. Feed it stream events and status readings; each call
/// says whether the person should see an update (the phase changed, or the
/// server recorded new activity under the same phase).
class TurnProgress {
  /// The model the turn was sent with; the server labels the turn's span
  /// `chat:<model>`.
  final String model;

  /// Spans already running before this turn started: never this turn's.
  final Set<String> _preexisting;

  TurnProgress({required this.model, Set<String> preexisting = const {}})
      : _preexisting = preexisting;

  bool _opened = false;
  String? _activityPhase;
  String? _spanId;
  String? _activityKey;

  // Which signal came last: text being written, or recorded activity.
  int _clock = 0;
  int _wroteAt = -1;
  int _activityAt = -1;

  String get _label => 'chat:${model.isEmpty ? 'sonder' : model}';

  /// The phase to show now.
  String get phase {
    if (_wroteAt > _activityAt) return 'writing';
    return _activityPhase ?? (_opened ? 'thinking' : 'routing');
  }

  /// The server committed to the turn (response headers arrived).
  bool opened() {
    if (_opened) return false;
    final before = phase;
    _opened = true;
    return phase != before;
  }

  /// Answer text arrived.
  bool wrote() {
    final before = phase;
    _wroteAt = ++_clock;
    return phase != before;
  }

  /// A status reading arrived; [activity] is its activity section, if any.
  bool observe(ActivityStatus? activity) {
    final span = _spanOf(activity);
    if (span == null) return false;
    final key = [
      span.id,
      span.events.length,
      span.events.isEmpty ? '' : span.events.last,
      span.modelCalls,
      span.toolCalls,
    ].join('|');
    if (key == _activityKey) return false;
    _activityKey = key;
    _activityAt = ++_clock;
    _activityPhase = activityPhase(span);
    return true;
  }

  /// This turn's span: the one claimed earlier, else the newest running
  /// `chat:<model>` span that was not running before the turn started.
  ActivityResponse? _spanOf(ActivityStatus? activity) {
    final active = activity?.active ?? const <ActivityResponse>[];
    final claimed = _spanId;
    if (claimed != null) {
      for (final span in active) {
        if (span.id == claimed) return span;
      }
      return null;
    }
    ActivityResponse? newest;
    for (final span in active) {
      if (span.id.isEmpty || _preexisting.contains(span.id)) continue;
      if (span.label != _label) continue;
      newest = span; // The server lists spans oldest first.
    }
    if (newest != null) _spanId = newest.id;
    return newest;
  }
}
