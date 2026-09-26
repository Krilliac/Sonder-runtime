import 'local_manager_models.dart';

/// Browser clients use the authenticated server/host-launcher APIs. They cannot
/// inspect or control processes and files on the computer hosting the browser.
class LocalManager {
  static const unavailableMessage =
      'Local files and processes are unavailable in the browser. '
      'Use the desktop app for local setup, or configure an authenticated host launcher in Settings.';
  static bool get canRunLocalTools => false;
  static String get platformLabel => 'Web browser';
  static String sharedHomePath() => '';
  static String runDirectoryPath() => '';
  static String serverLogPath() => '';
  static String launcherHealthTokenPath() => '';
  static String managedServerOutputTail() => '';
  static Future<String> readServerLogTail({int maxLines = 40}) async => '';
  static Future<String> readLogTail(String path, {int maxLines = 40}) async =>
      '';
  static Map<String, String> processEnvironment(
          {bool allowHosted = false, String contextSize = '8192'}) =>
      const {};
  static Future<bool> defaultServerReachable() async => false;
  static Future<bool> waitForServer({
    Duration timeout = const Duration(seconds: 25),
    Duration interval = const Duration(milliseconds: 400),
    Future<bool> Function()? reachabilityProbe,
    Future<void> Function(Duration)? delay,
    DateTime Function()? clock,
  }) async =>
      false;
  static Future<LocalInstallInfo> inspect() async => const LocalInstallInfo(
      platform: 'Web browser',
      appDir: '',
      systemDir: '',
      sharedHome: '',
      canLaunch: false,
      systemExists: false,
      gitCheckout: false,
      serverScript: false,
      trainingScript: false,
      bootstrapScript: false,
      engineBundle: false,
      defaultServerReachable: false);
  static Future<LocalActionResult> setupEngine(
          {bool allowHosted = false, String contextSize = '8192'}) async =>
      const LocalActionResult(false, unavailableMessage);
  static Future<LocalActionResult> startServer({
    bool allowHosted = false,
    String contextSize = '8192',
    bool persistOnAppClose = false,
    Duration readyTimeout = const Duration(seconds: 25),
    Future<bool> Function()? managedReachabilityProbe,
    Future<bool> Function()? portOccupiedProbe,
  }) async =>
      const LocalActionResult(false, unavailableMessage);
  static void stopManagedServerNow() {}
  static Future<LocalActionResult> stopServers() async =>
      const LocalActionResult(false, unavailableMessage);
  static Future<LocalActionResult> startEndlessTraining() async =>
      const LocalActionResult(false, unavailableMessage);
  static Future<LocalActionResult> updateFromGit() async =>
      const LocalActionResult(false, unavailableMessage);

  /// A browser cannot start processes: this returns the unavailable result
  /// with the Observatory web URL to copy, when one is configured (contract
  /// section 10). [executable] is accepted so callers compile on both
  /// platforms; a browser never uses it.
  static Future<ObservatoryLaunchResult> launchObservatory(
    List<String> connectUrls, {
    String runtimeUrl = '',
    String executable = '',
    String webUrl = '',
  }) async {
    final urls = observatoryConnectUrls(connectUrls);
    final blocked =
        observatoryLaunchBlocked(runtimeUrl: runtimeUrl, connectUrls: urls);
    if (blocked != null) return blocked;
    final url = observatoryWebLaunchUrl(webUrl, urls) ?? '';
    return ObservatoryLaunchResult(
      ok: false,
      mode: ObservatoryLaunchMode.unavailable,
      message: url.isEmpty
          ? 'The browser cannot start the Observatory. Set an Observatory web '
              'URL in Settings to get a link, or copy the connect URLs.'
          : 'The browser cannot start the Observatory. Copy this link and '
              'open it in a new tab.',
      url: url,
    );
  }
}
