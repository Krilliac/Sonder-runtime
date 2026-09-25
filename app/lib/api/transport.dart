/// HTTP transport shared by every Sonder API call.
///
/// Owns four things the rest of the API layer must agree on:
///
/// * [SonderException], the one error type the UI renders, with the server's
///   machine-readable `code`, the HTTP `status`, a parsed `Retry-After` and a
///   short `remedy`;
/// * [responseException], which turns any non-2xx response into that error
///   without ever showing a raw body or a Dart map;
/// * [describeServerError], the person-facing sentence for the failures a
///   phone user actually hits (421 host allowlist, 429 sign-in limit,
///   403 role gates);
/// * the fallback policy ([isPreRequestConnectFailure]): a turn may be
///   re-sent to the local server only when the primary refused the
///   connection or its name did not resolve, i.e. before any request byte
///   left the device. A timeout, TLS failure or partial response never
///   re-sends, because the server may already be running tools.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:http/http.dart' as http;

import '../account_session.dart';

/// Error text bounded before it reaches the transcript.
String boundedResponseMetadata(Object? value, [int limit = 256]) {
  final text = value?.toString().trim() ?? '';
  if (text.length <= limit) return text;
  return '${text.substring(0, limit)}...';
}

/// The one error type the API layer throws.
///
/// [message] is always readable prose: never a raw response body, a Dart
/// `Map`'s `toString()`, or `{code: …}`. Structured fields are bounded
/// metadata so a details view can show them without leaking the body.
class SonderException implements Exception {
  final String message;

  /// The underlying error, kept so a details view can show it without the
  /// chat bubble having to lead with it.
  final Object? cause;

  /// HTTP status of the failed response, or null for transport failures.
  final int? httpStatus;
  final String type;

  /// The server's machine-readable code (`HOST_NOT_ALLOWED`,
  /// `AUTH_RATE_LIMITED`, `FORBIDDEN`, …), or a client code such as
  /// `CANCELLED`, `TIMEOUT`, `UNREACHABLE`, `STREAM_STALLED`.
  final String code;
  final String correlationId;
  final bool retryable;

  /// Parsed from the `Retry-After` header (delta-seconds form).
  final int? retryAfterSeconds;

  /// One-line action a person can take, or empty when there is none.
  final String remedy;

  SonderException(
    this.message, {
    this.cause,
    this.httpStatus,
    this.type = '',
    this.code = '',
    this.correlationId = '',
    this.retryable = false,
    this.retryAfterSeconds,
    this.remedy = '',
  });

  /// Plan name for [httpStatus].
  int? get status => httpStatus;

  /// Plan name for [retryAfterSeconds], as a [Duration].
  Duration? get retryAfter =>
      retryAfterSeconds == null ? null : Duration(seconds: retryAfterSeconds!);

  /// True when the user stopped the request (a [CancelToken] fired).
  bool get isCancelled => code == cancelledCode;

  /// True for the 421 host allowlist refusal.
  bool get isHostNotAllowed =>
      code == 'HOST_NOT_ALLOWED' || (httpStatus == 421 && code.isEmpty);

  /// True for the register 403 whose remedy is a bootstrap secret.
  bool get needsBootstrapSecret => code == bootstrapRequiredCode;

  static const cancelledCode = 'CANCELLED';
  static const timeoutCode = 'TIMEOUT';
  static const unreachableCode = 'UNREACHABLE';
  static const stalledCode = 'STREAM_STALLED';
  static const bootstrapRequiredCode = 'BOOTSTRAP_REQUIRED';

  /// A copy with a different [message] and/or [remedy].
  SonderException copyWith({String? message, String? remedy}) =>
      SonderException(
        message ?? this.message,
        cause: cause,
        httpStatus: httpStatus,
        type: type,
        code: code,
        correlationId: correlationId,
        retryable: retryable,
        retryAfterSeconds: retryAfterSeconds,
        remedy: remedy ?? this.remedy,
      );

