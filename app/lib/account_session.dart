/// Account bearer bound to the exact authenticated server origin.
class AccountSession {
  final String token;
  final String origin;
  AccountSession({required this.token, required String origin})
      : origin = serverOrigin(origin) {
    if (token.isEmpty || token.length > 512 ||
        token.codeUnits.any((unit) => unit < 33 || unit > 126)) {
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
  final host = uri.host;
  final octets = host.split('.');
  final ipv4Loopback = octets.length == 4 && octets.first == '127' &&
      octets.every((part) {
        final n = int.tryParse(part);
        return n != null && n >= 0 && n <= 255 && n.toString() == part;
      });
  if (uri.scheme == 'http' && host != '::1' && !ipv4Loopback) {
    throw ArgumentError('Account traffic requires HTTPS or numeric loopback');
  }
  return uri.origin;
}
