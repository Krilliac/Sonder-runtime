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
}
