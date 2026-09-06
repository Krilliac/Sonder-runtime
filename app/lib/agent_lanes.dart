/// Durable server conversations. The server owns lifecycle and delivery state.
class AgentLane {
  final String id, sessionId, parentSessionId, title, task, status, attemptId;
  final String? parentLaneId;
  final String workspaceRoot, tier, error;
  final int revision, unreadReports;
  AgentLane.fromJson(Map<String, dynamic> j)
      : id = j['id']?.toString() ?? '',
        sessionId = j['session_id']?.toString() ?? '',
        parentSessionId = j['parent_session_id']?.toString() ?? '',
        parentLaneId = j['parent_lane_id']?.toString(),
        title = j['title']?.toString() ?? '',
        task = j['task']?.toString() ?? '',
        status = j['status']?.toString() ?? 'unknown',
        attemptId = j['attempt_id']?.toString() ?? '',
        workspaceRoot = j['workspace_root']?.toString() ?? '',
        tier = j['tier']?.toString() ?? '',
        error = j['error']?.toString() ?? '',
        revision = (j['revision'] as num?)?.toInt() ?? 0,
        unreadReports = (j['unread_reports'] as num?)?.toInt() ?? 0;
  String get displayTitle =>
      title.isEmpty ? (task.isEmpty ? 'Agent conversation' : task) : title;
  String get statusLabel => switch (status) {
        'interrupt_requested' => 'Interrupt requested',
        'cancel_requested' => 'Cancel requested',
        'awaiting_input' => 'Needs input',
        'queued' => 'Queued',
        'running' => 'Running',
        'completed' => 'Completed',
        'failed' => 'Failed',
        'interrupted' => 'Interrupted',
        'cancelled' => 'Cancelled',
        _ => 'Unknown status',
      };

  /// A compact server-owned identity for status lines and assistive labels.
  ///
  /// The lane endpoint deliberately withholds grant and capacity counters.
  /// Keep this summary to fields that are already public rather than deriving
  /// progress or inventing resource values in the client.
  String get executionSummary {
    final model = tier.isEmpty ? 'tier unavailable' : 'tier $tier';
    return '$statusLabel · $model · revision $revision';
  }

  bool get canResume => const {
        'interrupted',
        'failed',
        'completed',
        'awaiting_input',
      }.contains(status);
  bool get canInterrupt => const {'queued', 'running'}.contains(status);
  bool get canCancel => const {
        'queued',
        'running',
        'interrupt_requested',
        'interrupted',
        'awaiting_input',
      }.contains(status);
}

class AgentLanePage {
  final List<AgentLane> lanes;
  final int nextCursor;
  final bool hasMore;
  AgentLanePage.fromJson(Map<String, dynamic> j)
      : lanes = _maps(j['lanes']).map(AgentLane.fromJson).toList(),
        nextCursor = (j['next_cursor'] as num?)?.toInt() ?? 0,
        hasMore = j['has_more'] == true;
}

class AgentMessage {
  final String id, author, content, deliveryState;
  final int sequence;
  AgentMessage.fromJson(Map<String, dynamic> j)
      : id = j['id']?.toString() ?? '',
        author = j['author']?.toString() ?? '',
        content = j['content']?.toString() ?? '',
        deliveryState = j['delivery_state']?.toString() ?? '',
        sequence = (j['sequence'] as num?)?.toInt() ?? 0;
  String get authorLabel => switch (author) {
        'user' => 'You',
        'parent' => 'Parent agent',
        'child' => 'Agent',
        _ => author,
      };
}

class AgentEvent {
  final int sequence;
  final String id, type;
  final Map<String, dynamic> payload;
  AgentEvent.fromJson(Map<String, dynamic> j)
      : sequence = (j['sequence'] as num?)?.toInt() ?? 0,
        id = j['event_id']?.toString() ?? '',
        type = j['event_type']?.toString() ?? '',
        payload = j['payload'] is Map
            ? Map<String, dynamic>.from(j['payload'] as Map)
            : {};
}

class AgentSnapshot {
  final AgentLane lane;
  final List<AgentMessage> messages;
  final List<AgentEvent> events;
  final int nextCursor;
  final bool hasMore;
  AgentSnapshot.fromJson(Map<String, dynamic> j)
      : lane = AgentLane.fromJson(Map<String, dynamic>.from(j['lane'] as Map)),
        messages = _maps(j['messages']).map(AgentMessage.fromJson).toList(),
        events = _maps(j['events']).map(AgentEvent.fromJson).toList(),
        nextCursor = (j['next_cursor'] as num?)?.toInt() ?? 0,
        hasMore = j['has_more'] == true;
}

class AgentReceipt {
  final String commandId;
  final int revision;
  final AgentLane? lane;
  AgentReceipt.fromJson(Map<String, dynamic> j)
      : commandId = j['command_id']?.toString() ?? '',
        revision = (j['revision'] as num?)?.toInt() ?? 0,
        lane = j['lane'] is Map
            ? AgentLane.fromJson(Map<String, dynamic>.from(j['lane'] as Map))
            : null;
}

class AgentReport {
  final String id, laneId, summary;
  final List<String> artifacts;
  final bool acknowledged;
  AgentReport.fromJson(Map<String, dynamic> j)
      : id = j['id']?.toString() ?? '',
        laneId = j['lane_id']?.toString() ?? '',
        summary = j['summary']?.toString() ?? '',
        artifacts =
            (j['artifacts'] as List? ?? []).map((e) => e.toString()).toList(),
        acknowledged = j['acknowledged'] == true;
}

class AgentReportPage {
  final List<AgentReport> reports;
  final int nextCursor;
  final bool hasMore;
  AgentReportPage.fromJson(Map<String, dynamic> j)
      : reports = _maps(j['reports']).map(AgentReport.fromJson).toList(),
        nextCursor = (j['next_cursor'] as num?)?.toInt() ?? 0,
        hasMore = j['has_more'] == true;
}

Iterable<Map<String, dynamic>> _maps(Object? value) =>
    (value is List ? value : const []).whereType<Map>().map(
          (e) => Map<String, dynamic>.from(e),
        );
