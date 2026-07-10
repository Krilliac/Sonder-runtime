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
  final bool bootstrapScript;
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
    required this.defaultServerReachable,
  });
}

class LocalManager {
  static const _repoUrl = 'https://github.com/Krilliac/Trilobite-model.git';
  static Process? _managedServer;
  static int? _managedServerPid;

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

  static Map<String, String> processEnvironment({
    bool allowHosted = false,
    String contextSize = '8192',
  }) {
    return {
      ...Platform.environment,
      'TRILOBITE_HOME': sharedHomePath(),
      'TRILOBITE_ALLOW_CLOUD': allowHosted ? '1' : '0',
      'TRILOBITE_CONTEXT_SIZE': contextSize.trim().isEmpty ? '8192' : contextSize.trim(),
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
    final bootstrapScript = systemExists &&
        (await hasFile('bootstrap-engine.cmd') ||
            await hasFile('bootstrap_engine.py'));
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
      bootstrapScript: bootstrapScript,
      defaultServerReachable: reachable,
    );
  }

  static Future<LocalActionResult> setupEngine({
    bool allowHosted = false,
    String contextSize = '8192',
  }) async {
    if (!canRunLocalTools) {
      return const LocalActionResult(false, 'Engine setup is desktop-only.');
    }
    final system = bundledSystemDirectory();
    if (!await system.exists()) {
      return const LocalActionResult(false, 'No bundled local-system folder found.');
    }
    try {
      if (Platform.isWindows) {
        final script = File('${system.path}${Platform.pathSeparator}bootstrap-engine.cmd');
        if (await script.exists()) {
          await Process.start(
            'cmd.exe',
            ['/c', 'start', '', script.path],
            workingDirectory: system.path,
            environment: processEnvironment(
              allowHosted: allowHosted,
              contextSize: contextSize,
            ),
            runInShell: true,
          );
          return const LocalActionResult(true, 'Engine setup started.');
        }
      }
      await Process.start(
        Platform.isWindows ? 'python.exe' : 'python3',
        ['bootstrap_engine.py'],
        workingDirectory: system.path,
        environment: processEnvironment(
          allowHosted: allowHosted,
          contextSize: contextSize,
        ),
        mode: ProcessStartMode.detached,
      );
      return const LocalActionResult(true, 'Engine setup started.');
    } catch (e) {
      return LocalActionResult(false, 'Could not start engine setup: $e');
    }
  }

