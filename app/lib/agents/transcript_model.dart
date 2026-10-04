/// Turns a lane's durable events into transcript items: prose, tool calls
/// and lifecycle markers. Pure and synchronous, so the rules are testable
/// without widgets.
///
/// Rules:
/// - A model response that is a tool request (`{"tool": …, "arguments": …}`,
///   the lane protocol) is not prose. Its tool card stands for it.
/// - Tool events pair by `call_id`; a call without a result is "requested"
///   while the lane is active and "no result" once it is not. Nothing is
///   promoted to "done" without the server's result.
/// - Delivery state comes from the snapshot's messages when present.
library;

import 'dart:convert';

import '../agent_lanes.dart';

sealed class TranscriptItem {
  final int sequence;
  const TranscriptItem(this.sequence);

  /// Stable identity within one lane, for widget keys and expansion state.
  String get key;
}

class MessageItem extends TranscriptItem {
  /// `user`, `parent`, `child` (lane messages) or `model` (a response).
  final String author;
  final String content;
  final String deliveryState;
  const MessageItem(super.sequence,
      {required this.author, required this.content, this.deliveryState = ''});

  String get authorLabel => switch (author) {
        'user' => 'You',
        'parent' => 'Parent agent',
        'child' || 'model' => 'Agent',
        _ => author.isEmpty ? 'Agent' : author,
      };

  @override
  String get key => 'message-$sequence';
}

enum ToolState { requested, done, failed, received, noResult, rejected }

class ToolItem extends TranscriptItem {
  final String callKey;
  final String name;
  final Object? arguments;
  final ToolState state;
  final Object? output;
  final String errorCode;
  final Duration? duration;

  const ToolItem(
    super.sequence, {
    required this.callKey,
    required this.name,
    required this.state,
    this.arguments,
    this.output,
    this.errorCode = '',
    this.duration,
  });

  bool get hasResult =>
      state == ToolState.done ||
      state == ToolState.failed ||
      state == ToolState.received;

  /// The one argument that identifies the call at a glance: a path, a
  /// pattern, a command. Empty when nothing stands out.
  String get salientArgument {
    final args = arguments;
    if (args is! Map) return '';
    for (final name in const [
      'path',
      'file',
      'destination',
      'source',
      'pattern',
      'query',
      'command',
      'url',
      'suite',
      'root',
      'name',
    ]) {
      final value = args[name];
      if (value is String && value.trim().isNotEmpty) {
        return value.trim().split('\n').first;
      }
    }
    for (final value in args.values) {
      if (value is String && value.trim().isNotEmpty) {
        return value.trim().split('\n').first;
      }
    }
    return '';
  }

  @override
  String get key => 'tool-$callKey';
}

enum LifecycleKind {
  resumed,
  interrupted,
  cancelled,
  failed,
  stalled,
  completed,
  exited,
  reported
}

class LifecycleItem extends TranscriptItem {
  final LifecycleKind kind;

  /// The server's error code or text, when the event carries one.
  final String detail;
  const LifecycleItem(super.sequence, this.kind, {this.detail = ''});

  @override
  String get key => 'life-$sequence';
}

/// Whether a model response is a tool request in the lane protocol.
bool isToolRequestText(String content) {
  final trimmed = content.trim();
  if (!trimmed.startsWith('{')) return false;
  try {
    final value = jsonDecode(trimmed);
    return value is Map && value['tool'] is String;
  } on FormatException {
    return false;
  }
}

Duration? _durationOf(Map<String, dynamic> payload) {
  for (final key in const ['duration_ms', 'elapsed_ms']) {
    final value = payload[key];
    if (value is num && value >= 0) {
      return Duration(microseconds: (value * 1000).round());
    }
  }
  final seconds = payload['elapsed_seconds'] ?? payload['duration_seconds'];
  if (seconds is num && seconds >= 0) {
    return Duration(microseconds: (seconds * 1000000).round());
  }
  return null;
}

