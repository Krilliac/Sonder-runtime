import 'dart:io';

class LocalActionResult {
  final bool ok;
  final String message;

  const LocalActionResult(this.ok, this.message);
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
    required this.defaultServerReachable,
  });
}

class LocalManager {
  static const _repoUrl = 'https://github.com/Krilliac/Trilobite-model.git';

  static bool get canRunLocalTools =>
      Platform.isWindows || Platform.isLinux || Platform.isMacOS;

  static String get platformLabel {
    if (Platform.isWindows) return 'Windows';
    if (Platform.isLinux) return 'Linux';
    if (Platform.isMacOS) return 'macOS';
    if (Platform.isAndroid) return 'Android';
    if (Platform.isIOS) return 'iOS';
    return 'this platform';
  }

  static Directory appDirectory() {
    final exe = File(Platform.resolvedExecutable);
    return exe.parent;
  }

  static Directory bundledSystemDirectory() {
    final desktopSibling =
        Directory('${appDirectory().path}${Platform.pathSeparator}local-system');
    if (desktopSibling.existsSync()) return desktopSibling;
    if (Platform.isMacOS) {
      final contentsDir = appDirectory().parent;
      final resources = Directory(
        '${contentsDir.path}${Platform.pathSeparator}Resources'
        '${Platform.pathSeparator}local-system',
      );
      if (resources.existsSync()) return resources;
    }
    return desktopSibling;
  }

  static String sharedHomePath() {
    final existing = Platform.environment['TRILOBITE_HOME'];
    if (existing != null && existing.trim().isNotEmpty) {
      return existing;
    }
    if (Platform.isWindows) {
      final root = Platform.environment['LOCALAPPDATA'] ??
          Platform.environment['APPDATA'] ??
          Platform.environment['USERPROFILE'] ??
          appDirectory().path;
      return '$root${Platform.pathSeparator}trilobite';
    }
    final xdg = Platform.environment['XDG_DATA_HOME'];
    if (xdg != null && xdg.trim().isNotEmpty) {
      return '$xdg${Platform.pathSeparator}trilobite';
    }
    final home = Platform.environment['HOME'] ?? appDirectory().path;
    return '$home${Platform.pathSeparator}.local'
        '${Platform.pathSeparator}share'
        '${Platform.pathSeparator}trilobite';
  }

  static Map<String, String> processEnvironment() {
    return {
      ...Platform.environment,
      'TRILOBITE_HOME': sharedHomePath(),
    };
  }

  static Future<bool> defaultServerReachable() async {
    try {
      final socket = await Socket.connect(
        InternetAddress.loopbackIPv4,
        11435,
        timeout: const Duration(milliseconds: 350),
      );
      socket.destroy();
      return true;
    } catch (_) {
      return false;
    }
  }

  static Future<LocalInstallInfo> inspect() async {
    final system = bundledSystemDirectory();
    final systemExists = await system.exists();
    Future<bool> hasFile(String name) async {
      return File('${system.path}${Platform.pathSeparator}$name').exists();
    }
    final gitDir = Directory('${system.path}${Platform.pathSeparator}.git');
    final gitCheckout = systemExists && await gitDir.exists();
    final serverScript = systemExists && await hasFile('trilobite-serve.cmd');
    final trainingScript = systemExists && await hasFile('endless-train.cmd');
    final reachable = await defaultServerReachable();

    return LocalInstallInfo(
      platform: platformLabel,
      appDir: appDirectory().path,
      systemDir: system.path,
      sharedHome: sharedHomePath(),
      canLaunch: canRunLocalTools,
      systemExists: systemExists,
      gitCheckout: gitCheckout,
      serverScript: serverScript,
      trainingScript: trainingScript,
      defaultServerReachable: reachable,
    );
  }

