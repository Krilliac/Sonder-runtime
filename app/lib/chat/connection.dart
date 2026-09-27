import '../api.dart';

/// One reachability state shared by the rail dot, the empty state and the
/// offline notice, so they can never disagree (P0-3). Colour never carries
/// it alone: every state has a word.
enum ConnState {
  connecting,
  connected,
  refused,
  unauthorized,
  serverError,
  unreachable
}

class ConnectionStatus {
  final ConnState state;

  /// `mypc.local:11435`: the server as a person would name it.
  final String host;

  /// Extra words for the notice ("HTTP 503"), never a raw exception.
  final String detail;

  const ConnectionStatus(this.state, this.host, {this.detail = ''});

  factory ConnectionStatus.connecting(String serverUrl) =>
      ConnectionStatus(ConnState.connecting, hostOf(serverUrl));

  static String hostOf(String serverUrl) => serverUrl
      .trim()
      .replaceFirst(RegExp(r'^https?://'), '')
      .replaceAll(RegExp(r'/+$'), '');

  /// Classify a failed status read. Today's API folds most non-2xx replies
  /// into "Server returned HTTP n."; lane A adds `httpStatus`/`code`, which
  /// are preferred when present.
  factory ConnectionStatus.fromError(Object error, String serverUrl) {
    final host = hostOf(serverUrl);
    int? status;
    var code = '';
    var message = error.toString();
    if (error is SonderException) {
      status = error.httpStatus;
      code = error.code;
      message = error.message;
    }
    status ??= int.tryParse(
        RegExp(r'HTTP (\d{3})').firstMatch(message)?.group(1) ?? '');
    if (status == 421 || code == 'HOST_NOT_ALLOWED') {
      return ConnectionStatus(ConnState.refused, host);
    }
    if (status == 401 || status == 403 || message.startsWith('Unauthorized')) {
      return ConnectionStatus(ConnState.unauthorized, host,
          detail: status == null ? '' : 'HTTP $status');
    }
    if (status != null) {
      return ConnectionStatus(ConnState.serverError, host,
          detail: 'HTTP $status');
    }
    return ConnectionStatus(ConnState.unreachable, host);
  }

  bool get isConnected => state == ConnState.connected;

  /// The transport is down or the server refused this address: chat cannot
  /// work, so the offline notice and the disabled mode chip apply.
  bool get isOffline =>
      state == ConnState.unreachable || state == ConnState.refused;

  /// The status word, as the REPL vocabulary spells it.
  String get word => switch (state) {
        ConnState.connecting => 'connecting',
        ConnState.connected => 'connected',
        ConnState.refused => 'refused',
        ConnState.unauthorized => 'needs sign-in',
        ConnState.serverError => 'error',
        ConnState.unreachable => "can't reach",
      };

  /// ✓ ok, ! warn, ✗ fail, · note.
  String get glyph => switch (state) {
        ConnState.connecting => '·',
        ConnState.connected => '✓',
        ConnState.refused => '!',
        ConnState.unauthorized => '!',
        ConnState.serverError => '!',
        ConnState.unreachable => '✗',
      };

  /// One sentence for the empty state and the rail tooltip.
  String get sentence => switch (state) {
        ConnState.connecting => 'Connecting to $host…',
        ConnState.connected => 'Connected to $host',
        ConnState.refused => '$host refused this address',
        ConnState.unauthorized => '$host needs a key or sign-in',
        ConnState.serverError =>
          '$host answered with an error${detail.isEmpty ? '' : ' ($detail)'}',
        ConnState.unreachable => "Can't reach $host",
      };

  /// The one-line remedy under the sentence.
  String get remedy => switch (state) {
        ConnState.connecting => '',
        ConnState.connected => '',
        ConnState.refused =>
          "Use the PC's IP address, or add this host to allowed_hosts "
              '(SONDER_ALLOWED_HOSTS) on the PC.',
        ConnState.unauthorized => 'Check the API key or account in Settings.',
        ConnState.serverError => 'Retry, or open Runtime to see what failed.',
        ConnState.unreachable =>
          'Check that the server is running and the address in Settings.',
      };

  @override
  bool operator ==(Object other) =>
      other is ConnectionStatus &&
      other.state == state &&
      other.host == host &&
      other.detail == detail;

  @override
  int get hashCode => Object.hash(state, host, detail);
}
