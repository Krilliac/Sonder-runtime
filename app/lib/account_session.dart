/// Account bearer bound to the exact authenticated server origin.
class AccountSession {
  final String token;
  final String origin;
  AccountSession({required this.token, required String origin})
      : origin = serverOrigin(origin) {
    if (token.trim().isEmpty || token.contains(RegExp(r'[\r\n]'))) {
      throw ArgumentError('Invalid account session');
    }
  }
  bool matches(String url) {
    try {
      return origin == serverOrigin(url);
    } catch (_) {
      return false;
    }
  }
}

String serverOrigin(String value) {
  final uri = Uri.parse(value.trim());
  if (!{'http', 'https'}.contains(uri.scheme) ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      uri.hasQuery ||
      uri.hasFragment) {
    throw ArgumentError('Invalid server origin');
  }
  return uri.origin;
}
