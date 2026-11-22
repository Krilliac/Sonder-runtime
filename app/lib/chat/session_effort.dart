/// Decides when a chat request carries `session_reasoning_effort`.
///
/// Mirrors [SessionTemperatureSync]: the server stores reasoning effort per
/// session (also set by typing `/effort low`), and an absent field leaves it
/// alone. The app sends the current effort value on a session's first request
/// and on any change since the last request the server accepted.
class SessionEffortSync {
  /// The wire value `session_reasoning_effort` takes for Auto.
  static const auto = 'auto';

  final Map<String, Object> _confirmed = <String, Object>{};

  /// The `session_reasoning_effort` for the next request on [sessionId]
  /// ([effort], null for Auto), or null to leave the field out.
  Object? pending(String sessionId, String? effort) {
    final Object wanted = effort ?? auto;
    return _confirmed[sessionId] == wanted ? null : wanted;
  }

  /// The server accepted a request that carried [sent] for [sessionId].
  void confirm(String sessionId, Object sent) => _confirmed[sessionId] = sent;

  /// Forget what the server holds for [sessionId] (its request failed).
  void forget(String sessionId) => _confirmed.remove(sessionId);

  /// Forget every session (a different or restarted server).
  void reset() => _confirmed.clear();
}
