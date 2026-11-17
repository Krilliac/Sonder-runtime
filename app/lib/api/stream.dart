/// Server-sent-events client for `POST /v1/chat/completions` with
/// `stream: true` (plan P1-1).
///
/// Wire format (sonder_runtime/interfaces/http/serve.py `_chunk`,
/// `_begin_early_stream`, `_send_stream_terminal_error`):
///
/// ```text
/// : keep-alive                                  (comment; every ~15 s)
/// data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"…"}}]}
/// data: {… "choices":[{"delta":{},"finish_reason":"stop"}], "sonder_receipt":{…}, "sonder_activity":{…}}
/// data: {… "choices":[], "usage":{…}}
/// data: {"object":"error","error":{"message":"…","type":"…","code":"…"}}
/// data: [DONE]
/// ```
///
/// A plain model turn gets its headers early and keep-alives while the model
/// generates; routed work and slash commands send headers only when done.
/// So time-to-headers is bounded by [ChatStreamRequest.headerTimeout], and
/// after the headers any gap longer than [ChatStreamRequest.stallTimeout]
/// (keep-alives reset it) fails with [SonderException.stalledCode].
///
/// The stream is bounded against a broken or hostile peer: one line longer
/// than [ChatStreamRequest.maxLineChars], one frame longer than that, or an
/// answer longer than [ChatStreamRequest.maxAnswerChars] ends the turn with
/// [streamTooLargeCode]; and a turn that is still open after
/// [ChatStreamRequest.maxDuration] (for example a server that sends nothing
/// but keep-alives forever) ends with [SonderException.timeoutCode].
library;

import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

import 'chat.dart';
import 'transport.dart';

/// Default bound on one SSE line and on one event's data (1 Mi chars).
const defaultMaxStreamLineChars = 1 << 20;

/// Default bound on the streamed answer text (4 Mi chars).
const defaultMaxStreamAnswerChars = 4 << 20;

/// Error code for a stream line, frame or answer over its bound.
const streamTooLargeCode = 'STREAM_TOO_LARGE';

SonderException _tooLarge(String what) => SonderException(
      'The server sent $what larger than this app accepts, so the answer '
      'was abandoned.',
      code: streamTooLargeCode,
    );

/// Passes decoded text through unchanged, but fails once more than [max]
/// characters arrive without a line break, so the [LineSplitter] behind it
/// never buffers an unbounded line.
class _LineLengthGuard extends StreamTransformerBase<String, String> {
  final int max;
  const _LineLengthGuard(this.max);

  @override
  Stream<String> bind(Stream<String> stream) {
    var run = 0; // Characters since the last line break, across chunks.
    return stream.map((chunk) {
      for (var i = 0; i < chunk.length; i++) {
        final c = chunk.codeUnitAt(i);
        if (c == 0x0A || c == 0x0D) {
          run = 0;
        } else if (++run > max) {
          throw _tooLarge('a line');
        }
      }
      return chunk;
    });
  }
}

/// Bound on a non-streamed body read on the stream path (error envelope,
/// or a JSON completion from a server that ignored `stream`).
const _maxBodyBytes = 16 << 20;

Future<List<int>> _readAtMost(Stream<List<int>> stream, int max) async {
  final out = BytesBuilder(copy: false);
  await for (final chunk in stream) {
    if (out.length + chunk.length > max) throw _tooLarge('a response');
    out.add(chunk);
  }
  return out.takeBytes();
}

/// One event of a streamed chat turn.
sealed class ChatStreamEvent {
  const ChatStreamEvent();
}

/// The response headers arrived; the turn is committed on the server.
class ChatStreamOpened extends ChatStreamEvent {
  final Map<String, String> headers;
  const ChatStreamOpened(this.headers);
}

/// A keep-alive comment arrived. Carries no text; proves the link is alive.
class ChatStreamKeepAlive extends ChatStreamEvent {
  const ChatStreamKeepAlive();
}

/// The server says why the turn is waiting (`: sonder-status …` comment, for
/// example while GPU admission queues it). [text] is plain text, at most
/// [maxStatusChars] long; never render it as markup. An empty [text] is the
/// clearing frame: the wait is over, drop any status shown.
class ChatStreamStatus extends ChatStreamEvent {
  final String text;
  const ChatStreamStatus(this.text);
}

/// Longest status text kept from a `: sonder-status` comment.
const maxStatusChars = 200;

/// New answer text. [text] is the fragment; [accumulated] the answer so far.
class ChatStreamDelta extends ChatStreamEvent {
  final String text;
  final String accumulated;
  const ChatStreamDelta(this.text, this.accumulated);
}

