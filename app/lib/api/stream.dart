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
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'chat.dart';
import 'transport.dart';

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

/// One parsed SSE frame: either a comment (keep-alive) or a data payload.
class SseFrame {
  final bool comment;
  final String data;
  const SseFrame.comment()
      : comment = true,
        data = '';
  const SseFrame.data(this.data) : comment = false;

  bool get isDone => !comment && data.trim() == '[DONE]';
}

/// Incremental SSE line parser. Feed it lines (without terminators); it
/// returns the frames each line completes. Pure, so it is unit-tested alone.
class SseFrameParser {
  final List<String> _data = [];

  List<SseFrame> addLine(String line) {
    if (line.isEmpty) {
      if (_data.isEmpty) return const [];
      final frame = SseFrame.data(_data.join('\n'));
      _data.clear();
      return [frame];
    }
    if (line.startsWith(':')) return const [SseFrame.comment()];
    if (line.startsWith('data:')) {
      var value = line.substring(5);
      if (value.startsWith(' ')) value = value.substring(1);
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
  final StringBuffer _text = StringBuffer();
  final Map<String, dynamic> completion = {};
  String finishReason = '';

  String get text => _text.toString();

  /// Apply one decoded chunk. Returns the new content fragment (may be
  /// empty). Throws [SonderException] for an `object: error` frame.
  String apply(Map<String, dynamic> chunk) {
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
    var fragment = '';
    final choices = chunk['choices'];
    if (choices is List && choices.isNotEmpty && choices.first is Map) {
      final choice = Map<String, dynamic>.from(choices.first as Map);
      final delta = choice['delta'];
      if (delta is Map && delta['content'] is String) {
        fragment = delta['content'] as String;
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

  const ChatStreamRequest({
    required this.uri,
    required this.headers,
    required this.body,
    this.cancel,
    this.headerTimeout = const Duration(minutes: 5),
    this.stallTimeout = const Duration(seconds: 45),
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
  void Function()? unregister;
  var finished = false;

  void finish([Object? error]) {
    if (finished) return;
    finished = true;
    timer?.cancel();
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
        final bytes = await response.stream.toBytes();
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
        final bytes = await response.stream.toBytes();
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

    final parser = SseFrameParser();
    final acc = ChatStreamAccumulator();
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
        controller.add(const ChatStreamKeepAlive());
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
        if (fragment.isNotEmpty) {
          controller.add(ChatStreamDelta(fragment, acc.text));
        }
      } on SonderException catch (error) {
        finish(error);
      }
    }

    lines = response.stream
        .transform(utf8.decoder)
        .transform(const LineSplitter())
        .listen(
      (line) {
        if (finished) return;
        arm(current.stallTimeout, stalled);
        for (final frame in parser.addLine(line)) {
          handle(frame);
        }
      },
      onError: (Object error) {
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
      unawaited(start(request));
    },
    onCancel: () => finish(),
  );
  return controller.stream;
}
