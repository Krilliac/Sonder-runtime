/// How agent lanes and background work speak the shared status vocabulary.
///
/// The server owns every state; this file only chooses the glyph, colour
/// role and word for each one ([StatusKind], `lib/ui/status_vocab.dart`).
/// Requested interruption and cancellation keep their own words, so a
/// request is never shown as an acknowledgement (UX-CONTRACT.md).
library;

import '../agent_lanes.dart';
import '../background_work.dart';
import '../ui/status_vocab.dart';

/// The list's status filters. They describe loaded data only.
enum AgentFilter {
  all('All'),
  working('Working'),
  attention('Needs you'),
  unread('Unread'),
  finished('Finished');

  final String label;
  const AgentFilter(this.label);
}

const _working = {
  'queued',
  'running',
  'interrupt_requested',
  'cancel_requested'
};
const _attention = {'awaiting_input', 'failed', 'interrupted'};
const _finished = {'completed', 'cancelled'};

extension AgentLaneStatus on AgentLane {
  /// The glyph and colour of this lane's server status.
  StatusKind get statusKind => switch (status) {
        'running' ||
        'interrupt_requested' ||
        'cancel_requested' =>
          StatusKind.running,
        'queued' => StatusKind.note,
        'awaiting_input' || 'interrupted' => StatusKind.warn,
        'completed' => StatusKind.ok,
        'failed' => StatusKind.fail,
        'cancelled' => StatusKind.skipped,
        _ => StatusKind.unknown,
      };

  bool get isWorking => _working.contains(status);
  bool get needsAttention => _attention.contains(status);
  bool get isFinished => _finished.contains(status);

  bool matches(AgentFilter filter) => switch (filter) {
        AgentFilter.all => true,
        AgentFilter.working => isWorking,
        AgentFilter.attention => needsAttention,
        AgentFilter.unread => unreadReports > 0,
        AgentFilter.finished => isFinished,
      };

  /// What the server's lane error code means, effect first. Unknown codes
  /// are shown as sent.
  String? get errorSummary => error.isEmpty ? null : laneErrorSummary(error);
}

/// The meaning of a lane error code, as the lane service assigns them.
String laneErrorSummary(String code) => switch (code) {
      'BUDGET_EXHAUSTED' => 'The agent used up its step, token or time budget.',
      'AUTHORITY_DENIED' => 'The agent’s access grant was refused or expired.',
      'CONTEXT_HISTORY_OVERFLOW' =>
        'The conversation no longer fits the model’s context.',
      'LANE_ATTEMPT_FAILED' => 'The attempt stopped with an error.',
      _ => code,
    };

/// A background status (fleet, fleet agent, autopilot run) in the shared
/// vocabulary. The server's own word is kept, in sentence case.
({StatusKind kind, String word}) backgroundStatus(String status) {
  final value = status.toLowerCase();
  final kind = switch (value) {
    'running' || 'active' || 'planning' || 'executing' => StatusKind.running,
    'queued' || 'pending' || 'ready' || 'planned' || 'todo' => StatusKind.note,
    'done' ||
    'completed' ||
    'complete' ||
    'passed' ||
    'success' ||
    'succeeded' =>
      StatusKind.ok,
    'failed' || 'error' => StatusKind.fail,
    'cancelled' || 'canceled' => StatusKind.skipped,
    'interrupted' || 'paused' || 'awaiting_input' => StatusKind.warn,
    _ => StatusKind.unknown,
  };
  final word = value.isEmpty || value == 'unknown'
      ? 'Unknown status'
      : '${value[0].toUpperCase()}${value.substring(1).replaceAll('_', ' ')}';
  return (kind: kind, word: word);
}

bool backgroundMatches(String status, AgentFilter filter) {
  final kind = backgroundStatus(status).kind;
  return switch (filter) {
    AgentFilter.all => true,
    AgentFilter.working =>
      kind == StatusKind.running || kind == StatusKind.note,
    AgentFilter.attention => kind == StatusKind.fail || kind == StatusKind.warn,
    AgentFilter.unread => false,
    AgentFilter.finished => kind == StatusKind.ok || kind == StatusKind.skipped,
  };
}

/// `12s`, `4m`, `3h 12m`, `2d`: a compact span for list rows.
String compactSpan(Duration span) {
  final seconds = span.inSeconds;
  if (seconds < 60) return '${seconds < 1 ? 1 : seconds}s';
  final minutes = span.inMinutes;
  if (minutes < 60) return '${minutes}m';
  final hours = span.inHours;
  if (hours < 24) {
    final rest = minutes % 60;
    return rest == 0 ? '${hours}h' : '${hours}h ${rest}m';
  }
  return '${span.inDays}d';
}

/// Elapsed time the server reported, or null when it did not.
String? elapsedText(double seconds) => seconds <= 0
    ? null
    : compactSpan(Duration(milliseconds: (seconds * 1000).round()));

/// How long ago a background item last changed, measured on the server's
/// own clock (`captured_at` minus `updated_ts`), or null when either
/// timestamp is missing. Never derived from the device clock.
String? updatedAgo(double updatedTs, double capturedAt) {
  if (updatedTs <= 0 || capturedAt <= 0 || capturedAt < updatedTs) return null;
  return '${compactSpan(Duration(milliseconds: ((capturedAt - updatedTs) * 1000).round()))} ago';
}

/// Fleet progress parts, from the server's counts.
({int done, int running, int queued, int failed, int cancelled, int total})
    fleetProgress(BackgroundFleet fleet) {
  final c = fleet.counts;
  final done = c['done'] ?? 0;
  final running = c['running'] ?? 0;
  final queued = c['queued'] ?? 0;
  final failed = c['failed'] ?? 0;
  final cancelled = c['cancelled'] ?? 0;
  final counted =
      done + running + queued + failed + cancelled + (c['other'] ?? 0);
  final total =
      fleet.requestedAgents > counted ? fleet.requestedAgents : counted;
  return (
    done: done,
    running: running,
    queued: queued,
    failed: failed,
    cancelled: cancelled,
    total: total,
  );
}