/// The turn finished (`[DONE]`, or a non-streamed JSON answer).
class ChatStreamDone extends ChatStreamEvent {
  final ChatReply reply;
  const ChatStreamDone(this.reply);
}

String _boundedRawText(Object? value, int limit) {
  if (value == null) return '';
  final text = value is String ? value : value.toString();
  if (text.length <= limit) return text;
  return '${text.substring(0, limit)}...';
}

/// Bounded execution outcome carried on a completed `sonder_tool` SSE event.
class ToolRunResult {
  final bool ok;
  final int? returncode;
  final bool timedOut;
  final int? elapsedMs;
  final String stdoutTail;
  final String stderrTail;
  final String output;
  final bool truncated;

  const ToolRunResult({
    this.ok = false,
    this.returncode,
    this.timedOut = false,
    this.elapsedMs,
    this.stdoutTail = '',
    this.stderrTail = '',
    this.output = '',
    this.truncated = false,
  });

  factory ToolRunResult.fromJson(Map<String, dynamic> json) {
    final rawRc = json['returncode'];
    final rawElapsed = json['elapsed_ms'];
    return ToolRunResult(
      ok: json['ok'] == true,
      returncode: rawRc is num ? rawRc.toInt() : int.tryParse('$rawRc'),
      timedOut: json['timed_out'] == true,
      elapsedMs:
          rawElapsed is num ? rawElapsed.toInt() : int.tryParse('$rawElapsed'),
      stdoutTail: _boundedRawText(json['stdout_tail'], 12000),
      stderrTail: _boundedRawText(json['stderr_tail'], 12000),
      output: _boundedRawText(json['output'], 12000),
      truncated: json['truncated'] == true,
    );
  }
}

/// Live tool execution / approval event (`sonder_tool` on a chat chunk).
class ChatStreamToolEvent extends ChatStreamEvent {
  final String id;
  final String callId;
  final String digest;
  final String tool;
  final String language;
  final String code;
  final Map<String, String> args;
  final String preview;
  final int? timeout;
  final String backend;
  final bool bidiWarning;
  final List<String> bidiChars;
  final String state;
  final String? error;
  final ToolRunResult? result;

  const ChatStreamToolEvent({
    required this.id,
    this.callId = '',
    this.digest = '',
    required this.tool,
    this.language = '',
    this.code = '',
    this.args = const {},
    this.preview = '',
    this.timeout,
    this.backend = '',
    this.bidiWarning = false,
    this.bidiChars = const [],
    required this.state,
    this.error,
    this.result,
  });

  bool get isAwaitingApproval => state == 'awaiting_approval';

  factory ChatStreamToolEvent.fromJson(Map<String, dynamic> json) {
    final rawCallId = json['call_id']?.toString().trim().toLowerCase() ?? '';
    final callId =
        RegExp(r'^[0-9a-f]{8,64}$').hasMatch(rawCallId) ? rawCallId : '';
    final rawDigest = json['digest']?.toString().trim().toLowerCase() ?? '';
    final digest = RegExp(r'^[0-9a-f]{64}$').hasMatch(rawDigest) ? rawDigest : '';
    final rawArgs = json['args'];
    final args = <String, String>{};
    if (rawArgs is Map) {
      for (final entry in rawArgs.entries.take(16)) {
        final k = boundedResponseMetadata(entry.key, 64);
        if (k.isNotEmpty) {
          args[k] = boundedResponseMetadata(entry.value, 4096);
        }
      }
    }
    final rawBidi = json['bidi_chars'];
    final bidiChars = rawBidi is List
        ? rawBidi
            .map((e) => boundedResponseMetadata(e, 16))
            .where((e) => e.isNotEmpty)
            .take(16)
            .toList(growable: false)
        : const <String>[];
    final rawTimeout = json['timeout'];
    final timeout =
        rawTimeout is num ? rawTimeout.toInt() : int.tryParse('$rawTimeout');
    final rawError = json['error'];
    final errorText = rawError == null
        ? null
        : boundedResponseMetadata(rawError, 2000);
    final rawCode = json['code'];
    final code = rawCode is String
        ? (rawCode.length <= 16384 ? rawCode : rawCode.substring(0, 16384))
        : '';
    return ChatStreamToolEvent(
      id: boundedResponseMetadata(json['id'], 64),
      callId: callId,
      digest: digest,
      tool: boundedResponseMetadata(json['tool'], 128),
      language: boundedResponseMetadata(json['language'], 32),
      code: code,
      args: Map.unmodifiable(args),
      preview: boundedResponseMetadata(json['preview'], 4096),
      timeout: timeout != null && timeout > 0 ? timeout : null,
      backend: boundedResponseMetadata(json['backend'], 32),
      bidiWarning: json['bidi_warning'] == true || bidiChars.isNotEmpty,
      bidiChars: bidiChars,
      state: boundedResponseMetadata(json['state'], 32),
      error: errorText == null || errorText.isEmpty ? null : errorText,
      result: json['result'] is Map
          ? ToolRunResult.fromJson(
              Map<String, dynamic>.from(json['result'] as Map),
            )
          : null,
    );
  }
}

