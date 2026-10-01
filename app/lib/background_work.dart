import 'agent_lanes.dart';

class BackgroundChild {
  final String id, task, status, activity, preview;
  final double elapsedSeconds;
  final bool cancelable;
  const BackgroundChild({
    required this.id,
    required this.task,
    required this.status,
    this.activity = '',
    this.preview = '',
    this.elapsedSeconds = 0,
    this.cancelable = false,
  });
  factory BackgroundChild.fromJson(Map<String, dynamic> j) => BackgroundChild(
        id: j['id']?.toString() ?? '',
        task: j['task']?.toString() ?? '',
        status: j['status']?.toString() ?? 'unknown',
        activity: j['activity']?.toString() ?? '',
        preview: j['preview']?.toString() ?? '',
        elapsedSeconds: _double(j['elapsed_seconds']),
        cancelable: j['cancelable'] == true,
      );
  String get elapsedLabel => _durationLabel(elapsedSeconds);
}

class BackgroundFleet {
  final String id, task, status, preview;
  final int requestedAgents, workerSlots;
  final double elapsedSeconds;
  final Map<String, int> counts;
  final List<BackgroundChild> children;
  final bool cancelable;
  const BackgroundFleet({
    required this.id,
    required this.task,
    required this.status,
    required this.requestedAgents,
    required this.workerSlots,
    required this.counts,
    required this.children,
    this.elapsedSeconds = 0,
    this.preview = '',
    this.cancelable = false,
  });
  factory BackgroundFleet.fromJson(Map<String, dynamic> j) => BackgroundFleet(
        id: j['id']?.toString() ?? '',
        task: j['task']?.toString() ?? '',
        status: j['status']?.toString() ?? 'unknown',
        requestedAgents: _int(j['requested_agents']),
        workerSlots: _int(j['worker_slots']),
        preview: j['preview']?.toString() ?? '',
        cancelable: j['cancelable'] == true,
        counts: _counts(j['counts']),
        children: _maps(j['children'])
            .map(BackgroundChild.fromJson)
            .toList(growable: false),
        elapsedSeconds: _double(j['elapsed_seconds']),
      );
  String get elapsedLabel => _durationLabel(elapsedSeconds);
  String get countSummary =>
      '${counts['done'] ?? 0} done · ${counts['running'] ?? 0} running · ${counts['queued'] ?? 0} queued';
}

class BackgroundAutopilot {
  final String id, objective, status, phase, currentTask, preview;
  final Map<String, int> taskCounts;
  final double elapsedSeconds;
  final bool cancelable;
  const BackgroundAutopilot({
    required this.id,
    required this.objective,
    required this.status,
    required this.phase,
    required this.currentTask,
    required this.taskCounts,
    this.elapsedSeconds = 0,
    this.preview = '',
    this.cancelable = false,
  });
  factory BackgroundAutopilot.fromJson(Map<String, dynamic> j) =>
      BackgroundAutopilot(
        id: j['id']?.toString() ?? '',
        objective: j['objective']?.toString() ?? '',
        status: j['status']?.toString() ?? 'unknown',
        phase: j['phase']?.toString() ?? '',
        currentTask: j['current_task']?.toString() ?? '',
        preview: j['preview']?.toString() ?? '',
        taskCounts: _counts(j['task_counts']),
        elapsedSeconds: _double(j['elapsed_seconds']),
        cancelable: j['cancelable'] == true,
      );
  String get elapsedLabel => _durationLabel(elapsedSeconds);
  String get taskCountSummary =>
      '${taskCounts['done'] ?? 0}/${taskCounts['total'] ?? 0} tasks';
}

class BackgroundWork {
  final List<AgentLane> lanes;
  final List<BackgroundFleet> fleets;
  final List<BackgroundAutopilot> autopilot;
  final bool truncated;
  const BackgroundWork({
    this.lanes = const [],
    this.fleets = const [],
    this.autopilot = const [],
    this.truncated = false,
  });
  factory BackgroundWork.fromJson(Map<String, dynamic> j) {
    final groups = j['groups'] is Map
        ? Map<String, dynamic>.from(j['groups'] as Map)
        : const <String, dynamic>{};
    return BackgroundWork(
      truncated: j['truncated'] is Map && (j['truncated'] as Map).values.any((value) => value == true),
      lanes: _maps(groups['lanes'])
          .map(AgentLane.fromJson)
          .toList(growable: false),
      fleets: _maps(groups['fleets'])
          .map(BackgroundFleet.fromJson)
          .toList(growable: false),
      autopilot: _maps(groups['autopilot'])
          .map(BackgroundAutopilot.fromJson)
          .toList(growable: false),
    );
  }
}

int _int(Object? value) =>
    value is num ? value.toInt() : int.tryParse(value?.toString() ?? '') ?? 0;
double _double(Object? value) => value is num
    ? value.toDouble()
    : double.tryParse(value?.toString() ?? '') ?? 0;
String _durationLabel(double seconds) {
  if (seconds <= 0) return 'elapsed unknown';
  final whole = seconds.round();
  if (whole < 60) return '${whole}s';
  final minutes = whole ~/ 60;
  if (minutes < 60) return '${minutes}m ${whole % 60}s';
  return '${minutes ~/ 60}h ${minutes % 60}m';
}

Map<String, int> _counts(Object? value) => value is Map
    ? {for (final e in value.entries) e.key.toString(): _int(e.value)}
    : const {};
Iterable<Map<String, dynamic>> _maps(Object? value) =>
    (value is List ? value : const []).whereType<Map>().map(
          (e) => Map<String, dynamic>.from(e),
        );
