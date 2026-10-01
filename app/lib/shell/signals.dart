import 'dart:async';

import 'package:flutter/foundation.dart';

import '../api.dart';
import '../chat/connection.dart';

/// Reads the server's approvals ledger; null when the server has no
/// approvals route (it predates them).
typedef ApprovalsReader = Future<ApprovalsSnapshot?> Function();

/// How many refused calls wait for a person to approve them, for the
/// Runtime badge in the sidebar. The chat status poll does not carry it, so
/// this reads `GET /v1/approvals` on its own slow cadence:
///
/// * only while the server answers (the chat poll's [connection]);
/// * never again for an identity that may not read approvals (401/403) or
///   a server without the route (404), until [reset];
/// * paused while the app is in the background.
///
/// [waiting] is null whenever the count is not known, so the badge hides
/// instead of showing a stale or invented number.
class ApprovalsWatch {
  ApprovalsWatch({
    required ApprovalsReader read,
    required this.connection,
    this.interval = const Duration(seconds: 20),
  }) : _read = read;

  ApprovalsReader _read;
  final ValueListenable<ConnectionStatus> connection;
  final Duration interval;

  final ValueNotifier<int?> waiting = ValueNotifier<int?>(null);

  Timer? _timer;
  bool _started = false;
  bool _inFlight = false;
  bool _gaveUp = false;
  bool _paused = false;
  bool _disposed = false;
  int _generation = 0;

  void start() {
    if (_started || _disposed) return;
    _started = true;
    connection.addListener(_connectionChanged);
    unawaited(poll());
  }

  /// A different server, key or account: forget what was known and read
  /// again with [read] when given.
  void reset({ApprovalsReader? read}) {
    if (_disposed) return;
    if (read != null) _read = read;
    _generation++;
    _timer?.cancel();
    _inFlight = false;
    _gaveUp = false;
    waiting.value = null;
    if (_started) unawaited(poll());
  }

  void pause() {
    _paused = true;
    _timer?.cancel();
  }

  void resume() {
    if (!_paused) return;
    _paused = false;
    unawaited(poll());
  }

  void _connectionChanged() {
    if (connection.value.isConnected) {
      if (_timer == null || !_timer!.isActive) unawaited(poll());
    } else {
      waiting.value = null;
    }
  }

  /// One read, never overlapping another.
  Future<void> poll() async {
    if (_disposed || _paused || _gaveUp || _inFlight) return;
    _timer?.cancel();
    if (!connection.value.isConnected) {
      waiting.value = null;
      return;
    }
    _inFlight = true;
    final generation = _generation;
    try {
      final snapshot = await _read();
      if (_disposed || generation != _generation) return;
      if (snapshot == null) {
        _gaveUp = true;
        waiting.value = null;
        return;
      }
      waiting.value = snapshot.pending.length;
    } on SonderException catch (e) {
      if (_disposed || generation != _generation) return;
      final status = e.httpStatus;
      if (status == 401 || status == 403 || status == 404) {
        _gaveUp = true;
        waiting.value = null;
      }
      // Anything else is transient: keep the last count, read again later.
    } catch (_) {
      // Transient (transport, parse): read again later.
    } finally {
      if (!_disposed && generation == _generation) {
        _inFlight = false;
        if (!_gaveUp && !_paused) _timer = Timer(interval, poll);
      }
    }
  }

  void dispose() {
    if (_disposed) return;
    _disposed = true;
    _timer?.cancel();
    if (_started) connection.removeListener(_connectionChanged);
    waiting.dispose();
  }
}
