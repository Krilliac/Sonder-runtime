class LocalActionResult {
  final bool ok;
  final String message;

  /// Absolute path of the startup log that explains a failed launch, or an
  /// empty string when the action has no log to point at.
  final String logPath;

  /// Captured launcher output and/or the tail of [logPath]. Empty when there
  /// was nothing to read.
  final String logTail;

  const LocalActionResult(
    this.ok,
    this.message, {
    this.logPath = '',
    this.logTail = '',
  });

  bool get hasLogDetail => logPath.isNotEmpty || logTail.isNotEmpty;
}

class LocalInstallInfo {
  final String platform;
  final String appDir;
  final String systemDir;
  final String sharedHome;
  final bool canLaunch;
  final bool systemExists;
  final bool gitCheckout;
  final bool serverScript;
  final bool trainingScript;
  final bool bootstrapScript;
  final bool engineBundle;
  final bool defaultServerReachable;

  const LocalInstallInfo({
    required this.platform,
    required this.appDir,
    required this.systemDir,
    required this.sharedHome,
    required this.canLaunch,
    required this.systemExists,
    required this.gitCheckout,
    required this.serverScript,
    required this.trainingScript,
    required this.bootstrapScript,
    required this.engineBundle,
    required this.defaultServerReachable,
  });
}

/// How [LocalManager.launchObservatory] opened (or did not open) the
/// Observatory.
enum ObservatoryLaunchMode {
  /// The Observatory executable was started with one `--connect` per URL.
  executable,

  /// The configured Observatory web URL was opened with the OS opener.
  webUrl,

  /// Nothing can be launched on this platform or configuration. The result
  /// message says what to set up; its `url` may hold a URL to copy.
  unavailable,

  /// Launching is not allowed here: a non-loopback runtime, or no URLs.
  disabled,
}

/// The outcome of an Observatory launch. [arguments] and [url] never carry
/// a token or API key: producers are reached on loopback and the
/// Observatory asks for any credential itself.
class ObservatoryLaunchResult {
  final bool ok;
  final ObservatoryLaunchMode mode;
  final String message;

  /// The executable started, for [ObservatoryLaunchMode.executable].
  final String executable;

  /// Its arguments: `--connect <url>` per URL.
  final List<String> arguments;

  /// The web URL opened, or offered for copying.
  final String url;

  const ObservatoryLaunchResult({
    required this.ok,
    required this.mode,
    required this.message,
    this.executable = '',
    this.arguments = const [],
    this.url = '',
  });
}

/// Environment variable naming the Observatory executable (contract 10).
const observatoryBinEnv = 'SONDER_OBSERVATORY_BIN';

/// The executable looked up on PATH when nothing else names one.
const observatoryExecutableName = 'sonder-observatory';

/// Shown when no executable and no web URL are available.
const observatoryGuidance =
    'Sonder Observatory was not found. Set its executable in Settings, set '
    '$observatoryBinEnv, or put $observatoryExecutableName on PATH. You can '
    'also set an Observatory web URL in Settings (for example '
    'http://127.0.0.1:4173/ for a local preview build) and open that instead.';

/// Shown when the app talks to a runtime on another host.
const observatoryRemoteExplanation =
    'Opening the Observatory works only for a runtime on this computer: live '
    'telemetry is served on loopback on the runtime host. Run the '
    'Observatory on that host instead.';

/// True for `localhost`, `127.x.x.x` and `::1`.
bool isLoopbackHost(String host) {
  final h = host.toLowerCase();
  return h == 'localhost' ||
      h == '::1' ||
      h == '[::1]' ||
      h == '0:0:0:0:0:0:0:1' ||
      RegExp(r'^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$').hasMatch(h);
}

/// True when [url] parses and names a loopback host.
bool isLoopbackUrl(String url) {
  final uri = Uri.tryParse(url.trim());
  return uri != null && uri.host.isNotEmpty && isLoopbackHost(uri.host);
}

/// The connect URLs that are safe to hand to the Observatory: absolute
/// http(s) base URLs with no credentials, query or fragment (so no token can
/// ride along), without a trailing slash, de-duplicated, in order.
List<String> observatoryConnectUrls(Iterable<String> urls) {
  final seen = <String>{};
  final out = <String>[];
  for (final raw in urls) {
    final uri = Uri.tryParse(raw.trim());
    if (uri == null ||
        !const {'http', 'https'}.contains(uri.scheme.toLowerCase()) ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment) {
      continue;
    }
    final text = uri.toString().replaceAll(RegExp(r'/+$'), '');
    if (seen.add(text)) out.add(text);
  }
  return out;
}

