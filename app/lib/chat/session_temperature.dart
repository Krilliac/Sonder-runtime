/// Decides when a chat request carries `session_temperature`.
///
/// The server keeps a temperature per session (also set by typing
/// `/temp 0.7`), and an absent field leaves it alone. So the app sends the
/// Settings value only when it differs from what was last sent for that
/// session id; a `/temp` typed in chat is then not overwritten on every turn.
class SessionTemperatureSync {
  /// The wire value `session_temperature` takes for Auto.
  static const auto = 'auto';

  final Map<String, Object> _sent = <String, Object>{};

  /// The `session_temperature` value for the next request on [sessionId]
  /// ([temperature], or null for Auto), or null to leave the field out.
  /// Records what it returns, so call it once per request.
  ///
  /// A session's first request sends the value only when it is not Auto;
  /// after that, a change sends the new number, or `"auto"` when switched
  /// back to Auto.
  Object? next(String sessionId, double? temperature) {
    final Object wanted = temperature ?? auto;
    final last = _sent[sessionId];
    if (last == null) {
      _sent[sessionId] = wanted;
      return temperature;
    }
    if (last == wanted) return null;
    _sent[sessionId] = wanted;
    return wanted;
  }
}
