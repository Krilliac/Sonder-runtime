/// Decides when a chat request carries `persona`.
///
/// The server defaults to `"auto"` (or an existing session `/persona` pin) when
/// `"persona"` is omitted. The app:
/// - omits `"persona"` while untouched at Auto (`null`), preserving default
///   payloads and any external session pin;
/// - sends the pinned persona (`"off"` or one of the 9 persona names) on every
///   turn while pinned, so both per-request and session-sticky servers stay
///   aligned with the UI;
/// - sends `"auto"` when explicitly toggled back to Auto (or on any session
///   that previously received a pinned persona) until a turn confirms it.
class SessionPersonaSync {
  /// The wire value `persona` takes when explicitly resetting to Auto.
  static const auto = 'auto';

  final Map<String, String> _confirmed = <String, String>{};
  final Set<String> _explicitAuto = <String>{};

  /// Mark [sessionId] as needing an explicit `"auto"` reset on its next turn.
  void requestAuto(String sessionId) => _explicitAuto.add(sessionId);

  /// The `persona` value for the next request on [sessionId] ([persona], null
  /// for Auto), or null to omit the field. Does not mutate state.
  String? pending(String sessionId, String? persona) {
    if (persona != null) return persona;
    final last = _confirmed[sessionId];
    if ((last != null && last != auto) || _explicitAuto.contains(sessionId)) {
      return auto;
    }
    return null;
  }

  /// The server accepted a request that carried [sent] for [sessionId].
  void confirm(String sessionId, String sent) {
    _confirmed[sessionId] = sent;
    _explicitAuto.remove(sessionId);
  }

  /// Forget what the server holds for [sessionId] (its request failed or was
  /// cancelled). When the failed turn was resetting to `"auto"`, keep the
  /// explicit reset pending so the next turn retries it.
  void forget(String sessionId, {String? sent}) {
    _confirmed.remove(sessionId);
    if (sent == auto) {
      _explicitAuto.add(sessionId);
    }
  }

  /// Forget every session (a different or restarted server).
  void reset() {
    _confirmed.clear();
    _explicitAuto.clear();
  }
}
