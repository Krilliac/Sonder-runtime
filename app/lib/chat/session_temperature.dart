/// Decides when a chat request carries `session_temperature`.
///
/// The server keeps a temperature per session (also set by typing
/// `/temp 0.7`), and an absent field leaves it alone. So the app sends the
/// Settings value when the server may not have it: a session's first
/// request (the number, or `"auto"`), and any change since the last request
/// the server accepted. A value counts as sent only after [confirm]; a
/// failed turn, a new server or a reconnect makes the next request resend.
class SessionTemperatureSync {
  /// The wire value `session_temperature` takes for Auto.
  static const auto = 'auto';

  final Map<String, Object> _confirmed = <String, Object>{};

  /// The `session_temperature` for the next request on [sessionId]
  /// ([temperature], null for Auto), or null to leave the field out.
  /// Does not record anything.
  Object? pending(String sessionId, double? temperature) {
    final Object wanted = temperature ?? auto;
    return _confirmed[sessionId] == wanted ? null : wanted;
  }

  /// The server accepted a request that carried [sent] for [sessionId].
  void confirm(String sessionId, Object sent) => _confirmed[sessionId] = sent;

  /// Forget what the server holds for [sessionId] (its request failed).
  void forget(String sessionId) => _confirmed.remove(sessionId);

  /// Forget every session (a different or restarted server).
  void reset() => _confirmed.clear();
}