/// One parsed SSE frame: either a comment (keep-alive) or a data payload.
class SseFrame {
  final bool comment;
  final String data;

  /// Text of a `: sonder-status` comment ('' for the clearing frame); null for
  /// any other frame.
  final String? status;
  const SseFrame.comment()
      : comment = true,
        data = '',
        status = null;
  const SseFrame.statusComment(String this.status)
      : comment = true,
        data = '';
  const SseFrame.data(this.data)
      : comment = false,
        status = null;

  bool get isDone => !comment && data.trim() == '[DONE]';
}

/// Incremental SSE line parser. Feed it lines (without terminators); it
/// returns the frames each line completes. Pure, so it is unit-tested alone.
///
/// A frame whose data grows past [maxFrameChars] throws a [SonderException]
/// with [streamTooLargeCode] instead of buffering without limit.
class SseFrameParser {
  final int maxFrameChars;
  final List<String> _data = [];
  var _size = 0;

  SseFrameParser({this.maxFrameChars = defaultMaxStreamLineChars});

  List<SseFrame> addLine(String line) {
    if (line.isEmpty) {
      if (_data.isEmpty) return const [];
      final frame = SseFrame.data(_data.join('\n'));
      _data.clear();
      _size = 0;
      return [frame];
    }
    if (line.startsWith(':')) {
      var body = line.substring(1);
      if (body.startsWith(' ')) body = body.substring(1);
      const name = 'sonder-status';
      if (body.trimRight() == name || body.startsWith('$name ')) {
        var text = body.substring(name.length).trim();
        if (text.length > maxStatusChars) {
          text = text.substring(0, maxStatusChars).trimRight();
        }
        // An empty text is the server's clearing frame (the wait is over).
        return [SseFrame.statusComment(text)];
      }
      return const [SseFrame.comment()];
    }
    if (line.startsWith('data:')) {
      var value = line.substring(5);
      if (value.startsWith(' ')) value = value.substring(1);
      _size += value.length + 1;
      if (_size > maxFrameChars) {
        _data.clear();
        _size = 0;
        throw _tooLarge('an event');
      }
      _data.add(value);
    }
    // `event:`, `id:`, `retry:` and unknown fields are ignored.
    return const [];
  }

  /// Flush a trailing frame the server did not terminate with a blank line.
  List<SseFrame> close() => addLine('');
}

/// Folds chunk payloads into the answer text and the completion metadata.
class ChatStreamAccumulator {
  /// Longest answer accepted; more throws [streamTooLargeCode].
  final int maxAnswerChars;
  final StringBuffer _text = StringBuffer();

  ChatStreamAccumulator({this.maxAnswerChars = defaultMaxStreamAnswerChars});
  final Map<String, dynamic> completion = {};
  String finishReason = '';

  /// Tool event parsed from the most recent [apply] call, if any.
  ChatStreamToolEvent? lastToolEvent;

  String get text => _text.toString();

  /// Apply one decoded chunk. Returns the new content fragment (may be
  /// empty). Throws [SonderException] for an `object: error` frame.
  String apply(Map<String, dynamic> chunk) {
    lastToolEvent = null;
    if (chunk['object'] == 'error') {
      final error = chunk['error'] is Map
          ? Map<String, dynamic>.from(chunk['error'] as Map)
          : const <String, dynamic>{};
      final message = error['message'] is String &&
              (error['message'] as String).trim().isNotEmpty
          ? boundedResponseMetadata(error['message'], 1024)
          : 'The stream stopped before the answer finished.';
      throw SonderException(
        message,
        type: boundedResponseMetadata(error['type'], 64),
        code: boundedResponseMetadata(error['code'], 128),
        retryable: error['retryable'] == true,
      );
    }
    for (final key in const ['id', 'model']) {
      if (chunk[key] != null) completion[key] ??= chunk[key];
    }
    for (final key in const [
      'sonder_receipt',
      'sonder_activity',
      'sonder_elapsed_ms',
      'sonder_reasoning',
      'usage',
    ]) {
      if (chunk[key] != null) completion[key] = chunk[key];
    }
    if (chunk['sonder_tool'] is Map) {
      final parsed = ChatStreamToolEvent.fromJson(
        Map<String, dynamic>.from(chunk['sonder_tool'] as Map),
      );
      if (parsed.tool.isNotEmpty || parsed.id.isNotEmpty) {
        lastToolEvent = parsed;
      }
    }
    var fragment = '';
    final choices = chunk['choices'];
    if (choices is List && choices.isNotEmpty && choices.first is Map) {
      final choice = Map<String, dynamic>.from(choices.first as Map);
      final delta = choice['delta'];
      if (delta is Map && delta['content'] is String) {
        fragment = delta['content'] as String;
        if (_text.length + fragment.length > maxAnswerChars) {
          throw _tooLarge('an answer');
        }
        _text.write(fragment);
      }
      final finish = choice['finish_reason'];
      if (finish is String && finish.isNotEmpty) finishReason = finish;
    }
    return fragment;
  }

