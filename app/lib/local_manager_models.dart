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