  static Future<LocalActionResult> startServer({
    bool allowHosted = false,
    String contextSize = '8192',
    bool persistOnAppClose = false,
  }) async {
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
        final script = File(
          '${system.path}${Platform.pathSeparator}trilobite-serve.cmd',
        );
        if (await script.exists()) {
          if (persistOnAppClose) {
            return _startHeadlessServer(
              system,
              allowHosted: allowHosted,
              contextSize: contextSize,
            );
          }
          final process = await Process.start(
            'cmd.exe',
            ['/c', script.path],
            workingDirectory: system.path,
            environment: processEnvironment(
              allowHosted: allowHosted,
              contextSize: contextSize,
            ),
          );
          _trackManagedServer(process);
          return LocalActionResult(
            true,
            'Server startup requested. Managed PID ${process.pid}.',
          );
        }
      }
      final python = Platform.isWindows ? 'python.exe' : 'python3';
      if (persistOnAppClose) {
        return _startHeadlessServer(
          system,
          allowHosted: allowHosted,
          contextSize: contextSize,
        );
      }
      final process = await Process.start(
        python,
        ['trilobite_serve.py'],
        workingDirectory: system.path,
        environment: processEnvironment(
          allowHosted: allowHosted,
          contextSize: contextSize,
        ),
      );
      _trackManagedServer(process);
      return LocalActionResult(
        true,
        'Server startup requested. Managed PID ${process.pid}.',
      );
    } catch (e) {
      return LocalActionResult(false, 'Could not start server: $e');
    }
  }

  static Future<LocalActionResult> _startHeadlessServer(
    Directory system, {
    required bool allowHosted,
    required String contextSize,
  }) async {
    final args = <String>[
      'start',
      '--context-size',
      contextSize.trim().isEmpty ? '8192' : contextSize.trim(),
    ];
    if (Platform.isWindows) {
      final script = File(
        '${system.path}${Platform.pathSeparator}trilobite-headless.cmd',
      );
      if (!await script.exists()) {
        return const LocalActionResult(false, 'Headless supervisor is missing.');
      }
      await Process.start(
        'cmd.exe',
        ['/c', 'start', '', '/min', script.path, ...args],
        workingDirectory: system.path,
        environment: processEnvironment(
          allowHosted: allowHosted,
          contextSize: contextSize,
        ),
        runInShell: true,
      );
    } else {
      final script = File(
        '${system.path}${Platform.pathSeparator}trilobite_headless.py',
      );
      if (!await script.exists()) {
        return const LocalActionResult(false, 'Headless supervisor is missing.');
      }
      await Process.start(
        'python3',
        [script.path, ...args],
        workingDirectory: system.path,
        environment: processEnvironment(
          allowHosted: allowHosted,
          contextSize: contextSize,
        ),
        mode: ProcessStartMode.detached,
      );
    }
    return const LocalActionResult(
      true,
      'Server startup requested in managed background mode.',
    );
  }

  static void _trackManagedServer(Process process) {
    _managedServer = process;
    _managedServerPid = process.pid;
    process.stdout.listen((_) {}, onError: (_) {});
    process.stderr.listen((_) {}, onError: (_) {});
    process.exitCode.then((_) {
      if (_managedServerPid == process.pid) {
        _managedServer = null;
        _managedServerPid = null;
      }
    });
  }

  static void stopManagedServerNow() {
    final process = _managedServer;
    if (process != null) {
      process.kill(ProcessSignal.sigterm);
      process.kill(ProcessSignal.sigkill);
    }
    _managedServer = null;
    _managedServerPid = null;
  }

  static Future<LocalActionResult> stopServers() async {
    if (!canRunLocalTools) {
      return const LocalActionResult(false, 'Server shutdown is desktop-only.');
    }
    final managedResult = await _stopTrackedServer();
    try {
      final headlessResult = await _stopHeadlessServer(bundledSystemDirectory());
      final results = <LocalActionResult>[
        if (managedResult != null) managedResult,
        if (headlessResult != null) headlessResult,
      ];
      if (results.isEmpty) {
        return const LocalActionResult(true, 'No app-managed server was found.');
      }
      return LocalActionResult(
        results.every((result) => result.ok),
        results.map((result) => result.message).join('\n'),
      );
    } catch (e) {
      return LocalActionResult(false, 'Could not stop managed servers: $e');
    }
  }

  static Future<LocalActionResult?> _stopTrackedServer() async {
    final process = _managedServer;
    final pid = _managedServerPid;
    if (process == null || pid == null) return null;
    try {
      if (Platform.isWindows) {
        final result = await Process.run(
          'taskkill',
          ['/PID', '$pid', '/T', '/F'],
          environment: processEnvironment(),
        ).timeout(const Duration(seconds: 20));
        final output = _processOutput(result);
        return LocalActionResult(
          result.exitCode == 0,
          output.isEmpty ? 'Stopped app-managed PID $pid.' : output,
        );
      }
      final stopped = process.kill(ProcessSignal.sigterm);
      return LocalActionResult(stopped, 'Stop requested for app-managed PID $pid.');
    } finally {
      if (_managedServerPid == pid) {
        _managedServer = null;
        _managedServerPid = null;
      }
    }
  }

  static Future<LocalActionResult?> _stopHeadlessServer(Directory system) async {
    final windows = Platform.isWindows;
    final script = File(
      '${system.path}${Platform.pathSeparator}'
      '${windows ? 'trilobite-headless.cmd' : 'trilobite_headless.py'}',
    );
    if (!await script.exists()) return null;
    final result = await Process.run(
      windows ? 'cmd.exe' : 'python3',
      windows ? ['/c', script.path, 'stop'] : [script.path, 'stop'],
      workingDirectory: system.path,
      environment: processEnvironment(),
    ).timeout(const Duration(seconds: 20));
    final output = _processOutput(result);
    return LocalActionResult(
      result.exitCode == 0,
      output.isEmpty ? 'Headless stop command exited.' : output,
    );
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
      final safeUpdater =
          File('${system.path}${Platform.pathSeparator}safe_update.py');
      final safeUpdaterCmd =
          File('${system.path}${Platform.pathSeparator}trilobite-safe-update.cmd');
      if (Platform.isWindows && await safeUpdaterCmd.exists()) {
        final safe = await Process.run(
          'cmd.exe',
          ['/c', safeUpdaterCmd.path],
          workingDirectory: system.path,
          environment: processEnvironment(),
        ).timeout(const Duration(minutes: 8));
        final output = _processOutput(safe);
        return LocalActionResult(
          safe.exitCode == 0,
          output.isEmpty
              ? 'Safe updater exited with code ${safe.exitCode}.'
              : output,
        );
      }
      if (await safeUpdater.exists()) {
        final safe = await Process.run(
          Platform.isWindows ? 'python.exe' : 'python3',
          [safeUpdater.path, '--repo', system.path],
          workingDirectory: system.path,
          environment: processEnvironment(),
        ).timeout(const Duration(minutes: 8));
        final output = _processOutput(safe);
        return LocalActionResult(
          safe.exitCode == 0,
          output.isEmpty
              ? 'Safe updater exited with code ${safe.exitCode}.'
              : output,
        );
      }
      final status = await _runGit(system, ['status', '--porcelain']);
      final hadLocalChanges = (status.stdout as String).trim().isNotEmpty;
      final result = await _runGit(
        system,
        ['pull', '--rebase', '--autostash'],
        timeout: const Duration(minutes: 5),
      );
      var output = _processOutput(result);
      if (result.exitCode != 0 && _looksLikeMissingUpstream(output)) {
        final fallback = await _runGit(
          system,
          ['pull', '--rebase', '--autostash', 'origin', 'main'],
          timeout: const Duration(minutes: 5),
        );
        output = _processOutput(fallback);
        return LocalActionResult(
          fallback.exitCode == 0,
          _gitUpdateMessage(output, fallback.exitCode, hadLocalChanges),
        );
      }
      return LocalActionResult(
        result.exitCode == 0,
        _gitUpdateMessage(output, result.exitCode, hadLocalChanges),
      );
    } catch (e) {
      return LocalActionResult(false, 'Could not update: $e');
    }
  }

  static Future<ProcessResult> _runGit(
    Directory system,
    List<String> args, {
    Duration timeout = const Duration(minutes: 3),
  }) {
    return Process.run(
      'git',
      args,
      workingDirectory: system.path,
      environment: processEnvironment(),
    ).timeout(timeout);
  }

  static String _processOutput(ProcessResult result) {
    return [
      if ((result.stdout as String).trim().isNotEmpty)
        (result.stdout as String).trim(),
      if ((result.stderr as String).trim().isNotEmpty)
        (result.stderr as String).trim(),
    ].join('\n');
  }

  static bool _looksLikeMissingUpstream(String output) {
    final text = output.toLowerCase();
    return text.contains('no tracking information') ||
        text.contains('no upstream branch') ||
        text.contains('there is no tracking information');
  }

  static String _gitUpdateMessage(
    String output,
    int exitCode,
    bool hadLocalChanges,
  ) {
    final lines = <String>[
      if (hadLocalChanges)
        'Local edits were temporarily saved while updating. If Git reports conflicts, open the bundled local-system folder and resolve them there.',
      if (output.trim().isNotEmpty) output.trim(),
      if (output.trim().isEmpty) 'Git exited with code $exitCode.',
    ];
    return lines.join('\n');
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
