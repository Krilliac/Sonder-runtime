import 'dart:convert';
import 'dart:io';

import 'launcher_health.dart';

import 'local_manager_models.dart';

class LocalManager {
  static const _repoUrl = 'https://github.com/Krilliac/Sonder-runtime.git';
  static const _serverLogName = 'sonder_serve.log';
  static const _logTailBytes = 64 * 1024;
  static const _managedOutputLineLimit = 60;
  static const _serverReadyTimeout = Duration(seconds: 25);
  static const _maxServerProbeInterval = Duration(seconds: 2);
  static Process? _managedServer;
  static int? _managedServerPid;
  static final List<String> _managedServerOutput = <String>[];

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
    final desktopSibling = Directory(
        '${appDirectory().path}${Platform.pathSeparator}local-system');
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
    final existing = Platform.environment['SONDER_HOME'];
    if (existing != null && existing.trim().isNotEmpty) {
      return existing;
    }
    if (Platform.isWindows) {
      final root = Platform.environment['LOCALAPPDATA'] ??
          Platform.environment['APPDATA'] ??
          Platform.environment['USERPROFILE'] ??
          appDirectory().path;
      return '$root${Platform.pathSeparator}sonder';
    }
    final xdg = Platform.environment['XDG_DATA_HOME'];
    if (xdg != null && xdg.trim().isNotEmpty) {
      return '$xdg${Platform.pathSeparator}sonder';
    }
    final home = Platform.environment['HOME'] ?? appDirectory().path;
    return '$home${Platform.pathSeparator}.local'
        '${Platform.pathSeparator}share'
        '${Platform.pathSeparator}sonder';
  }

  /// Mirror of `sonder_paths.default_home() / "run"` on the Python side.
  /// `sonder_headless.py` writes its supervised child logs here.
  static String runDirectoryPath() {
    return '${sharedHomePath()}${Platform.pathSeparator}run';
  }

  /// Absolute path of the API server startup log. This is where a launcher
  /// that dies before the API binds records the real cause, so every failed
  /// start must point the operator at it.
  static String serverLogPath() {
    return '${runDirectoryPath()}${Platform.pathSeparator}$_serverLogName';
  }

  static String launcherHealthTokenPath() {
    return '${runDirectoryPath()}${Platform.pathSeparator}'
        'sonder-launcher-health.token';
  }

  /// Last [maxLines] non-blank lines of the server startup log, or an empty
  /// string when the log is missing, empty, or unreadable.
  static Future<String> readServerLogTail({int maxLines = 40}) {
    return readLogTail(serverLogPath(), maxLines: maxLines);
  }

  static Future<String> readLogTail(String path, {int maxLines = 40}) async {
    try {
      final file = File(path);
      if (!await file.exists()) return '';
      final length = await file.length();
      final start = length > _logTailBytes ? length - _logTailBytes : 0;
      final bytes = <int>[];
      // The log is opened in binary append mode by the supervisor, so decode
      // leniently rather than throwing on a torn multi-byte sequence.
      await for (final chunk in file.openRead(start)) {
        bytes.addAll(chunk);
      }
      final lines = const LineSplitter()
          .convert(utf8.decode(bytes, allowMalformed: true))
          .map((line) => line.trimRight())
          .where((line) => line.trim().isNotEmpty)
          .toList();
      if (lines.isEmpty) return '';
      final tail = lines.length > maxLines
          ? lines.sublist(lines.length - maxLines)
          : lines;
      return tail.join('\n');
    } catch (_) {
      return '';
    }
  }

  /// Recent stdout/stderr of the app-managed launcher process. The direct
  /// (non-headless) launch path never reaches the supervisor's log file, so
  /// this is the only place its errors appear.
  static String managedServerOutputTail() => _managedServerOutput.join('\n');

  static Map<String, String> processEnvironment({
    bool allowHosted = false,
    String contextSize = '8192',
  }) {
    return {
      ...Platform.environment,
      'SONDER_HOME': sharedHomePath(),
      'SONDER_ALLOW_CLOUD': allowHosted ? '1' : '0',
      'SONDER_CONTEXT_SIZE':
          contextSize.trim().isEmpty ? '8192' : contextSize.trim(),
    };
  }

  static Future<bool> defaultServerReachable() async {
    final token = await _readLauncherHealthToken();
    if (token.length < 32) return false;
    final nonce = newLauncherHealthNonce();
    final client = HttpClient()..findProxy = (_) => 'DIRECT';
    client.connectionTimeout = const Duration(milliseconds: 350);
    try {
      final request = await client
          .getUrl(Uri.parse('http://127.0.0.1:11435$launcherHealthPath'))
          .timeout(const Duration(milliseconds: 800));
      request.headers.set(launcherHealthNonceHeader, nonce);
      final response = await request.close().timeout(
            const Duration(milliseconds: 800),
          );
      if (response.statusCode != HttpStatus.ok) return false;
      final bytes = <int>[];
      await for (final chunk in response.timeout(
        const Duration(milliseconds: 800),
      )) {
        bytes.addAll(chunk);
        if (bytes.length > 4096) return false;
      }
      final payload = jsonDecode(utf8.decode(bytes));
      // A TCP accept let unrelated or wedged listeners block startup and made
      // the UI promise a healthy server; require the launcher's signed identity.
      return launcherHealthPayloadMatches(
        payload,
        token: token,
        nonce: nonce,
        port: 11435,
      );
    } catch (_) {
      return false;
    } finally {
      client.close(force: true);
    }
  }

  /// Poll until the managed API proves its identity or [timeout] elapses.
  /// Starting a launcher process only proves the process spawned, so a start
  /// is not reported as successful until the signed health probe passes.
  static Future<bool> waitForServer({
    Duration timeout = _serverReadyTimeout,
    Duration interval = const Duration(milliseconds: 400),
    Future<bool> Function()? reachabilityProbe,
    Future<void> Function(Duration)? delay,
    DateTime Function()? clock,
  }) async {
    final now = clock ?? DateTime.now;
    final deadline = now().add(timeout);
    final probe = reachabilityProbe ?? defaultServerReachable;
    final wait = delay ?? Future<void>.delayed;
    var nextInterval = interval;
    var firstProbe = true;
    while (true) {
      if (!firstProbe && !now().isBefore(deadline)) return false;
      firstProbe = false;
      if (await probe()) return true;
      final remaining = deadline.difference(now());
      if (remaining <= Duration.zero) return false;
      await wait(nextInterval <= remaining ? nextInterval : remaining);
      nextInterval = _nextServerProbeInterval(nextInterval);
    }
  }

  /// Keep an unavailable signed-health endpoint from filling the local server
  /// log during startup, while still discovering a healthy launcher promptly.
  static Duration _nextServerProbeInterval(Duration current) {
    if (current <= Duration.zero) return Duration.zero;
    final currentMs = current.inMilliseconds;
    final maxMs = _maxServerProbeInterval.inMilliseconds;
    if (currentMs >= maxMs) return _maxServerProbeInterval;
    if (currentMs > maxMs ~/ 2) return _maxServerProbeInterval;
    return Duration(milliseconds: currentMs * 2);
  }

  static Future<LocalInstallInfo> inspect() async {
    final system = bundledSystemDirectory();
    final systemExists = await system.exists();
    Future<bool> hasFile(String name) async {
      return File('${system.path}${Platform.pathSeparator}$name').exists();
    }

    final gitDir = Directory('${system.path}${Platform.pathSeparator}.git');
    final gitCheckout = systemExists && await gitDir.exists();
    final serverScript = systemExists &&
        await hasFile(
            Platform.isWindows ? 'sonder-serve.cmd' : 'sonder-serve.sh');
    final trainingScript = systemExists &&
        await hasFile(
            Platform.isWindows ? 'endless-train.cmd' : 'endless-train.sh');
    final bootstrapScript = systemExists &&
        await hasFile(Platform.isWindows
            ? 'bootstrap-engine.cmd'
            : 'bootstrap-engine.sh');
    var engineBundle = false;
    final engineDirectory = Directory(
      '${system.path}${Platform.pathSeparator}engine',
    );
    if (systemExists && await engineDirectory.exists()) {
      await for (final entry in engineDirectory.list(followLinks: false)) {
        if (entry is! Directory) continue;
        final manifest = File(
          '${entry.path}${Platform.pathSeparator}ENGINE-BUNDLE.json',
        );
        if (await manifest.exists()) {
          engineBundle = true;
          break;
        }
      }
    }
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
      engineBundle: engineBundle,
      defaultServerReachable: reachable,
    );
  }

  static Future<LocalActionResult> setupEngine({
    bool allowHosted = false,
    String contextSize = '8192',
  }) async {
    if (!canRunLocalTools) {
      return const LocalActionResult(
          false, 'Host runtime setup is desktop-only.');
    }
    final system = bundledSystemDirectory();
    if (!await system.exists()) {
      return const LocalActionResult(
          false, 'No bundled local-system folder found.');
    }
    try {
      if (Platform.isWindows) {
        final script =
            File('${system.path}${Platform.pathSeparator}bootstrap-engine.cmd');
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
          return const LocalActionResult(true, 'Host runtime setup started.');
        }
      }
      final script = File(
        '${system.path}${Platform.pathSeparator}bootstrap-engine.sh',
      );
      if (!await script.exists()) {
        return const LocalActionResult(
          false,
          'The platform engine bootstrap launcher is missing.',
        );
      }
      await Process.start(
        '/bin/sh',
        [script.path],
        workingDirectory: system.path,
        environment: processEnvironment(
          allowHosted: allowHosted,
          contextSize: contextSize,
        ),
        mode: ProcessStartMode.detached,
      );
      return const LocalActionResult(true, 'Host runtime setup started.');
    } catch (e) {
      return LocalActionResult(false, 'Could not start host runtime setup: $e');
    }
  }

  static Future<LocalActionResult> startServer({
    bool allowHosted = false,
    String contextSize = '8192',
    bool persistOnAppClose = false,
    Duration readyTimeout = _serverReadyTimeout,
    Future<bool> Function()? managedReachabilityProbe,
    Future<bool> Function()? portOccupiedProbe,
  }) async {
    if (!canRunLocalTools) {
      return LocalActionResult(
        false,
        'Local process startup is not available on $platformLabel. Run the server on a desktop or LAN host.',
      );
    }
    final system = bundledSystemDirectory();
    final managedReachable = managedReachabilityProbe ?? defaultServerReachable;
    if (await managedReachable()) {
      return const LocalActionResult(
        true,
        'A server is already reachable on 127.0.0.1:11435.',
      );
    }
    final portOccupied = portOccupiedProbe ?? _defaultServerPortOccupied;
    if (await portOccupied()) {
      return const LocalActionResult(
        false,
        'A service is already listening on 127.0.0.1:11435, but it is not '
        'verified as this app-managed Sonder server. Stop that service '
        'before starting a managed local server.',
      );
    }
    if (!await system.exists()) {
      return const LocalActionResult(
        false,
        'No bundled local-system folder found next to the app.',
      );
    }
    _managedServerOutput.clear();
    try {
      final serverEnvironment = await _managedServerEnvironment(
        allowHosted: allowHosted,
        contextSize: contextSize,
      );
      if (Platform.isWindows) {
        final script = File(
          '${system.path}${Platform.pathSeparator}sonder-serve.cmd',
        );
        if (await script.exists()) {
          if (persistOnAppClose) {
            return await _startHeadlessServer(
              system,
              allowHosted: allowHosted,
              contextSize: contextSize,
              readyTimeout: readyTimeout,
            );
          }
          final process = await Process.start(
            'cmd.exe',
            ['/c', script.path],
            workingDirectory: system.path,
            environment: serverEnvironment,
          );
          _trackManagedServer(process);
          return await _awaitServerReady(
            'Server startup requested. Managed PID ${process.pid}.',
            readyTimeout,
          );
        }
      }
      if (persistOnAppClose) {
        return await _startHeadlessServer(
          system,
          allowHosted: allowHosted,
          contextSize: contextSize,
          readyTimeout: readyTimeout,
        );
      }
      final script = File(
        '${system.path}${Platform.pathSeparator}sonder-serve.sh',
      );
      if (!await script.exists()) {
        return const LocalActionResult(false, 'Server launcher is missing.');
      }
      final process = await Process.start(
        '/bin/sh',
        [script.path],
        workingDirectory: system.path,
        environment: serverEnvironment,
      );
      _trackManagedServer(process);
      return await _awaitServerReady(
        'Server startup requested. Managed PID ${process.pid}.',
        readyTimeout,
      );
    } catch (e) {
      return _serverStartFailure('Could not start server: $e');
    }
  }

  /// Detect a listener only to avoid competing with it. This is deliberately
  /// separate from [defaultServerReachable], which is the signed proof needed
  /// before the app trusts or manages the service.
  static Future<bool> _defaultServerPortOccupied() async {
    Socket? socket;
    try {
      socket = await Socket.connect(
        InternetAddress.loopbackIPv4,
        11435,
        timeout: const Duration(milliseconds: 350),
      );
      return true;
    } on SocketException {
      return false;
    } finally {
      socket?.destroy();
    }
  }

  /// Wait for the launched server to answer on its port and turn the outcome
  /// into a result the UI can act on. A launcher that spawned but never bound
  /// the port is a failure, not a success.
  static Future<LocalActionResult> _awaitServerReady(
    String startedMessage,
    Duration timeout,
  ) async {
    if (await waitForServer(timeout: timeout)) {
      return LocalActionResult(
        true,
        '$startedMessage\nServer is reachable on 127.0.0.1:11435.',
      );
    }
    return _serverStartFailure(
      '$startedMessage\nThe server did not become reachable on '
      '127.0.0.1:11435 within ${timeout.inSeconds}s.',
    );
  }

  /// Build a failing result that carries the evidence: the launcher's own
  /// output (direct launch) and the tail of the supervisor log (headless
  /// launch). Either one is where a packaging or interpreter fault shows up.
  static Future<LocalActionResult> _serverStartFailure(String summary) async {
    final logPath = serverLogPath();
    final logTail = await readServerLogTail();
    final processTail = managedServerOutputTail();
    final details = <String>[
      if (processTail.isNotEmpty) 'Launcher output:\n$processTail',
      if (logTail.isNotEmpty) 'Last lines of $logPath:\n$logTail',
    ];
    final blocks = <String>[
      summary,
      'Startup log: $logPath',
      if (details.isEmpty)
        'The launcher printed nothing and the startup log is missing or '
            'empty. Open the path above after the next attempt.',
    ];
    return LocalActionResult(
      false,
      blocks.join('\n\n'),
      logPath: logPath,
      logTail: details.join('\n\n'),
    );
  }

  static Future<LocalActionResult> _startHeadlessServer(
    Directory system, {
    required bool allowHosted,
    required String contextSize,
    required Duration readyTimeout,
  }) async {
    final serverEnvironment = await _managedServerEnvironment(
      allowHosted: allowHosted,
      contextSize: contextSize,
    );
    final args = <String>[
      'start',
      '--context-size',
      contextSize.trim().isEmpty ? '8192' : contextSize.trim(),
    ];
    if (Platform.isWindows) {
      final script = File(
        '${system.path}${Platform.pathSeparator}sonder-headless.cmd',
      );
      if (!await script.exists()) {
        return const LocalActionResult(
            false, 'Headless supervisor is missing.');
      }
      await Process.start(
        'cmd.exe',
        ['/c', 'start', '', '/min', script.path, ...args],
        workingDirectory: system.path,
        environment: serverEnvironment,
        runInShell: true,
      );
    } else {
      final script = File(
        '${system.path}${Platform.pathSeparator}sonder-headless.sh',
      );
      if (!await script.exists()) {
        return const LocalActionResult(
            false, 'Headless supervisor is missing.');
      }
      await Process.start(
        '/bin/sh',
        [script.path, ...args],
        workingDirectory: system.path,
        environment: serverEnvironment,
        mode: ProcessStartMode.detached,
      );
    }
    return _awaitServerReady(
      'Server startup requested in managed background mode.',
      readyTimeout,
    );
  }

  static Future<Map<String, String>> _managedServerEnvironment({
    required bool allowHosted,
    required String contextSize,
  }) async {
    final token = await _ensureLauncherHealthToken();
    return {
      ...processEnvironment(
        allowHosted: allowHosted,
        contextSize: contextSize,
      ),
      launcherHealthTokenEnvironment: token,
      launcherHealthRoleEnvironment: launcherHealthManagedRole,
    };
  }

  static Future<String> _readLauncherHealthToken() async {
    final configured =
        Platform.environment[launcherHealthTokenEnvironment]?.trim() ?? '';
    if (configured.length >= 32) return configured;
    try {
      final token =
          (await File(launcherHealthTokenPath()).readAsString()).trim();
      return token.length >= 32 ? token : '';
    } catch (_) {
      return '';
    }
  }

  static Future<String> _ensureLauncherHealthToken() async {
    final existing = await _readLauncherHealthToken();
    if (existing.isNotEmpty) return existing;
    final target = File(launcherHealthTokenPath());
    await target.parent.create(recursive: true);
    final temporary = File(
      '${target.path}.$pid.${DateTime.now().microsecondsSinceEpoch}.tmp',
    );
    final token = newLauncherHealthToken();
    try {
      await temporary.writeAsString(token, flush: true);
      if (!Platform.isWindows) {
        final chmod = await Process.run('chmod', ['600', temporary.path]);
        if (chmod.exitCode != 0) {
          throw FileSystemException(
            'Could not protect launcher health token',
            temporary.path,
          );
        }
      }
      final raced = await _readLauncherHealthToken();
      if (raced.isNotEmpty) return raced;
      await temporary.rename(target.path);
      return token;
    } finally {
      try {
        if (await temporary.exists()) await temporary.delete();
      } catch (_) {}
    }
  }

  static void _trackManagedServer(Process process) {
    _managedServer = process;
    _managedServerPid = process.pid;
    _captureManagedOutput(process.stdout);
    _captureManagedOutput(process.stderr);
    process.exitCode.then((code) {
      if (_managedServerPid == process.pid) {
        _managedServer = null;
        _managedServerPid = null;
        _recordManagedOutput('[launcher exited with code $code]');
      }
    });
  }

  static void _captureManagedOutput(Stream<List<int>> stream) {
    stream
        .transform(const Utf8Decoder(allowMalformed: true))
        .transform(const LineSplitter())
        .listen(_recordManagedOutput, onError: (_) {});
  }

  static void _recordManagedOutput(String line) {
    final text = line.trim();
    if (text.isEmpty) return;
    _managedServerOutput.add(text);
    if (_managedServerOutput.length > _managedOutputLineLimit) {
      _managedServerOutput.removeRange(
        0,
        _managedServerOutput.length - _managedOutputLineLimit,
      );
    }
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
      final headlessResult =
          await _stopHeadlessServer(bundledSystemDirectory());
      final results = <LocalActionResult>[
        if (managedResult != null) managedResult,
        if (headlessResult != null) headlessResult,
      ];
      if (results.isEmpty) {
        return const LocalActionResult(
            true, 'No app-managed server was found.');
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
      return LocalActionResult(
          stopped, 'Stop requested for app-managed PID $pid.');
    } finally {
      if (_managedServerPid == pid) {
        _managedServer = null;
        _managedServerPid = null;
      }
    }
  }

  static Future<LocalActionResult?> _stopHeadlessServer(
      Directory system) async {
    final windows = Platform.isWindows;
    final script = File(
      '${system.path}${Platform.pathSeparator}'
      '${windows ? 'sonder-headless.cmd' : 'sonder-headless.sh'}',
    );
    if (!await script.exists()) return null;
    final result = await Process.run(
      windows ? 'cmd.exe' : '/bin/sh',
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
      return const LocalActionResult(
          false, 'Grounded practice launcher is desktop-only.');
    }
    final system = bundledSystemDirectory();
    if (!await system.exists()) {
      return const LocalActionResult(
          false, 'No bundled local-system folder found.');
    }
    try {
      if (Platform.isWindows) {
        final script =
            File('${system.path}${Platform.pathSeparator}endless-train.cmd');
        if (await script.exists()) {
          await Process.start(
            'cmd.exe',
            ['/c', 'start', '', script.path],
            workingDirectory: system.path,
            environment: processEnvironment(),
            runInShell: true,
          );
          return const LocalActionResult(true, 'Grounded practice started.');
        }
      }
      final script = File(
        '${system.path}${Platform.pathSeparator}endless-train.sh',
      );
      if (!await script.exists()) {
        return const LocalActionResult(
            false, 'Grounded practice launcher is missing.');
      }
      await Process.start(
        '/bin/sh',
        [script.path],
        workingDirectory: system.path,
        environment: processEnvironment(),
        mode: ProcessStartMode.detached,
      );
      return const LocalActionResult(true, 'Grounded practice started.');
    } catch (e) {
      return LocalActionResult(false, 'Could not start grounded practice: $e');
    }
  }

  static Future<LocalActionResult> updateFromGit() async {
    if (!canRunLocalTools) {
      return const LocalActionResult(false, 'Git update is desktop-only.');
    }
    final system = bundledSystemDirectory();
    if (!await system.exists()) {
      return const LocalActionResult(
          false, 'No bundled local-system folder found.');
    }
    try {
      final gitDir = Directory('${system.path}${Platform.pathSeparator}.git');
      if (!await gitDir.exists()) {
        return await _replaceBundledSystemFromGit(system);
      }
      final safeUpdater =
          File('${system.path}${Platform.pathSeparator}safe_update.py');
      final safeUpdaterCmd =
          File('${system.path}${Platform.pathSeparator}sonder-safe-update.cmd');
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

  /// Environment names never handed to the Observatory process: it asks for
  /// its own credentials, so none of the app's may leak into it. Desktop
  /// session variables (DBUS_SESSION_BUS_ADDRESS, XAUTHORITY) are kept: a
  /// GUI process needs them.
  static final RegExp _secretEnvName = RegExp(
      r'(TOKEN|API_?KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)',
      caseSensitive: false);

  /// [environment] without any variable whose name looks like a credential.
  static Map<String, String> observatoryEnvironment(
          Map<String, String> environment) =>
      {
        for (final entry in environment.entries)
          if (!_secretEnvName.hasMatch(entry.key)) entry.key: entry.value,
      };

  static Future<void> _startDetached(String executable, List<String> arguments,
      Map<String, String> environment) async {
    await Process.start(
      executable,
      arguments,
      environment: environment,
      includeParentEnvironment: false,
      mode: ProcessStartMode.detached,
    );
  }

  /// Opens [url] with the OS opener: `xdg-open`, `open`, or `cmd /c start`.
  static Future<bool> _openUrl(
      String url, Map<String, String> environment) async {
    final String program;
    final List<String> arguments;
    if (Platform.isWindows) {
      // cmd parses the command line itself: escape its metacharacters so
      // the `&` between query parameters is not a command separator.
      final escaped = url.replaceAllMapped(
          RegExp(r'[&|<>^()]'), (match) => '^${match.group(0)}');
      program = 'cmd.exe';
      arguments = ['/c', 'start', '', escaped];
    } else if (Platform.isMacOS) {
      program = 'open';
      arguments = [url];
    } else {
      program = 'xdg-open';
      arguments = [url];
    }
    try {
      await Process.start(
        program,
        arguments,
        environment: environment,
        includeParentEnvironment: false,
        mode: ProcessStartMode.detached,
      );
      return true;
    } on ProcessException {
      return false;
    }
  }

  static bool _isFile(String path) => File(path).existsSync();

  /// The first `PATH` entry holding [name] (with a `PATHEXT` suffix on
  /// Windows), or null.
  static String? findExecutableOnPath(
      String name, Map<String, String> environment) {
    final path = environment['PATH'] ?? environment['Path'] ?? '';
    final separator = Platform.isWindows ? ';' : ':';
    final suffixes = Platform.isWindows
        ? [
            '',
            ...(environment['PATHEXT'] ?? '.EXE;.CMD;.BAT')
                .split(';')
                .where((suffix) => suffix.isNotEmpty),
          ]
        : const [''];
    for (final directory in path.split(separator)) {
      if (directory.trim().isEmpty) continue;
      for (final suffix in suffixes) {
        final candidate =
            '$directory${Platform.pathSeparator}$name${suffix.toLowerCase()}';
        if (_isFile(candidate)) return candidate;
      }
    }
    return null;
  }

  /// Opens Sonder Observatory connected to [connectUrls] (contract
  /// section 10).
  ///
  /// The executable comes from, in order: [executable] (the Settings value),
  /// the `SONDER_OBSERVATORY_BIN` environment variable, then
  /// `sonder-observatory` on `PATH`. It is started detached with one
  /// `--connect <url>` per URL. Without an executable, a configured
  /// [webUrl] is opened as `<webUrl>?fixture=0&connect=<url>&…` with the OS
  /// opener; otherwise the result carries guidance.
  ///
  /// Launching is disabled when [runtimeUrl] is not loopback: producer
  /// telemetry is loopback on the runtime host. No token or API key is ever
  /// passed, in arguments, URL or environment.
  ///
  /// [environment], [start], [fileExists], [findOnPath] and [open] are
  /// injectable for tests; they default to this process's environment and
  /// the real process, file and opener calls.
  static Future<ObservatoryLaunchResult> launchObservatory(
    List<String> connectUrls, {
    String runtimeUrl = '',
    String executable = '',
    String webUrl = '',
    Map<String, String>? environment,
    Future<void> Function(String executable, List<String> arguments,
            Map<String, String> environment)?
        start,
    bool Function(String path)? fileExists,
    String? Function(String name, Map<String, String> environment)? findOnPath,
    Future<bool> Function(String url, Map<String, String> environment)? open,
  }) async {
    final urls = observatoryConnectUrls(connectUrls);
    final blocked =
        observatoryLaunchBlocked(runtimeUrl: runtimeUrl, connectUrls: urls);
    if (blocked != null) return blocked;
    if (!canRunLocalTools) {
      // Android and iOS cannot start the desktop Observatory: offer the
      // link, as a browser build does.
      final url = observatoryWebLaunchUrl(webUrl, urls) ?? '';
      return ObservatoryLaunchResult(
        ok: false,
        mode: ObservatoryLaunchMode.unavailable,
        message: url.isEmpty
            ? 'This device cannot start the Observatory. Copy the connect '
                'URLs and open them in the Observatory on the runtime host.'
            : 'This device cannot start the Observatory. Copy this link.',
        url: url,
      );
    }
    final env = environment ?? Platform.environment;
    final childEnv = observatoryEnvironment(env);
    final exists = fileExists ?? _isFile;

    String resolved = '';
    final configured = executable.trim();
    final fromEnv = (env[observatoryBinEnv] ?? '').trim();
    if (configured.isNotEmpty) {
      if (!exists(configured)) {
        return ObservatoryLaunchResult(
          ok: false,
          mode: ObservatoryLaunchMode.unavailable,
          message: 'The Observatory executable set in Settings was not '
              'found: $configured',
        );
      }
      resolved = configured;
    } else if (fromEnv.isNotEmpty) {
      if (!exists(fromEnv)) {
        return ObservatoryLaunchResult(
          ok: false,
          mode: ObservatoryLaunchMode.unavailable,
          message: '$observatoryBinEnv names an executable that was not '
              'found: $fromEnv',
        );
      }
      resolved = fromEnv;
    } else {
      resolved = (findOnPath ?? findExecutableOnPath)(
              observatoryExecutableName, env) ??
          '';
    }

    if (resolved.isNotEmpty) {
      final arguments = [
        for (final url in urls) ...['--connect', url],
      ];
      try {
        await (start ?? _startDetached)(resolved, arguments, childEnv);
      } on ProcessException catch (error) {
        return ObservatoryLaunchResult(
          ok: false,
          mode: ObservatoryLaunchMode.executable,
          message: 'Could not start the Observatory: ${error.message}',
          executable: resolved,
          arguments: arguments,
        );
      }
      return ObservatoryLaunchResult(
        ok: true,
        mode: ObservatoryLaunchMode.executable,
        message: 'Opened the Observatory with ${urls.length} '
            '${urls.length == 1 ? 'producer' : 'producers'}.',
        executable: resolved,
        arguments: arguments,
      );
    }

    final url = observatoryWebLaunchUrl(webUrl, urls);
    if (url != null) {
      final opened = await (open ?? _openUrl)(url, childEnv);
      return ObservatoryLaunchResult(
        ok: opened,
        mode: opened
            ? ObservatoryLaunchMode.webUrl
            : ObservatoryLaunchMode.unavailable,
        message: opened
            ? 'Opened the Observatory in the browser.'
            : 'Could not open a browser. Copy this link instead.',
        url: url,
      );
    }
    return const ObservatoryLaunchResult(
      ok: false,
      mode: ObservatoryLaunchMode.unavailable,
      message: observatoryGuidance,
    );
  }
}