  ChatReply reply({Map<String, String> headers = const {}}) => chatReplyFrom(
        content: text,
        completion: completion,
        headers: headers,
        finishReason: finishReason,
      );
}

/// Everything needed to open one streamed turn.
class ChatStreamRequest {
  final Uri uri;
  final Map<String, String> headers;
  final String body;
  final CancelToken? cancel;
  final Duration headerTimeout;
  final Duration stallTimeout;

  /// Hard cap on the whole turn, headers included. Keep-alives do not reset
  /// it, so a peer that only ever sends keep-alives cannot hold the turn
  /// open forever.
  final Duration maxDuration;

  /// Longest single line, and longest single event, accepted.
  final int maxLineChars;

  /// Longest answer text accepted.
  final int maxAnswerChars;

  const ChatStreamRequest({
    required this.uri,
    required this.headers,
    required this.body,
    this.cancel,
    this.headerTimeout = const Duration(minutes: 5),
    this.stallTimeout = const Duration(seconds: 45),
    this.maxDuration = const Duration(minutes: 30),
    this.maxLineChars = defaultMaxStreamLineChars,
    this.maxAnswerChars = defaultMaxStreamAnswerChars,
  });
}

/// Open a streamed chat turn and emit its events.
///
/// Errors are [SonderException]s: a non-2xx before the stream (the JSON
/// envelope path), an `object: error` frame, a stall, a connection that
/// closed before `[DONE]` (`STREAM_INTERRUPTED`), or a cancel (`CANCELLED`).
/// The client is closed when the stream ends for any reason.
///
/// [onConnectError] is called with a failure that happened before the
/// response headers; it may return a replacement request (the local
/// fallback) or null to report the error.
Stream<ChatStreamEvent> openChatStream(
  ChatStreamRequest request, {
  ChatStreamRequest? Function(Object error)? onConnectError,
}) {
  late StreamController<ChatStreamEvent> controller;
  NoRedirectClient? client;
  StreamSubscription<String>? lines;
  Timer? timer;
  Timer? deadline;
  void Function()? unregister;
  var finished = false;

  void finish([Object? error]) {
    if (finished) return;
    finished = true;
    timer?.cancel();
    deadline?.cancel();
    unregister?.call();
    lines?.cancel();
    client?.close();
    if (error != null) controller.addError(error);
    controller.close();
  }

  void arm(Duration duration, SonderException Function() onFire) {
    timer?.cancel();
    timer = Timer(duration, () => finish(onFire()));
  }

  Future<void> start(ChatStreamRequest current,
      {bool allowFallback = true}) async {
    client?.close();
    client = NoRedirectClient(http.Client());
    unregister?.call();
    unregister =
        current.cancel?.onCancel(() => finish(SonderException.cancelled()));
    if (finished) return;
    arm(
      current.headerTimeout,
      () => SonderException.transport(
        TimeoutException('no response headers', current.headerTimeout),
        current.uri.origin,
      ),
    );
    final http.StreamedResponse response;
    try {
      final req = http.Request('POST', current.uri)
        ..headers.addAll(current.headers)
        ..headers['Accept'] = 'text/event-stream'
        ..body = current.body;
      response = await client!.send(req);
    } catch (error) {
      if (finished) return;
      final replacement = allowFallback ? onConnectError?.call(error) : null;
      if (replacement != null) {
        await start(replacement, allowFallback: false);
        return;
      }
      finish(SonderException.transport(error, current.uri.origin));
      return;
    }
    if (finished) return;
    final headers = response.headers;
    if (response.statusCode < 200 || response.statusCode >= 300) {
      try {
        final bytes = await _readAtMost(response.stream, _maxBodyBytes);
        finish(responseException(
          response,
          httpStatusFallback(response.statusCode),
          bodyBytes: bytes,
        ));
      } catch (error) {
        finish(SonderException.transport(error, current.uri.origin));
      }
      return;
    }
    controller.add(ChatStreamOpened(headers));
    final contentType = headers['content-type'] ?? '';
    if (!contentType.contains('text/event-stream')) {
      // The server answered without streaming (older build, or a route that
      // ignores `stream`): the body is one completion object.
      try {
        final bytes = await _readAtMost(response.stream, _maxBodyBytes);
        if (finished) return;
        final obj = jsonDecode(utf8.decode(bytes));
        if (obj is! Map) throw const FormatException('not an object');
        final completion = Map<String, dynamic>.from(obj);
        final choices = completion['choices'];
        if (choices is! List || choices.isEmpty || choices.first is! Map) {
          throw SonderException('Empty response from server.');
        }
        final choice = Map<String, dynamic>.from(choices.first as Map);
        final message = choice['message'];
        final content =
            message is Map ? message['content']?.toString() ?? '' : '';
        if (content.isNotEmpty) {
          controller.add(ChatStreamDelta(content, content));
        }
        controller.add(ChatStreamDone(chatReplyFrom(
          content: content,
          completion: completion,
          headers: headers,
          finishReason: choice['finish_reason']?.toString() ?? '',
        )));
        finish();
      } on SonderException catch (error) {
        finish(error);
      } catch (_) {
        finish(SonderException('Could not parse server response.'));
      }
      return;
    }

    final parser = SseFrameParser(maxFrameChars: current.maxLineChars);
    final acc = ChatStreamAccumulator(maxAnswerChars: current.maxAnswerChars);
    SonderException stalled() => SonderException(
          'The server stopped sending for ${current.stallTimeout.inSeconds} s, '
          'so the answer was abandoned.',
          code: SonderException.stalledCode,
          retryable: true,
        );
    arm(current.stallTimeout, stalled);

    void handle(SseFrame frame) {
      if (finished) return;
      if (frame.comment) {
        final status = frame.status;
        controller.add(status == null
            ? const ChatStreamKeepAlive()
            : ChatStreamStatus(status));
        return;
      }
      if (frame.isDone) {
        controller.add(ChatStreamDone(acc.reply(headers: headers)));
        finish();
        return;
      }
      final Object? decoded;
      try {
        decoded = jsonDecode(frame.data);
      } catch (_) {
        return; // A malformed frame is skipped, not fatal.
      }
      if (decoded is! Map) return;
      try {
        final fragment = acc.apply(Map<String, dynamic>.from(decoded));
        final toolEvent = acc.lastToolEvent;
        if (toolEvent != null) {
          controller.add(toolEvent);
        }
        if (fragment.isNotEmpty) {
          controller.add(ChatStreamDelta(fragment, acc.text));
        }
      } on SonderException catch (error) {
        finish(error);
      }
    }

    lines = response.stream
        .transform(utf8.decoder)
        .transform(_LineLengthGuard(current.maxLineChars))
        .transform(const LineSplitter())
        .listen(
      (line) {
        if (finished) return;
        arm(current.stallTimeout, stalled);
        final List<SseFrame> frames;
        try {
          frames = parser.addLine(line);
        } on SonderException catch (error) {
          finish(error);
          return;
        }
        for (final frame in frames) {
          handle(frame);
        }
      },
      onError: (Object error) {
        if (error is SonderException) {
          finish(error); // A bound was exceeded.
          return;
        }
        finish(SonderException(
          'The connection dropped before the answer finished.',
          cause: error,
          code: 'STREAM_INTERRUPTED',
          retryable: true,
        ));
      },
      onDone: () {
        for (final frame in parser.close()) {
          handle(frame);
        }
        finish(SonderException(
          'The connection closed before the answer finished.',
          code: 'STREAM_INTERRUPTED',
          retryable: true,
        ));
      },
      cancelOnError: true,
    );
  }

  controller = StreamController<ChatStreamEvent>(
    onListen: () {
      if (request.cancel?.isCancelled == true) {
        finish(SonderException.cancelled());
        return;
      }
      deadline = Timer(
        request.maxDuration,
        () => finish(SonderException(
          'The answer took longer than ${request.maxDuration.inMinutes} min, '
          'so it was abandoned. Long jobs belong in a work run.',
          code: SonderException.timeoutCode,
          retryable: true,
        )),
      );
      unawaited(start(request));
    },
    onCancel: () => finish(),
  );
  return controller.stream;
}