  String get diagnosticText {
    final lines = <String>[];
    if (httpStatus != null) lines.add('HTTP $httpStatus');
    if (type.isNotEmpty) lines.add('type: $type');
    if (code.isNotEmpty) lines.add('code: $code');
    if (correlationId.isNotEmpty) lines.add('request: $correlationId');
    if (retryAfterSeconds != null) {
      lines.add('retry after: ${retryAfterSeconds}s');
    } else if (retryable) {
      lines.add('retryable: yes');
    }
    return lines.join('\n');
  }

  /// Turn a transport failure into something a person can act on.
  ///
  /// These used to reach the chat bubble verbatim, so the first thing a new
  /// user saw was "ClientException with SocketException: The remote computer
  /// refused the network connection (OS Error: ..., errno = 1225), address =
  /// 127.0.0.1, port = 56249". Every part of that is either noise (an
  /// ephemeral local port number) or jargon (errno 1225), and none of it
  /// says the one thing that matters: the server is not running.
  factory SonderException.transport(Object error, String baseUrl) {
    if (error is SonderException) return error;
    final text = error.toString();
    final refused = _refusedPattern.hasMatch(text) ||
        text.contains('Connection closed before full header');
    final timedOut = error is TimeoutException ||
        text.contains('TimeoutException') ||
        text.contains('timed out');

    if (timedOut) {
      return SonderException(
        'The Sonder server at $baseUrl did not respond in time.\n\n'
        'A model loading for the first time can take a while — the Runtime '
        'page shows whether the server is up.',
        cause: error,
        code: timeoutCode,
        retryable: true,
      );
    }
    if (refused) {
      return SonderException(
        'Cannot reach the Sonder server at $baseUrl.\n\n'
        "It does not look like it is running. Open the Runtime page and use "
        'Start server, or check the server URL in Settings.',
        cause: error,
        code: unreachableCode,
        retryable: true,
        remedy: 'Start the server on the PC, or check the server URL.',
      );
    }
    return SonderException(
      'Could not reach $baseUrl.',
      cause: error,
      code: unreachableCode,
      retryable: true,
    );
  }

  /// The error a cancelled request completes with.
  factory SonderException.cancelled() =>
      SonderException('Request cancelled.', code: cancelledCode);

  @override
  String toString() => message;
}

/// Build a [SonderException] from any non-2xx response.
///
/// Reads the SPEC-2 envelope `{error:{code,message,type,retryable,
/// correlation_id}}`, the account shape `{ok:false,message}`, and a bare
/// string `error`. A body that is not JSON keeps [fallback]. The message is
/// always a string pulled from a known field, never a map's `toString()`.
SonderException responseException(http.BaseResponse response, String fallback,
    {List<int>? bodyBytes}) {
  var message = fallback;
  var type = '';
  var code = '';
  var correlationId = boundedResponseMetadata(
    response.headers['x-sonder-correlation-id'],
  );
  var retryable = const {408, 429, 502, 503, 504}.contains(response.statusCode);
  final bytes =
      bodyBytes ?? (response is http.Response ? response.bodyBytes : null);
  try {
    final decoded = bytes == null ? null : jsonDecode(utf8.decode(bytes));
    if (decoded is Map) {
      final error = decoded['error'];
      final candidate = error is Map
          ? error['message']
          : (error is String ? error : decoded['message']);
      final detail = candidate is String || candidate is num
          ? candidate.toString().trim()
          : '';
      if (detail.isNotEmpty) {
        // Error responses are untrusted server input; keep a malformed proxy
        // response from turning into an unbounded chat transcript entry.
        message =
            detail.length <= 1024 ? detail : '${detail.substring(0, 1024)}...';
      }
      if (error is Map) {
        type = boundedResponseMetadata(error['type'], 64);
        code = boundedResponseMetadata(error['code'], 128);
        correlationId =
            error['correlation_id']?.toString().trim().isNotEmpty == true
                ? boundedResponseMetadata(error['correlation_id'])
                : correlationId;
        retryable =
            error['retryable'] is bool ? error['retryable'] == true : retryable;
      } else {
        if (error is String && error.trim().isNotEmpty && code.isEmpty) {
          // agent-lanes uses {"error": "FORBIDDEN", "message": "..."}.
          final bare = error.trim();
          if (RegExp(r'^[A-Z][A-Z0-9_]{2,63}$').hasMatch(bare)) {
            code = bare;
            final other = decoded['message'];
            message = other is String && other.trim().isNotEmpty
                ? boundedResponseMetadata(other, 1024)
                : fallback;
          }
        }
        correlationId =
            decoded['correlation_id']?.toString().trim().isNotEmpty == true
                ? boundedResponseMetadata(decoded['correlation_id'])
                : correlationId;
        retryable = decoded['retryable'] is bool
            ? decoded['retryable'] == true
            : retryable;
      }
    }
  } catch (_) {
    // A non-JSON response still gets the stable status-code fallback.
  }
  if (code.isEmpty && response.statusCode == 421) code = 'HOST_NOT_ALLOWED';
  if (response.statusCode == 403 &&
      message.trim() == 'first-admin bootstrap is not authorized') {
    code = SonderException.bootstrapRequiredCode;
  }
  final retryAfter =
      int.tryParse(response.headers['retry-after']?.trim() ?? '');
  return SonderException(
    message,
    httpStatus: response.statusCode,
    type: type,
    code: code,
    correlationId: correlationId,
    retryable: retryable,
    retryAfterSeconds: retryAfter == null || retryAfter < 0 ? null : retryAfter,
  );
}