List<TranscriptItem> buildTranscript(
  Iterable<AgentEvent> events,
  Iterable<AgentMessage> messages, {
  required bool active,
}) {
  final sorted = events.toList()
    ..sort((a, b) => a.sequence.compareTo(b.sequence));
  final out = <TranscriptItem>[];

  final byMessage = <int, AgentMessage>{};
  for (final event in sorted) {
    if (event.type == 'lane.message') {
      byMessage[event.sequence] =
          AgentMessage.fromJson({...event.payload, 'sequence': event.sequence});
    }
  }
  for (final message in messages) {
    byMessage[message.sequence] = message;
  }
  for (final message in byMessage.values) {
    out.add(MessageItem(message.sequence,
        author: message.author,
        content: message.content,
        deliveryState: message.deliveryState));
  }

  final responses = <int, String>{};
  final tools = <String, List<AgentEvent>>{};
  final toolOrder = <String>[];
  var runningSeen = false;
  for (final event in sorted) {
    final p = event.payload;
    switch (event.type) {
      case 'model.response':
        final content = p['content']?.toString() ?? '';
        responses[event.sequence] = content;
        if (content.trim().isEmpty || isToolRequestText(content)) break;
        out.add(MessageItem(event.sequence, author: 'model', content: content));
      case 'tool.rejected':
        final source = (p['source_sequence'] as num?)?.toInt();
        Object? request;
        try {
          request = source == null || responses[source] == null
              ? null
              : jsonDecode(responses[source]!.trim());
        } on FormatException {
          request = null;
        }
        final name = request is Map && request['tool'] is String
            ? request['tool'] as String
            : 'Tool request';
        out.add(ToolItem(event.sequence,
            callKey: 'rejected-${event.sequence}',
            name: name,
            state: ToolState.rejected,
            arguments: request is Map ? request['arguments'] : null,
            errorCode: p['error_code']?.toString() ?? ''));
      case 'lane.running':
        if (runningSeen) {
          out.add(LifecycleItem(event.sequence, LifecycleKind.resumed));
        }
        runningSeen = true;
      case 'lane.stopped':
        out.add(LifecycleItem(
            event.sequence,
            p['status'] == 'cancelled'
                ? LifecycleKind.cancelled
                : LifecycleKind.interrupted));
      case 'lane.failed':
        out.add(LifecycleItem(
            event.sequence,
            p['status'] == 'awaiting_input'
                ? LifecycleKind.stalled
                : LifecycleKind.failed,
            detail: p['error']?.toString() ?? ''));
      case 'lane.completed':
        out.add(LifecycleItem(event.sequence, LifecycleKind.completed));
      case 'lane.owner.exited':
        out.add(LifecycleItem(event.sequence, LifecycleKind.exited,
            detail: p['status']?.toString() ?? ''));
      case 'lane.report':
        out.add(LifecycleItem(event.sequence, LifecycleKind.reported));
      default:
        if (event.type.startsWith('tool.')) {
          final id = p['call_id']?.toString() ?? '';
          final key = id.isEmpty
              ? (event.id.isEmpty ? '${event.sequence}' : event.id)
              : id;
          if (!tools.containsKey(key)) toolOrder.add(key);
          tools.putIfAbsent(key, () => []).add(event);
        }
    }
  }

  for (final key in toolOrder) {
    final group = tools[key]!;
    final first = group.first;
    AgentEvent? result, failed, completed;
    for (final event in group) {
      switch (event.type) {
        case 'tool.result':
          result = event;
        case 'tool.failed':
          failed = event;
        case 'tool.completed':
          completed = event;
      }
    }
    final ToolState state;
    if (result != null) {
      state = result.payload['success'] == true
          ? ToolState.done
          : result.payload['success'] == false
              ? ToolState.failed
              : ToolState.received;
    } else if (failed != null) {
      state = ToolState.failed;
    } else if (completed != null) {
      state = ToolState.done;
    } else {
      state = active ? ToolState.requested : ToolState.noResult;
    }
    final end = result ?? failed ?? completed;
    var duration = end == null ? null : _durationOf(end.payload);
    if (duration == null &&
        end != null &&
        first.occurredAt != null &&
        end.occurredAt != null &&
        !end.occurredAt!.isBefore(first.occurredAt!)) {
      duration = end.occurredAt!.difference(first.occurredAt!);
    }
    String name = '';
    for (final event in group) {
      final value = event.payload['name'] ?? event.payload['tool'];
      if (value is String && value.isNotEmpty) {
        name = value;
        break;
      }
    }
    final error = (end?.payload['error_code'] ?? '').toString();
    out.add(ToolItem(
      first.sequence,
      callKey: key,
      name: name.isEmpty ? 'Tool activity' : name,
      state: state,
      arguments: first.payload['arguments'],
      output: result?.payload['output'],
      errorCode: state == ToolState.failed && error != 'null' ? error : '',
      duration: duration,
    ));
  }

  // Stable by sequence: a lane message and its event share one sequence.
  final indexed = out.asMap().entries.toList()
    ..sort((a, b) {
      final bySequence = a.value.sequence.compareTo(b.value.sequence);
      return bySequence != 0 ? bySequence : a.key.compareTo(b.key);
    });
  return [for (final e in indexed) e.value];
}