  static Future<LocalActionResult> startServer() async {
    if (!canRunLocalTools) {
      return LocalActionResult(
        false,
        'Local process startup is not available on $platformLabel. Run the server on a desktop or LAN host.',
      );
    }
    final system = bundledSystemDirectory();
    if (await defaultServerReachable()) {
      return const LocalActionResult(
        true,
        'A server is already reachable on 127.0.0.1:11435.',
      );
    }
    if (!await system.exists()) {
      return const LocalActionResult(
        false,
        'No bundled local-system folder found next to the app.',
      );
    }
    try {
      if (Platform.isWindows) {
        final script = File('${system.path}${Platform.pathSeparator}trilobite-serve.cmd');
        if (await script.exists()) {
          await Process.start(
            'cmd.exe',
            ['/c', 'start', '', '/min', script.path],
            workingDirectory: system.path,
            environment: processEnvironment(),
            runInShell: true,
          );
          return const LocalActionResult(true, 'Server startup requested.');
        }
      }
      final python = Platform.isWindows ? 'python.exe' : 'python3';
      await Process.start(
        python,
        ['trilobite_serve.py'],
        workingDirectory: system.path,
        environment: processEnvironment(),
        mode: ProcessStartMode.detached,
      );
      return const LocalActionResult(true, 'Server startup requested.');
    } catch (e) {
      return LocalActionResult(false, 'Could not start server: $e');
    }
  }

  static Future<LocalActionResult> startEndlessTraining() async {
    if (!canRunLocalTools) {
      return const LocalActionResult(false, 'Training launcher is desktop-only.');
    }
    final system = bundledSystemDirectory();
    if (!await system.exists()) {
      return const LocalActionResult(false, 'No bundled local-system folder found.');
    }
    try {
      if (Platform.isWindows) {
        final script = File('${system.path}${Platform.pathSeparator}endless-train.cmd');
        if (await script.exists()) {
          await Process.start(
            'cmd.exe',
            ['/c', 'start', '', script.path],
            workingDirectory: system.path,
            environment: processEnvironment(),
            runInShell: true,
          );
          return const LocalActionResult(true, 'Endless training started.');
        }
      }
      await Process.start(
        Platform.isWindows ? 'python.exe' : 'python3',
        ['endless_train.py'],
        workingDirectory: system.path,
        environment: processEnvironment(),
        mode: ProcessStartMode.detached,
      );
      return const LocalActionResult(true, 'Endless training started.');
    } catch (e) {
      return LocalActionResult(false, 'Could not start training: $e');
    }
  }

  static Future<LocalActionResult> updateFromGit() async {
    if (!canRunLocalTools) {
      return const LocalActionResult(false, 'Git update is desktop-only.');
    }
    final system = bundledSystemDirectory();
    if (!await system.exists()) {
      return const LocalActionResult(false, 'No bundled local-system folder found.');
    }
    try {
      final gitDir = Directory('${system.path}${Platform.pathSeparator}.git');
      if (!await gitDir.exists()) {
        return _replaceBundledSystemFromGit(system);
      }
      final result = await Process.run(
        'git',
        ['pull', '--ff-only'],
        workingDirectory: system.path,
        environment: processEnvironment(),
      ).timeout(const Duration(minutes: 3));
      final output = [
        if ((result.stdout as String).trim().isNotEmpty)
          (result.stdout as String).trim(),
        if ((result.stderr as String).trim().isNotEmpty)
          (result.stderr as String).trim(),
      ].join('\n');
      return LocalActionResult(
        result.exitCode == 0,
        output.isEmpty ? 'Git exited with code ${result.exitCode}.' : output,
      );
    } catch (e) {
      return LocalActionResult(false, 'Could not update: $e');
    }
  }

  static Future<LocalActionResult> _replaceBundledSystemFromGit(
    Directory system,
  ) async {
    final parent = system.parent;
    final next = Directory(
      '${parent.path}${Platform.pathSeparator}local-system-next',
    );
    final backup = Directory(
      '${parent.path}${Platform.pathSeparator}local-system-backup',
    );
    if (await next.exists()) await next.delete(recursive: true);
    if (await backup.exists()) await backup.delete(recursive: true);

    final clone = await Process.run(
      'git',
      ['clone', '--depth=1', _repoUrl, next.path],
      workingDirectory: parent.path,
      environment: processEnvironment(),
    ).timeout(const Duration(minutes: 5));
    if (clone.exitCode != 0) {
      return LocalActionResult(
        false,
        'Could not download update:\n${clone.stderr}',
      );
    }

    await system.rename(backup.path);
    await next.rename(system.path);
    return const LocalActionResult(
      true,
      'Updated local-system from Git. Restart any running server window to use the new files.',
    );
  }
}