/// The person-facing explanation for [error] from the server at [server].
///
/// Returns a copy whose [SonderException.message] and
/// [SonderException.remedy] name the host, the setting, or the wait, so a
/// notice can show it verbatim. Errors this function does not recognise are
/// returned with their (already readable) server message.
///
/// [action] completes the FORBIDDEN sentence: "Only an administrator can
/// <action>." (default "do this").
SonderException describeServerError(SonderException error, Uri server,
    {String action = 'do this'}) {
  final host = server.host.isEmpty ? server.toString() : server.host;
  final code = error.code;
  final status = error.httpStatus;
  if (error.isHostNotAllowed) {
    final emulator = host == '10.0.2.2';
    return error.copyWith(
      message: 'The server at $host refused this address. Connect with the '
          "PC's IP (or 127.0.0.1 with `adb reverse`), or add `$host` to "
          '`[server].allowed_hosts` / `SONDER_ALLOWED_HOSTS` on the PC.',
      remedy: emulator
          ? 'Run `adb reverse tcp:11435 tcp:11435` and connect to '
              'http://127.0.0.1:11435.'
          : 'SONDER_ALLOWED_HOSTS=$host',
    );
  }
  if (code == 'AUTH_RATE_LIMITED') {
    final wait = error.retryAfterSeconds;
    return error.copyWith(
      message: wait == null
          ? 'Too many failed sign-ins from this network. Try again shortly.'
          : 'Too many failed sign-ins from this network. Try again in $wait s.',
      remedy: 'Wait, then check the API key or password.',
    );
  }
  if (code == 'WORK_CAPACITY_EXHAUSTED') {
    return error.copyWith(
      message: 'Every work slot on $host is busy. Stop a running work run, '
          'or try again later.',
      remedy: 'Open Runtime > Work runs to stop one.',
    );
  }
  if (code == SonderException.bootstrapRequiredCode) {
    return error.copyWith(
      message: 'Creating the first administrator needs the bootstrap secret '
          'from the PC.',
      remedy: 'Enter the bootstrap secret shown by the server on first run.',
    );
  }
  if (code == 'FORBIDDEN' || status == 403) {
    final lower = error.message.toLowerCase();
    if (lower.contains('work run')) {
      return error.copyWith(
        message: 'Work runs need a developer or admin account.',
        remedy: 'Sign in with a developer or admin account.',
      );
    }
    if (lower.contains('developer or admin')) {
      return error.copyWith(
        message: 'This needs a developer or admin account.',
        remedy: 'Sign in with a developer or admin account.',
      );
    }
    return error.copyWith(
      message: 'Only an administrator can $action.',
      remedy: 'Sign in as an administrator or use the deployment API key.',
    );
  }
  if (status == 401 || code == 'UNAUTHENTICATED') {
    // Keep a specific server reason ("API key expired"); replace only the
    // generic fallbacks.
    final generic = error.message.startsWith('Server returned HTTP') ||
        error.message.startsWith('Unauthorized');
    return error.copyWith(
      message: generic
          ? 'The server at $host did not accept the API key or sign-in.'
          : error.message,
      remedy: 'Check the API key or sign in again in Settings.',
    );
  }
  if (code == 'IDEMPOTENT_ACTION_COMPLETED') {
    return error.copyWith(
      message: 'That action already ran, so it was not run again.',
      remedy: 'Refresh to see its result.',
    );
  }
  if (code == 'IDEMPOTENCY_KEY_REUSED') {
    return error.copyWith(
      message: 'The server saw a different request with the same retry key, '
          'so this one was not started.',
      remedy: 'Try the action again.',
    );
  }
  if (code == 'IDEMPOTENCY_CAPACITY_EXHAUSTED' ||
      code == 'IDEMPOTENCY_RECEIPT_UNAVAILABLE') {
    final wait = error.retryAfterSeconds;
    return error.copyWith(
      message: 'The server could not record this action, so it was not '
          'started. Try again${wait == null ? ' later' : ' in $wait s'}.',
    );
  }
  if (status == 429) {
    final wait = error.retryAfterSeconds;
    return error.copyWith(
      message: 'The server at $host is busy. '
          'Try again${wait == null ? ' shortly' : ' in $wait s'}.',
    );
  }
  if (status == 503) {
    final wait = error.retryAfterSeconds;
    final base = error.message.startsWith('Server returned HTTP')
        ? 'The server at $host is not ready.'
        : error.message;
    return error.copyWith(
      message: wait == null ? base : '$base Try again in $wait s.',
    );
  }
  return error;
}