/// Why [url] cannot be the Observatory web URL, or null when it can (or is
/// empty, meaning "not set"). A non-loopback URL must use HTTPS.
String? observatoryWebUrlError(String url) {
  final text = url.trim();
  if (text.isEmpty) return null;
  final uri = Uri.tryParse(text);
  if (uri == null ||
      !const {'http', 'https'}.contains(uri.scheme.toLowerCase()) ||
      uri.host.isEmpty) {
    return 'Observatory web URL must be an http(s) URL.';
  }
  if (uri.userInfo.isNotEmpty || uri.hasQuery || uri.hasFragment) {
    return 'Observatory web URL must not contain credentials, a query or a '
        'fragment.';
  }
  if (!isLoopbackHost(uri.host) && uri.scheme.toLowerCase() != 'https') {
    return 'A non-loopback Observatory web URL requires HTTPS.';
  }
  return null;
}

/// `<webUrl>?fixture=0&connect=<url>&connect=<url>`, every value
/// URL-encoded. Null when [webUrl] is empty or invalid.
String? observatoryWebLaunchUrl(String webUrl, List<String> connectUrls) {
  if (webUrl.trim().isEmpty || observatoryWebUrlError(webUrl) != null) {
    return null;
  }
  final base = Uri.parse(webUrl.trim());
  final query = [
    'fixture=0',
    for (final url in connectUrls) 'connect=${Uri.encodeQueryComponent(url)}',
  ].join('&');
  return base.replace(query: query).toString();
}

/// The launch that must not happen: a non-loopback runtime, or nothing to
/// connect to. Null when a launch may proceed.
ObservatoryLaunchResult? observatoryLaunchBlocked({
  required String runtimeUrl,
  required List<String> connectUrls,
}) {
  if (!isLoopbackUrl(runtimeUrl)) {
    return const ObservatoryLaunchResult(
      ok: false,
      mode: ObservatoryLaunchMode.disabled,
      message: observatoryRemoteExplanation,
    );
  }
  if (connectUrls.isEmpty) {
    return const ObservatoryLaunchResult(
      ok: false,
      mode: ObservatoryLaunchMode.disabled,
      message: 'The runtime reported no telemetry URLs to connect to. Turn on '
          'live export (SONDER_OBSERVATORY_EXPORT=1) or bind a provider that '
          'publishes telemetry.',
    );
  }
  return null;
}

/// True when [path] names a macOS application bundle (`…/Observatory.app`),
/// which is a directory started through `open`, not an executable file.
bool isMacAppBundle(String path) =>
    path.trim().replaceAll(RegExp(r'/+$'), '').toLowerCase().endsWith('.app');

/// The program and arguments that start the Observatory at [executable] on
/// [operatingSystem] (a `Platform.operatingSystem` value). A macOS `.app`
/// bundle is started with `open -n -a <bundle> --args …` so the connect
/// arguments reach a fresh instance; anything else is run directly.
(String, List<String>) observatoryProcessCommand(
    String executable, List<String> arguments,
    {required String operatingSystem}) {
  if (operatingSystem == 'macos' && isMacAppBundle(executable)) {
    return ('open', ['-n', '-a', executable, '--args', ...arguments]);
  }
  return (executable, arguments);
}

/// The OS opener command for [url] on [operatingSystem]: `cmd.exe /c start`
/// on Windows, `open` on macOS, `xdg-open` elsewhere.
///
/// cmd parses its command line itself, so its metacharacters are escaped
/// with `^`: the `&` between query parameters must not end the command.
(String, List<String>) observatoryOpenerCommand(String url,
    {required String operatingSystem}) {
  switch (operatingSystem) {
    case 'windows':
      final escaped = url.replaceAllMapped(
          RegExp(r'[&|<>^()]'), (match) => '^${match.group(0)}');
      return ('cmd.exe', ['/c', 'start', '', escaped]);
    case 'macos':
      return ('open', [url]);
    default:
      return ('xdg-open', [url]);
  }
}