/// A cancel signal owned by one request.
///
/// Each call that accepts a token creates its own `http.Client`; [cancel]
/// closes exactly that client and completes the call with
/// [SonderException.cancelled]. Nothing else can take the slot over.
class CancelToken {
  bool _cancelled = false;
  final List<void Function()> _listeners = [];

  bool get isCancelled => _cancelled;

  void cancel() {
    if (_cancelled) return;
    _cancelled = true;
    final listeners = List.of(_listeners);
    _listeners.clear();
    for (final listener in listeners) {
      listener();
    }
  }

  /// Run [listener] on cancel (immediately if already cancelled). Returns a
  /// function that unregisters it.
  void Function() onCancel(void Function() listener) {
    if (_cancelled) {
      listener();
      return () {};
    }
    _listeners.add(listener);
    return () => _listeners.remove(listener);
  }
}

/// `http.Client` wrapper that refuses redirects, so credentials never follow
/// a 3xx to another origin.
class NoRedirectClient extends http.BaseClient {
  final http.Client inner;
  NoRedirectClient(this.inner);
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    request.followRedirects = false;
    return inner.send(request);
  }

  @override
  void close() => inner.close();
}

final RegExp _refusedPattern = RegExp(
  r'Connection refused|actively refused|refused the network connection|'
  r'errno = (111|61|10061|1225)\b',
  caseSensitive: false,
);

final RegExp _dnsPattern = RegExp(
  r'Failed host lookup|No address associated with hostname|'
  r'nodename nor servname|Name or service not known|'
  r'Temporary failure in name resolution|No such host is known|'
  r'errno = (7|8|11001|11004)\b',
  caseSensitive: false,
);

final RegExp _afterWritePattern = RegExp(
  r'Connection closed before full header|Connection reset|Broken pipe|'
  r'HandshakeException|CERTIFICATE|TlsException|TimeoutException|timed out',
  caseSensitive: false,
);

/// Whether [error] proves the request never reached the server.
///
/// Only "connection refused" and "host name did not resolve" qualify: both
/// fail before a single request byte is written, so re-sending elsewhere
/// cannot run a tool twice. Everything else (timeouts, TLS failures, resets,
/// a connection closed mid-response) returns false. Classification is by the
/// error text so this library stays free of `dart:io` for the web build.
bool isPreRequestConnectFailure(Object error) {
  if (error is TimeoutException || error is SonderException) return false;
  final text = error.toString();
  if (_afterWritePattern.hasMatch(text)) return false;
  return _refusedPattern.hasMatch(text) || _dnsPattern.hasMatch(text);
}

/// A new random `Idempotency-Key` (8-128 of `[A-Za-z0-9._:-]`).
String newIdempotencyKey([String prefix = 'app']) {
  final random = Random.secure();
  final bytes = List<int>.generate(16, (_) => random.nextInt(256));
  final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  return '$prefix-$hex';
}

/// Where requests go and which credentials they carry.
class SonderEndpoint {
  final String baseUrl;
  final String apiKey;
  final AccountSession? accountSession;

  const SonderEndpoint({
    required this.baseUrl,
    this.apiKey = '',
    this.accountSession,
  });

  Uri get serverUri => Uri.tryParse(baseUrl.trim()) ?? Uri();

  Uri uri(String path, [String? rootUrl]) {
    final root = (rootUrl ?? baseUrl).trim().replaceAll(RegExp(r'/+$'), '');
    return Uri.parse('$root$path');
  }

  /// Request headers. [keyOverride] replaces the API key and drops the
  /// account token (the local fallback gets no credentials at all).
  Map<String, String> headers([String? keyOverride]) {
    final h = <String, String>{'Content-Type': 'application/json'};
    final key = keyOverride ?? apiKey;
    if (key.trim().isNotEmpty) {
      h['Authorization'] = 'Bearer ${key.trim()}';
    }
    if (keyOverride == null && accountSession?.matches(baseUrl) == true) {
      h['X-Sonder-Account-Token'] = accountSession!.token;
    }
    return h;
  }
}

/// Send one request on its own client, bounded by [timeout] and [cancel].
///
/// The client is always closed: on success, on error, on timeout (so the
/// socket does not keep running after the caller gave up), and on cancel.
/// A cancel or timeout completes immediately even if the underlying future
/// has not yet noticed the close.
Future<http.Response> sendRequest(
  String method,
  Uri uri, {
  Map<String, String>? headers,
  Object? body,
  Duration timeout = const Duration(seconds: 20),
  CancelToken? cancel,
}) async {
  if (cancel?.isCancelled == true) throw SonderException.cancelled();
  final client = NoRedirectClient(http.Client());
  final done = Completer<http.Response>();
  void Function()? unregister;
  Timer? timer;
  var closed = false;
  void closeOnce() {
    if (closed) return;
    closed = true;
    client.close();
  }

  unregister = cancel?.onCancel(() {
    closeOnce();
    if (!done.isCompleted) done.completeError(SonderException.cancelled());
  });
  timer = Timer(timeout, () {
    closeOnce();
    if (!done.isCompleted) {
      done.completeError(TimeoutException('request timed out', timeout));
    }
  });
  () async {
    try {
      final request = http.Request(method, uri);
      if (headers != null) request.headers.addAll(headers);
      if (body is String) {
        request.body = body;
      } else if (body is List<int>) {
        request.bodyBytes = body;
      }
      final streamed = await client.send(request);
      final response = await http.Response.fromStream(streamed);
      if (!done.isCompleted) done.complete(response);
    } catch (error, stack) {
      if (!done.isCompleted) done.completeError(error, stack);
    }
  }();
  try {
    return await done.future;
  } finally {
    timer.cancel();
    unregister?.call();
    closeOnce();
  }
}

Future<http.Response> requestGet(Uri uri,
        {Map<String, String>? headers,
        Duration timeout = const Duration(seconds: 20),
        CancelToken? cancel}) =>
    sendRequest('GET', uri, headers: headers, timeout: timeout, cancel: cancel);

Future<http.Response> requestPost(Uri uri,
        {Map<String, String>? headers,
        Object? body,
        Duration timeout = const Duration(seconds: 20),
        CancelToken? cancel}) =>
    sendRequest('POST', uri,
        headers: headers, body: body, timeout: timeout, cancel: cancel);

/// Decode a 2xx JSON object body or throw a readable [SonderException].
Map<String, dynamic> decodeJsonObject(http.Response response, String what) {
  try {
    final decoded = jsonDecode(utf8.decode(response.bodyBytes));
    if (decoded is Map<String, dynamic>) return decoded;
    if (decoded is Map) return Map<String, dynamic>.from(decoded);
  } catch (_) {
    // Fall through to the readable parse error.
  }
  throw SonderException('Could not parse $what.');
}

/// Readable fallback for a status with no usable body.
String httpStatusFallback(int status) => 'Server returned HTTP $status.';
