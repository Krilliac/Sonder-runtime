import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data' show BytesBuilder;
import 'package:flutter/foundation.dart' show visibleForTesting;

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

  /// Registers an already-started launcher as the managed server. Test seam.
  @visibleForTesting
  static void trackManagedServerForTest(Process process) =>
      _trackManagedServer(process);

  /// Synchronous (the app is closing): the launcher is a shell, so the server
  /// is its child. Killing only the shell would orphan the Python server, so
  /// the whole tree goes: `taskkill /T /F` on Windows, the shell's children
  /// then the shell on Unix.
  static void stopManagedServerNow() {
    final process = _managedServer;
    if (process != null) {
      try {
        if (Platform.isWindows) {
          Process.runSync('taskkill', ['/PID', '${process.pid}', '/T', '/F']);
        } else {
          Process.runSync('pkill', ['-KILL', '-P', '${process.pid}']);
        }
      } catch (_) {}
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
        final safe = await runBoundedProcess(
          'cmd.exe',
          ['/c', safeUpdaterCmd.path],
          workingDirectory: system.path,
          environment: _gitEnvironment(),
          timeout: _safeUpdateTimeout,
        );
        if (safe.timedOut) {
          return LocalActionResult(
              false, updateTimeoutMessage(_safeUpdateTimeout));
        }
        final output = safe.output;
        return LocalActionResult(
          safe.exitCode == 0,
          output.isEmpty
              ? 'Safe updater exited with code ${safe.exitCode}.'
              : output,
        );
      }
      if (await safeUpdater.exists()) {
        final safe = await runBoundedProcess(
          Platform.isWindows ? 'python.exe' : 'python3',
          [safeUpdater.path, '--repo', system.path],
          workingDirectory: system.path,
          environment: _gitEnvironment(),
          timeout: _safeUpdateTimeout,
        );
        if (safe.timedOut) {
          return LocalActionResult(
              false, updateTimeoutMessage(_safeUpdateTimeout));
        }
        final output = safe.output;
        return LocalActionResult(
          safe.exitCode == 0,
          output.isEmpty
              ? 'Safe updater exited with code ${safe.exitCode}.'
              : output,
        );
      }
      final status = await _runGit(system, ['status', '--porcelain']);
      if (status.timedOut) {
        return LocalActionResult(
            false, updateTimeoutMessage(const Duration(minutes: 3)));
      }
      final hadLocalChanges = status.stdout.trim().isNotEmpty;
      final result = await _runGit(
        system,
        ['pull', '--rebase', '--autostash'],
        timeout: const Duration(minutes: 5),
      );
      if (result.timedOut) {
        return LocalActionResult(
            false, updateTimeoutMessage(const Duration(minutes: 5)));
      }
      var output = result.output;
      if (result.exitCode != 0 && _looksLikeMissingUpstream(output)) {
        final fallback = await _runGit(
          system,
          ['pull', '--rebase', '--autostash', 'origin', 'main'],
          timeout: const Duration(minutes: 5),
        );
        if (fallback.timedOut) {
          return LocalActionResult(
              false, updateTimeoutMessage(const Duration(minutes: 5)));
        }
        output = fallback.output;
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

  static const _safeUpdateTimeout = Duration(minutes: 8);

  static String _processOutput(ProcessResult result) {
    return [
      if ((result.stdout as String).trim().isNotEmpty)
        (result.stdout as String).trim(),
      if ((result.stderr as String).trim().isNotEmpty)
        (result.stderr as String).trim(),
    ].join('\n');
  }

  static Future<BoundedProcessResult> _runGit(
    Directory system,
    List<String> args, {
    Duration timeout = const Duration(minutes: 3),
  }) {
    return runBoundedProcess(
      'git',
      args,
      workingDirectory: system.path,
      environment: _gitEnvironment(),
      timeout: timeout,
    );
  }

  /// Git must never wait on a credential prompt nobody can see.
  static Map<String, String> _gitEnvironment() => {
        ...processEnvironment(),
        'GIT_TERMINAL_PROMPT': '0',
      };

  /// What an update that ran out of time says. The process tree was
  /// stopped; a safe updater or `pull --autostash` may have stashed local
  /// edits before it stalled, and only the stopped process knew that.
  static String updateTimeoutMessage(Duration timeout) =>
      'The update did not finish within ${timeout.inMinutes} minutes and was '
      'stopped. Local edits may have been saved in Git: run `git stash list` '
      'in the bundled local-system folder before updating again.';

  /// Runs [executable] with stdin closed, collecting its output. Past
  /// [timeout] the whole process tree is killed ([onKillTree], default
  /// [killProcessTree]) instead of being left running while the app moves
  /// on, so a retry cannot race an orphaned updater or clone.
  static Future<BoundedProcessResult> runBoundedProcess(
    String executable,
    List<String> args, {
    required String workingDirectory,
    Map<String, String>? environment,
    bool includeParentEnvironment = true,
    required Duration timeout,
    int? maxOutputChars,
    Future<void> Function(int pid)? onKillTree,
  }) async {
    // On Unix, give each bounded command its own session so a timeout can
    // signal its whole process group, including any git subprocesses. The
    // setsid shim needs python3; a machine without it (a first install) runs
    // the command directly and a timeout kills only that process.
    Process process;
    var grouped = false;
    if (Platform.isWindows) {
      process = await Process.start(
        executable,
        args,
        workingDirectory: workingDirectory,
        environment: environment,
        includeParentEnvironment: includeParentEnvironment,
      );
    } else {
      try {
        process = await Process.start(
          'python3',
          [
            '-c',
            'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])',
            executable,
            ...args,
          ],
          workingDirectory: workingDirectory,
          environment: environment,
          includeParentEnvironment: includeParentEnvironment,
        );
        grouped = true;
      } on ProcessException {
        process = await Process.start(
          executable,
          args,
          workingDirectory: workingDirectory,
          environment: environment,
          includeParentEnvironment: includeParentEnvironment,
        );
      }
    }
    try {
      await process.stdin.close();
    } catch (_) {}
    final stdout = StringBuffer();
    final stderr = StringBuffer();
    void appendBounded(StringBuffer buf, String chunk) {
      if (maxOutputChars == null) {
        buf.write(chunk);
        return;
      }
      final remaining = maxOutputChars - buf.length;
      if (remaining <= 0) return;
      if (chunk.length <= remaining) {
        buf.write(chunk);
      } else {
        buf.write(chunk.substring(0, remaining));
      }
    }

    const decoder = Utf8Decoder(allowMalformed: true);
    final outDone = process.stdout
        .transform(decoder)
        .forEach((chunk) => appendBounded(stdout, chunk))
        .catchError((Object _) {});
    final errDone = process.stderr
        .transform(decoder)
        .forEach((chunk) => appendBounded(stderr, chunk))
        .catchError((Object _) {});
    int exitCode;
    var timedOut = false;
    try {
      exitCode = await process.exitCode.timeout(timeout);
    } on TimeoutException {
      timedOut = true;
      try {
        await (onKillTree ??
            (Platform.isWindows || grouped
                ? killProcessTree
                : (int pid) async =>
                    Process.killPid(pid, ProcessSignal.sigkill)))(process.pid);
      } catch (_) {}
      process.kill(ProcessSignal.sigkill);
      exitCode = await process.exitCode
          .timeout(const Duration(seconds: 10), onTimeout: () => -1);
    }
    await Future.wait([outDone, errDone])
        .timeout(const Duration(seconds: 5), onTimeout: () => const []);
    return BoundedProcessResult(
      exitCode: exitCode,
      stdout: stdout.toString(),
      stderr: stderr.toString(),
      timedOut: timedOut,
    );
  }

  /// Kills [pid] and its children: `taskkill /T /F` on Windows; on Unix,
  /// [pid] is the process-group leader created by [runBoundedProcess].
  static Future<void> killProcessTree(int pid) async {
    if (Platform.isWindows) {
      await Process.run('taskkill', ['/T', '/F', '/PID', '$pid'])
          .timeout(const Duration(seconds: 10));
    } else {
      // A negative pid signals the whole group; fall back to the leader alone.
      if (!Process.killPid(-pid, ProcessSignal.sigkill)) {
        Process.killPid(pid, ProcessSignal.sigkill);
      }
    }
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

    final clone = await runBoundedProcess(
      'git',
      ['clone', '--depth=1', _repoUrl, next.path],
      workingDirectory: parent.path,
      environment: _gitEnvironment(),
      timeout: const Duration(minutes: 5),
    );
    if (clone.timedOut) {
      return const LocalActionResult(
        false,
        'Could not download update: the download did not finish within 5 '
        'minutes and was stopped.',
      );
    }
    if (clone.exitCode != 0) {
      return LocalActionResult(
        false,
        'Could not download update:\n${clone.stderr}',
      );
    }

    await swapInBundledSystem(system, next, backup);
    return const LocalActionResult(
      true,
      'Updated local-system from Git. Restart any running server window to use the new files.',
    );
  }

  /// Moves [next] into [system]'s place, keeping the live tree in [backup]
  /// until the new one is installed. If the second rename fails (Defender or
  /// the indexer holding the fresh clone), the live tree is renamed back,
  /// with brief retries for transient sharing violations, and the original
  /// error is rethrown. The backup is deleted only after the new tree is in
  /// place.
  static Future<void> swapInBundledSystem(
    Directory system,
    Directory next,
    Directory backup, {
    Future<void> Function(Directory from, String to)? rename,
  }) async {
    final move =
        rename ?? (Directory from, String to) async => await from.rename(to);
    await move(system, backup.path);
    try {
      await move(next, system.path);
    } catch (_) {
      Object? restoreError;
      for (var attempt = 0; attempt < 5; attempt++) {
        try {
          await move(backup, system.path);
          restoreError = null;
          break;
        } catch (e) {
          restoreError = e;
          await Future<void>.delayed(
              Duration(milliseconds: 200 * (attempt + 1)));
        }
      }
      if (restoreError != null) {
        throw FileSystemException(
            'The update could not be installed and the previous local-system '
            'could not be moved back. It is intact at ${backup.path}; rename '
            'it to ${system.path}.',
            backup.path);
      }
      rethrow;
    }
    try {
      await backup.delete(recursive: true);
    } catch (_) {
      // A leftover backup is removed before the next update.
    }
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

  /// Scrubbed environment for client-executed child processes (`run_code`,
  /// `run_program`, `test_run`): strips credential-named variables and
  /// `SONDER_*` runtime/auth variables so secrets never leak to subprocesses.
  static Map<String, String> clientProcessEnvironment(
          Map<String, String> environment) =>
      {
        for (final entry in environment.entries)
          if (!_secretEnvName.hasMatch(entry.key) &&
              !entry.key.toUpperCase().startsWith('SONDER_'))
            entry.key: entry.value,
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

  /// Opens [url] with the OS opener ([observatoryOpenerCommand]).
  static Future<bool> _openUrl(
      String url, Map<String, String> environment) async {
    final (program, arguments) = observatoryOpenerCommand(url,
        operatingSystem: Platform.operatingSystem);
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

  /// True when [path] can be launched as the Observatory on
  /// [operatingSystem]: an existing file, or on macOS an existing `.app`
  /// bundle directory.
  static bool observatoryPathExists(String path, {String? operatingSystem}) {
    if (_isFile(path)) return true;
    return (operatingSystem ?? Platform.operatingSystem) == 'macos' &&
        isMacAppBundle(path) &&
        Directory(path).existsSync();
  }

  /// The first `PATH` entry holding [name] (with a `PATHEXT` suffix on
  /// Windows), or null. [fileExists] and [operatingSystem] default to the
  /// real file system and host; tests inject them.
  static String? findExecutableOnPath(
      String name, Map<String, String> environment,
      {bool Function(String path)? fileExists, String? operatingSystem}) {
    final exists = fileExists ?? _isFile;
    final windows = (operatingSystem ?? Platform.operatingSystem) == 'windows';
    final path = environment['PATH'] ?? environment['Path'] ?? '';
    final separator = windows ? ';' : ':';
    final directorySeparator = windows ? r'\' : '/';
    final suffixes = windows
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
        final base = directory.endsWith(directorySeparator)
            ? directory
            : '$directory$directorySeparator';
        final candidate = '$base$name${suffix.toLowerCase()}';
        if (exists(candidate)) return candidate;
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
  /// On macOS a `.app` bundle path is accepted and started with
  /// `open -n -a <bundle> --args …` ([observatoryProcessCommand]).
  ///
  /// [environment], [start], [fileExists], [findOnPath], [open] and
  /// [operatingSystem] are injectable for tests; they default to this
  /// process's environment, the real process, file and opener calls, and
  /// the host OS.
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
    String? operatingSystem,
  }) async {
    final os = operatingSystem ?? Platform.operatingSystem;
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
    final exists = fileExists ??
        (String path) => observatoryPathExists(path, operatingSystem: os);

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
      resolved = (findOnPath ??
              (name, environment) => findExecutableOnPath(name, environment,
                  fileExists: exists,
                  operatingSystem: os))(observatoryExecutableName, env) ??
          '';
    }

    if (resolved.isNotEmpty) {
      final arguments = [
        for (final url in urls) ...['--connect', url],
      ];
      final (program, programArguments) =
          observatoryProcessCommand(resolved, arguments, operatingSystem: os);
      try {
        await (start ?? _startDetached)(program, programArguments, childEnv);
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

  static const _maxClientOutputChars = 65536;
  static const _maxClientErrorChars = 4096;
  static const _maxClientReadBytes = 2 * 1024 * 1024;
  static const _maxClientProcessOutputChars = 131072;

  static String _boundClientText(String text, int limit) {
    if (text.length <= limit) return text;
    return '${text.substring(0, limit - 16)}\n...[truncated]';
  }

  static final RegExp _driveRelativePattern = RegExp(r'^[A-Za-z]:(?:$|[^/\\])');
  static final RegExp _localExtendedDrivePattern =
      RegExp(r'^\\\\\?\\[A-Za-z]:(?:$|[/\\])');
  static final RegExp _windowsReservedDeviceName = RegExp(
    r'^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$',
    caseSensitive: false,
  );

  static bool _isWindowsReservedDeviceSegment(String segment) {
    final canon = _canonicalClientComponent(segment);
    if (canon.isEmpty) return false;
    final dot = canon.indexOf('.');
    final stem = dot >= 0 ? canon.substring(0, dot) : canon;
    return _windowsReservedDeviceName.hasMatch(stem);
  }

  /// Lexically refuse UNC, device, extended, NT-namespace, control-char, and
  /// Windows drive-relative paths BEFORE any filesystem or symlink call.
  static void _rejectUnsafeClientPathSpelling(
    String raw, {
    bool allowLocalExtended = false,
  }) {
    for (var i = 0; i < raw.length; i++) {
      final code = raw.codeUnitAt(i);
      if (code < 32 || code == 127) {
        throw FileSystemException(
            'Path contains disallowed control characters', raw);
      }
    }
    var text = raw.trim();
    if (text.isEmpty) return;
    if (allowLocalExtended && _localExtendedDrivePattern.hasMatch(text)) {
      text = text.substring(4);
    }
    if (text.startsWith(r'\\') ||
        text.startsWith('//') ||
        text.startsWith(r'\/') ||
        text.startsWith(r'/\')) {
      throw FileSystemException(
          'UNC, device, or extended path is not allowed', raw);
    }
    if (text == r'\??' ||
        text == '/??' ||
        (text.length > 3 &&
            (text.startsWith(r'\??') || text.startsWith('/??')) &&
            (text[3] == r'\' || text[3] == '/'))) {
      throw FileSystemException('NT namespace path is not allowed', raw);
    }
    if (_driveRelativePattern.hasMatch(text)) {
      throw FileSystemException(
          'Windows drive-relative path is not allowed', raw);
    }
    for (final seg in text.split(RegExp(r'[/\\]'))) {
      if (seg.isEmpty) continue;
      if (Platform.isWindows && RegExp(r'^[A-Za-z]:$').hasMatch(seg)) continue;
      if (_isWindowsReservedDeviceSegment(seg)) {
        throw FileSystemException(
            'Windows reserved device name is not allowed', raw);
      }
    }
  }

  static bool _isAbsolutePath(String p) {
    if (p.isEmpty) return false;
    if (Platform.isWindows) {
      return RegExp(r'^[A-Za-z]:[/\\]').hasMatch(p) || p.startsWith(r'\\');
    }
    return p.startsWith('/');
  }

  static String _normalizeSegments(String path) {
    final sep = Platform.pathSeparator;
    final parts = path.split(RegExp(r'[/\\]+'));
    String prefix = '';
    var start = 0;
    if (Platform.isWindows && parts.isNotEmpty && RegExp(r'^[A-Za-z]:$').hasMatch(parts[0])) {
      prefix = '${parts[0]}$sep';
      start = 1;
    } else if (path.startsWith('/')) {
      prefix = '/';
      start = 1;
    }
    final stack = <String>[];
    for (var i = start; i < parts.length; i++) {
      final seg = parts[i];
      if (seg.isEmpty || seg == '.') continue;
      if (seg == '..') {
        if (stack.isNotEmpty) stack.removeLast();
      } else {
        stack.add(seg);
      }
    }
    if (prefix.isEmpty) return stack.join(sep);
    if (stack.isEmpty) return prefix;
    return '$prefix${stack.join(sep)}';
  }

  static void _rejectUnsafeLinkAncestors(String normalized) {
    final sep = Platform.pathSeparator;
    final parts = normalized.split(sep);
    if (parts.length <= 1) return;
    var cur = parts[0];
    if (Platform.isWindows && RegExp(r'^[A-Za-z]:$').hasMatch(cur)) {
      cur = '$cur$sep';
    } else if (cur.isEmpty && normalized.startsWith(sep)) {
      cur = sep;
    }
    for (var i = 1; i < parts.length; i++) {
      final seg = parts[i];
      if (seg.isEmpty) continue;
      cur = cur.endsWith(sep) ? '$cur$seg' : '$cur$sep$seg';
      if (FileSystemEntity.isLinkSync(cur)) {
        final target = Link(cur).targetSync();
        _rejectUnsafeClientPathSpelling(target, allowLocalExtended: true);
      }
    }
  }

  static bool _isRootPath(String path) {
    final sep = Platform.pathSeparator;
    if (Platform.isWindows) {
      return path.length == 3 &&
          RegExp(r'^[A-Za-z]:$').hasMatch(path.substring(0, 2)) &&
          path[2] == sep;
    }
    return path == sep;
  }

  static String _canonicalizeWithExistingAncestor(String rawPath) {
    _rejectUnsafeClientPathSpelling(rawPath);
    final normalized = _normalizeSegments(rawPath);
    _rejectUnsafeLinkAncestors(normalized);
    final sep = Platform.pathSeparator;
    var cur = normalized;
    final tail = <String>[];
    while (cur.isNotEmpty) {
      final type = FileSystemEntity.typeSync(cur, followLinks: false);
      if (type != FileSystemEntityType.notFound) {
        final resolved = type == FileSystemEntityType.directory
            ? Directory(cur).resolveSymbolicLinksSync()
            : File(cur).resolveSymbolicLinksSync();
        _rejectUnsafeClientPathSpelling(resolved, allowLocalExtended: true);
        if (tail.isEmpty) return _normalizeSegments(resolved);
        return _normalizeSegments(
            '$resolved$sep${tail.reversed.join(sep)}');
      }
      if (_isRootPath(cur)) break;
      final sepIdx = cur.lastIndexOf(sep);
      if (sepIdx < 0) break;
      if (sepIdx == 0) {
        // POSIX root `/` — preserve root rather than breaking early.
        if (cur.length > 1) {
          tail.add(cur.substring(1));
          cur = sep;
          continue;
        }
        break;
      }
      // On Windows `C:\nonexistent`, sepIdx == 2: preserve `C:\` rather than
      // stripping to drive-relative `C:` (which resolves to process CWD!).
      if (Platform.isWindows &&
          sepIdx == 2 &&
          RegExp(r'^[A-Za-z]:$').hasMatch(cur.substring(0, 2))) {
        tail.add(cur.substring(sepIdx + 1));
        cur = '${cur.substring(0, 2)}$sep';
        continue;
      }
      tail.add(cur.substring(sepIdx + 1));
      cur = cur.substring(0, sepIdx);
    }
    return normalized;
  }

  static String _clientRoot(String workspaceRoot) {
    if (workspaceRoot.isNotEmpty) {
      _rejectUnsafeClientPathSpelling(workspaceRoot);
    }
    final rootRaw = workspaceRoot.trim().isEmpty
        ? Directory.current.path
        : workspaceRoot.trim();
    final resolvedRoot = _canonicalizeWithExistingAncestor(
      _isAbsolutePath(rootRaw)
          ? rootRaw
          : '${Directory.current.path}${Platform.pathSeparator}$rootRaw',
    );
    _rejectUnsafeClientPathSpelling(resolvedRoot);
    return resolvedRoot;
  }

  static String _resolveClientPath(
    Object? rawArg,
    String workspaceRoot, {
    required bool bypass,
  }) {
    final rawFull = rawArg?.toString() ?? '';
    _rejectUnsafeClientPathSpelling(rawFull);
    final root = _clientRoot(workspaceRoot);
    final raw = rawFull.trim();
    final candidate = raw.isEmpty || raw == '.'
        ? root
        : (_isAbsolutePath(raw) ? raw : '$root${Platform.pathSeparator}$raw');
    final resolved = _canonicalizeWithExistingAncestor(candidate);
    _rejectUnsafeClientPathSpelling(resolved);
    if (!bypass) {
      final rCheck = Platform.isWindows ? root.toLowerCase() : root;
      final pCheck = Platform.isWindows ? resolved.toLowerCase() : resolved;
      final sep = Platform.pathSeparator;
      final rootWithSep = rCheck.endsWith(sep) ? rCheck : '$rCheck$sep';
      if (pCheck != rCheck && !pCheck.startsWith(rootWithSep)) {
        throw FileSystemException(
            'Path "$raw" escapes workspace root "$root"', resolved);
      }
    }
    return resolved;
  }

  // Git runs programs named by repository metadata as a side effect of the
  // read-only git tools: core.fsmonitor on every status/diff, hooks, and the
  // filter/diff/merge drivers that .git/config defines and .gitattributes /
  // .git/info/attributes select. A write there would turn a write-class call
  // (no prompt in acceptEdits) into execution with no execute prompt, so the
  // write tools refuse these paths in every mode, bypass included. The same
  // holds for credential stores (a swapped token, or a program a later
  // command runs: ~/.ssh/config ProxyCommand, authorized_keys, an .npmrc
  // script-shell). Mirrors sonder_runtime/interfaces/client_agent/executor.py
  // and the server's file_ops secret-name rules.
  static const _gitMetadataDirectory = '.git';
  static const _gitControlFiles = {'.gitattributes', '.gitmodules', '.gitconfig'};
  static const _excludedSearchDirs = {
    '.git',
    '__pycache__',
    '.dart_tool',
    'node_modules',
  };
  static const _credentialDirectories = {
    '.ssh', '.aws', '.azure', '.gcloud', '.gnupg', '.kube', //
  };
  static const _credentialFiles = {
    '.credentials.json', '.netrc', '_netrc', '.token', 'auth.json',
    'credentials.json', 'secrets.json', 'token.json', '.git-credentials',
    '.pgpass', 'id_dsa', 'id_ecdsa', 'id_ecdsa_sk', 'id_ed25519',
    'id_ed25519_sk', 'id_rsa', '.npmrc', '.pypirc', 'kubeconfig',
    '.terraformrc', 'terraform.rc', 'credentials.tfrc.json', '.boto',
    '.s3cfg', '.htpasswd', '.vault-token', 'service-account.json',
    'service_account.json', 'serviceaccount.json',
    'application_default_credentials.json',
    'permission_mode.json', 'sonder-launcher-health.token',
  };
  static const _credentialSuffixes = {
    '.key', '.p12', '.pem', '.pfx', '.kdbx', '.ppk', '.p8', '.pk8',
  };
  static const _namedCredentialConfigSuffixes = {
    '.cfg', '.ini', '.json', '.toml', '.txt', '.yaml', '.yml', //
  };
  static const _credentialPairs = {
    '.docker/config.json', '.cargo/credentials.toml', '.cargo/credentials',
    '.gem/credentials', '.m2/settings.xml', '.m2/settings-security.xml',
    'github cli/hosts.yml',
  };
  static const _credentialDirectoryPairs = {'.config/gcloud', 'roaming/gcloud'};
  static const _credentialTriples = {
    '.config/gh/hosts.yml', '.config/glab-cli/config.yml', //
  };

  /// The name Windows opens for [part], lower-cased: NTFS ignores trailing
  /// dots and spaces and treats `name:stream` (`.git::$INDEX_ALLOCATION`) as
  /// `name`. Folded on every platform; on POSIX that only errs toward refusal.
  static String _canonicalClientComponent(String part) {
    final colon = part.indexOf(':');
    final s = colon >= 0 ? part.substring(0, colon) : part;
    var end = s.length;
    while (end > 0) {
      final c = s.codeUnitAt(end - 1);
      if (c != 0x20 && c != 0x2E) break;
      end--;
    }
    return s.substring(0, end).toLowerCase();
  }

  static bool _isInsideClientDir(String path, String dir) {
    final p = Platform.isWindows ? path.toLowerCase() : path;
    final d = Platform.isWindows ? dir.toLowerCase() : dir;
    final sep = Platform.pathSeparator;
    return p == d || p.startsWith(d.endsWith(sep) ? d : '$d$sep');
  }

  static List<String> _clientSplitPath(String path) =>
      path.split(RegExp(r'[/\\]')).where((s) => s.isNotEmpty).toList();

  /// Every component of [root], the credential rules' context.
  static List<String> _clientRootContext(String root) => _clientSplitPath(root);

  /// (the root's components, [path]'s components below [root]);
  /// no context and the whole path when it is outside.
  static (List<String>, List<String>) _clientComponentsUnder(
      String path, String root) {
    if (!_isInsideClientDir(path, root)) {
      return (const <String>[], _clientSplitPath(path));
    }
    final sep = Platform.pathSeparator;
    final prefixLength = root.endsWith(sep) ? root.length : root.length + 1;
    final rest = path.length <= prefixLength ? '' : path.substring(prefixLength);
    return (_clientRootContext(root), _clientSplitPath(rest));
  }

  static bool _isCredentialName(String name) {
    final dot = name.lastIndexOf('.');
    final suffix = dot > 0 ? name.substring(dot) : '';
    if (_credentialFiles.contains(name) || _credentialSuffixes.contains(suffix)) {
      return true;
    }
    if (name == '.env' ||
        name == '.envrc' ||
        name.startsWith('.env.') ||
        suffix == '.env') {
      return true;
    }
    return _namedCredentialConfigSuffixes.contains(suffix) &&
        (name.contains('credential') || name.contains('secret'));
  }

  /// [names] (canonical components below the root, leaf last) name a
  /// credential store. [context] holds every canonical component of the
  /// canonical workspace root (empty for a path outside the root, whose
  /// [names] are then the whole path). Every rule that names a directory
  /// runs on [context] plus [names], so a root that is, or sits anywhere
  /// inside, a credential store (`~/.aws`, `~/.aws/sso`,
  /// `~/.config/gcloud/configurations`) makes every write under it a
  /// credential write. The leaf-name rule still judges only the target's own
  /// name.
  static bool _isCredentialPath(List<String> names, List<String> context) {
    if (names.isEmpty) return false;
    final window = [...context, ...names];
    if (window.any(_credentialDirectories.contains)) return true;
    for (var i = 0; i + 1 < window.length; i++) {
      if (_credentialDirectoryPairs.contains('${window[i]}/${window[i + 1]}')) {
        return true;
      }
    }
    if (_isCredentialName(names.last)) return true;
    final n = window.length;
    if (n >= 2 &&
        _credentialPairs.contains('${window[n - 2]}/${window[n - 1]}')) {
      return true;
    }
    return n >= 3 &&
        _credentialTriples
            .contains('${window[n - 3]}/${window[n - 2]}/${window[n - 1]}');
  }

  /// [_resolveClientPath] for the write-class tools: refuses git metadata,
  /// git control files and credential stores, judged on both the lexical
  /// spelling (so `sub/../.git` is caught) and the resolved path (so a
  /// symlink or short name is caught). The git rules do not inspect
  /// components above the workspace root; a path outside it (bypass) is
  /// inspected in full. The credential rules also see every component of the
  /// root (see [_isCredentialPath]).
  static String _resolveClientWritePath(
    Object? rawArg,
    String workspaceRoot, {
    required bool bypass,
  }) {
    final resolved = _resolveClientPath(rawArg, workspaceRoot, bypass: bypass);
    final root = _clientRoot(workspaceRoot);
    final raw = (rawArg?.toString() ?? '').trim();
    final lexical = _isAbsolutePath(raw)
        ? _clientComponentsUnder(raw, root)
        : (_clientRootContext(root), _clientSplitPath(raw));
    for (final (contextParts, parts) in [
      lexical,
      _clientComponentsUnder(resolved, root),
    ]) {
      final names = parts.map(_canonicalClientComponent).toList();
      final context = contextParts.map(_canonicalClientComponent).toList();
      if (names.contains(_gitMetadataDirectory)) {
        throw _ClientPathRefused(
            "Refusing to modify git repository metadata '$raw': hooks and "
            'config there name programs git runs; use run_program (execute '
            'approval) if this is intended.');
      }
      if (names.isNotEmpty && _gitControlFiles.contains(names.last)) {
        throw _ClientPathRefused(
            "Refusing to modify git control file '$raw': it selects programs "
            'git runs; use run_program (execute approval) if this is intended.');
      }
      if (_isCredentialPath(names, context)) {
        throw _ClientPathRefused(
            "Refusing to modify credential store '$raw' from a client tool; "
            'use run_program (execute approval) if this is intended.');
      }
    }
    return resolved;
  }

  static bool _isGitConfigPath(List<String> names, List<String> context) {
    final window = [...context, ...names];
    for (var i = 0; i + 1 < window.length; i++) {
      if (window[i] == _gitMetadataDirectory &&
          (window[i + 1] == 'config' || window[i + 1].startsWith('config.'))) {
        return true;
      }
    }
    return names.isNotEmpty && names.last == '.gitconfig';
  }

  static bool _isBlockedClientReadPath(
      List<String> names, List<String> context) {
    return _isCredentialPath(names, context) || _isGitConfigPath(names, context);
  }

  /// [_resolveClientPath] for read-class tools (`file_read`, `text_search`):
  /// blocks credential stores and `.git/config` / `.gitconfig` in every mode
  /// so unprompted `read_only` tools cannot exfiltrate local secrets.
  static String _resolveClientReadPath(
    Object? rawArg,
    String workspaceRoot, {
    required bool bypass,
  }) {
    final resolved = _resolveClientPath(rawArg, workspaceRoot, bypass: bypass);
    final root = _clientRoot(workspaceRoot);
    final raw = (rawArg?.toString() ?? '').trim();
    final lexical = _isAbsolutePath(raw)
        ? _clientComponentsUnder(raw, root)
        : (_clientRootContext(root), _clientSplitPath(raw));
    for (final (contextParts, parts) in [
      lexical,
      _clientComponentsUnder(resolved, root),
    ]) {
      final names = parts.map(_canonicalClientComponent).toList();
      final context = contextParts.map(_canonicalClientComponent).toList();
      if (_isBlockedClientReadPath(names, context)) {
        throw _ClientPathRefused(
            "Refusing to read credential store or git config '$raw' from a "
            'client tool; use run_program (execute approval) if this is intended.');
      }
    }
    return resolved;
  }

  static Future<String> _readFileBoundedUtf8(File file, int maxBytes) async {
    final builder = BytesBuilder(copy: false);
    await for (final chunk in file.openRead(0, maxBytes)) {
      builder.add(chunk);
      if (builder.length >= maxBytes) break;
    }
    return const Utf8Decoder(allowMalformed: true).convert(builder.takeBytes());
  }

  /// Safe iterative directory walk that prunes excluded/credential directories,
  /// skips symlinks/junctions, and tolerates unreadable subdirectories.
  static Future<void> _walkClientFiles(
    Directory rootDir,
    String rootForPolicy, {
    required Future<bool> Function(File file, String relPath) onFile,
  }) async {
    final base = rootDir.path;
    final sep = Platform.pathSeparator;
    final stack = <Directory>[rootDir];
    while (stack.isNotEmpty) {
      final cur = stack.removeLast();
      List<FileSystemEntity> children;
      try {
        children = await cur.list(followLinks: false).toList();
      } catch (_) {
        continue;
      }
      children.sort((a, b) => a.path.toLowerCase().compareTo(b.path.toLowerCase()));
      final subDirs = <Directory>[];
      for (final entity in children) {
        if (entity is Link) continue;
        try {
          if (FileSystemEntity.isLinkSync(entity.path)) continue;
        } catch (_) {
          continue;
        }
        var rel = entity.path.startsWith(base)
            ? entity.path.substring(base.length)
            : entity.path;
        if (rel.startsWith(sep)) rel = rel.substring(1);
        final relPosix = rel.replaceAll('\\', '/');
        final segs = relPosix.split('/').where((s) => s.isNotEmpty).toList();
        if (segs.isEmpty) continue;
        final leafCanon = _canonicalClientComponent(segs.last);
        if (entity is Directory) {
          if (_excludedSearchDirs.contains(leafCanon) ||
              _credentialDirectories.contains(leafCanon)) {
            continue;
          }
          subDirs.add(entity);
        } else if (entity is File) {
          final (ctxParts, nameParts) =
              _clientComponentsUnder(entity.path, rootForPolicy);
          final names = nameParts.map(_canonicalClientComponent).toList();
          final context = ctxParts.map(_canonicalClientComponent).toList();
          if (_isBlockedClientReadPath(names, context)) continue;
          final keepGoing = await onFile(entity, relPosix);
          if (!keepGoing) return;
        }
      }
      for (var i = subDirs.length - 1; i >= 0; i--) {
        stack.add(subDirs[i]);
      }
    }
  }

  // Command-line -c values beat every config file, so these hold even for a
  // repository the tools did not write. Mirrors _GIT_STATIC_OVERRIDES in
  // sonder_runtime/interfaces/client_agent/executor.py:
  //   core.hooksPath / core.fsmonitor / core.sshCommand: programs git runs.
  //   submodule/diff/status settings: never descend into a submodule, whose
  //     own config the driver probe does not see.
  //   safe.bareRepository=explicit: a directory the write tools filled with
  //     HEAD/objects/refs/config (no .git component) is not a repository.
  //   protocol.allow=never (and GIT_NO_LAZY_FETCH): no lazy fetch runs a
  //     promisor remote's upload-pack.
  //   log.showSignature=false: git log never runs gpg.program.
  static List<String> get _gitStaticOverrides => [
        '-c', 'core.hooksPath=${Platform.isWindows ? 'nul' : '/dev/null'}',
        '-c', 'core.fsmonitor=false',
        '-c', 'core.sshCommand=',
        '-c', 'submodule.recurse=false',
        '-c', 'diff.ignoreSubmodules=all',
        '-c', 'diff.submodule=short',
        '-c', 'status.submoduleSummary=false',
        '-c', 'fetch.recurseSubmodules=false',
        '-c', 'safe.bareRepository=explicit',
        '-c', 'protocol.allow=never',
        '-c', 'log.showSignature=false',
      ];

  // `git config` arguments listing every configured filter/merge/diff driver
  // key (git's regex syntax; Dart never compiles it). `git config` launches
  // no program.
  static const _gitDriverProbe = [
    'config', '--null', '--name-only', '--get-regexp',
    r'^(filter\..*\.(clean|smudge|process|required)|merge\..*\.driver|diff\..*\.textconv)$',
  ];
  static const _gitDriverLeaves = {
    'filter': {'clean', 'smudge', 'process', 'required'},
    'merge': {'driver'},
    'diff': {'textconv'},
  };
  static const _builtinMergeDriver = 'git merge-file --marker-size=%L %A %O %B';
  // ASCII only and case-sensitive, so no Unicode case-fold slips in.
  static final _safeGitDriverName = RegExp(r'^[A-Za-z0-9_.-]+$');
  static const _gitProgramEnvVars = {
    'GIT_EXTERNAL_DIFF', 'GIT_PAGER', 'GIT_SSH', 'GIT_SSH_COMMAND',
    'GIT_ASKPASS',
  };
  static const _gitTimeout = Duration(seconds: 30);

  static Map<String, String> _clientGitEnvironment() {
    final env = <String, String>{};
    Platform.environment.forEach((key, value) {
      if (!_gitProgramEnvVars.contains(key.toUpperCase())) env[key] = value;
    });
    env['GIT_NO_LAZY_FETCH'] = '1';
    env['GIT_OPTIONAL_LOCKS'] = '0';
    env['GIT_TERMINAL_PROMPT'] = '0';
    return env;
  }

  /// Absolute path of the git on PATH; never one the write tools could have
  /// planted. Relative PATH entries are skipped, and on Windows only
  /// `git.exe` is accepted (a `git.cmd` would run through cmd.exe).
  static String _clientGitExecutable(String root, String cwd) {
    final pathVar = Platform.environment['PATH'] ?? '';
    final name = Platform.isWindows ? 'git.exe' : 'git';
    final sep = Platform.pathSeparator;
    for (var dir in pathVar.split(Platform.isWindows ? ';' : ':')) {
      dir = dir.trim();
      if (Platform.isWindows && dir.length >= 2 && dir.startsWith('"') && dir.endsWith('"')) {
        dir = dir.substring(1, dir.length - 1);
      }
      if (dir.isEmpty || !_isAbsolutePath(dir)) continue;
      final candidate = '${dir.endsWith(sep) ? dir : '$dir$sep'}$name';
      if (FileSystemEntity.typeSync(candidate) != FileSystemEntityType.file) {
        continue;
      }
      if (!Platform.isWindows && (File(candidate).statSync().mode & 0x49) == 0) {
        continue;
      }
      final resolved = File(candidate).resolveSymbolicLinksSync();
      for (final base in [root, cwd]) {
        if (_isInsideClientDir(resolved, base)) {
          throw _ClientGitRefused(
              'Refusing to run a git executable found inside the workspace: $resolved');
        }
      }
      return resolved;
    }
    throw const _ClientGitRefused('git is not installed or not on PATH');
  }

  /// Neutralising overrides for every driver key [_gitDriverProbe] listed:
  /// filters run no program (empty clean/smudge/process, required=false),
  /// merge drivers become git's built-in merge-file, textconv becomes `cat`
  /// (the diff and log tools also pass --no-textconv). A key of an
  /// unexpected shape, or a driver name `-c` cannot express exactly (git
  /// splits `-c` at the first '='; a quote, space, control or non-ASCII
  /// character may not round-trip), refuses: fail closed. Only names in
  /// [_safeGitDriverName] are overridden, the same charset as
  /// sonder_runtime/adapters/git_program_guard.py.
  static List<String> _clientGitDriverOverrides(String probeStdout) {
    final overrides = <String>[];
    final seen = <String>{};
    for (final rawKey in probeStdout.split('\x00')) {
      final key = rawKey.trim();
      if (key.isEmpty) continue;
      final firstDot = key.indexOf('.');
      final lastDot = key.lastIndexOf('.');
      if (firstDot <= 0 || lastDot <= firstDot + 1) {
        throw const _ClientGitRefused(
            'Unexpected git driver configuration key; refusing to run git.');
      }
      final section = key.substring(0, firstDot).toLowerCase();
      final name = key.substring(firstDot + 1, lastDot);
      final leaf = key.substring(lastDot + 1).toLowerCase();
      if (!(_gitDriverLeaves[section]?.contains(leaf) ?? false)) {
        throw const _ClientGitRefused(
            'Unexpected git driver configuration key; refusing to run git.');
      }
      if (!_safeGitDriverName.hasMatch(name)) {
        throw const _ClientGitRefused(
            'git driver name is outside [A-Za-z0-9_.-]; refusing to run git.');
      }
      if (!seen.add('$section.$name')) continue;
      final prefix = '$section.$name.';
      if (section == 'filter') {
        overrides.addAll([
          '-c', '${prefix}process=',
          '-c', '${prefix}clean=',
          '-c', '${prefix}smudge=',
          '-c', '${prefix}required=false',
        ]);
      } else if (section == 'merge') {
        overrides.addAll(['-c', '${prefix}driver=$_builtinMergeDriver']);
      } else {
        overrides.addAll(['-c', '${prefix}textconv=cat']);
      }
    }
    return overrides;
  }

  /// The git arguments that keep repository config from launching a
  /// program: the static overrides, then a `git config` probe (which
  /// launches nothing) of the configured drivers, each neutralised. A probe
  /// that fails (including safe.bareRepository refusing an embedded bare
  /// repository) or times out throws, and no further git command runs.
  static Future<List<String>> _clientHardenedGitArgs(
    String git,
    String cwd,
    Map<String, String> env,
  ) async {
    final base = ['--no-pager', '--no-optional-locks', ..._gitStaticOverrides];
    final probe = await runBoundedProcess(
      git,
      [...base, ..._gitDriverProbe],
      workingDirectory: cwd,
      environment: env,
      includeParentEnvironment: false,
      timeout: _gitTimeout,
    );
    if (probe.timedOut) {
      throw const _ClientGitRefused(
          'git driver configuration probe timed out; refusing to run git.');
    }
    if (probe.exitCode != 0 && probe.exitCode != 1) {
      // 1 is git's "no matching key".
      final detail = probe.output.isEmpty ? 'exit code ${probe.exitCode}' : probe.output;
      throw _ClientGitRefused(
          'git driver configuration could not be read; refusing to run git: '
          '${_boundClientText(detail, _maxClientErrorChars)}');
    }
    return [...base, ..._clientGitDriverOverrides(probe.stdout)];
  }

  /// Reports native desktop execution readiness (or unavailability on mobile).
  static Future<ClientAgentHealth> checkClientAgent({
    String companionUrl = '',
    String token = '',
    String workspaceRoot = '',
  }) async {
    if (!canRunLocalTools) {
      return ClientAgentHealth(
        reachable: false,
        nativeRunner: false,
        message: 'Local execution is not supported on $platformLabel.',
      );
    }
    final ws = workspaceRoot.trim().isEmpty
        ? Directory.current.path
        : workspaceRoot.trim();
    return ClientAgentHealth(
      reachable: true,
      authenticated: true,
      nativeRunner: true,
      workspace: ws,
      canBypass: true,
      message: 'Native $platformLabel execution ready.',
    );
  }

  /// No-op for native desktop runner (mode is held in client settings).
  static Future<bool> setClientAgentMode({
    required String mode,
    bool confirmBypass = false,
    String companionUrl = '',
    String token = '',
  }) async =>
      canRunLocalTools;

  /// Execute one `execution_target="client"` tool natively on the desktop PC.
  static Future<ClientToolExecResult> executeClientTool({
    required String callId,
    required String tool,
    required Map<String, dynamic> args,
    String risk = '',
    String mode = 'manual',
    String workspaceRoot = '',
    bool approved = false,
    String approvalToken = '',
    String argsDigest = '',
    String companionUrl = '',
    String companionToken = '',
  }) async {
    if (!canRunLocalTools) {
      return ClientToolExecResult(
        ok: false,
        error: 'Client tool execution is not supported on $platformLabel.',
      );
    }
    final cleanTool = tool.trim().replaceFirst(RegExp(r'^/+'), '');
    // A declared risk can raise the local classification, never lower it.
    final effectiveRisk = effectiveClientToolRisk(risk, cleanTool);
    if (!approved && !clientModeAllowsWithoutPrompt(mode, effectiveRisk)) {
      return ClientToolExecResult(
        ok: false,
        needsApproval: true,
        error:
            "Approval required on your PC for '$cleanTool' ($effectiveRisk) in $mode mode.",
      );
    }
    final bypass = mode.trim() == 'bypass';
    try {
      switch (cleanTool) {
        case 'file_read':
          final path = _resolveClientReadPath(args['path'], workspaceRoot,
              bypass: bypass);
          final file = File(path);
          if (!await file.exists()) {
            return ClientToolExecResult(
                ok: false, error: 'File not found: $path');
          }
          final content = await _readFileBoundedUtf8(file, _maxClientReadBytes);
          final lines = const LineSplitter().convert(content);
          final rawOffset = int.tryParse('${args['offset'] ?? 1}') ?? 1;
          final offset = rawOffset < 1 ? 1 : rawOffset;
          final rawLimit = int.tryParse('${args['limit'] ?? 200}') ?? 200;
          final limit = rawLimit.clamp(1, 500);
          final end = (offset - 1 + limit).clamp(0, lines.length);
          final start = (offset - 1).clamp(0, lines.length);
          final out = <String>[];
          for (var i = start; i < end; i++) {
            out.add('${i + 1}: ${lines[i]}');
          }
          return ClientToolExecResult(
            ok: true,
            output: _boundClientText(out.join('\n'), _maxClientOutputChars),
          );
        case 'file_write':
          final path = _resolveClientWritePath(args['path'], workspaceRoot,
              bypass: bypass);
          final content = args['content']?.toString() ?? '';
          final writeMode =
              (args['mode']?.toString() ?? 'overwrite').trim().toLowerCase();
          final file = File(path);
          if (writeMode == 'create' && await file.exists()) {
            return ClientToolExecResult(
                ok: false, error: 'File already exists: $path');
          }
          await file.parent.create(recursive: true);
          if (writeMode == 'append' && await file.exists()) {
            await file.writeAsString(content, mode: FileMode.append);
          } else {
            await file.writeAsString(content);
          }
          return ClientToolExecResult(
            ok: true,
            output: 'Wrote ${content.length} chars to $path',
          );
        case 'file_edit':
          final path = _resolveClientWritePath(args['path'], workspaceRoot,
              bypass: bypass);
          final oldStr = args['old']?.toString() ?? '';
          final newStr = args['new']?.toString() ?? '';
          if (oldStr.isEmpty) {
            return const ClientToolExecResult(
                ok: false, error: "'old' text must be non-empty");
          }
          final file = File(path);
          if (!await file.exists()) {
            return ClientToolExecResult(
                ok: false, error: 'File not found: $path');
          }
          final text = await file.readAsString();
          final count = oldStr.allMatches(text).length;
          if (count == 0) {
            return const ClientToolExecResult(
                ok: false, error: 'Target string not found in file');
          }
          if (count > 1) {
            return ClientToolExecResult(
                ok: false,
                error: 'Target string matched $count times; must be unique');
          }
          await file.writeAsString(text.replaceFirst(oldStr, newStr));
          return ClientToolExecResult(ok: true, output: 'Edited $path');
        case 'file_delete':
          final path = _resolveClientWritePath(args['path'], workspaceRoot,
              bypass: bypass);
          final file = File(path);
          if (!await file.exists()) {
            return ClientToolExecResult(
                ok: false, error: 'File not found: $path');
          }
          await file.delete();
          return ClientToolExecResult(ok: true, output: 'Deleted $path');
        case 'directory_create':
          final path = _resolveClientWritePath(args['path'], workspaceRoot,
              bypass: bypass);
          await Directory(path).create(recursive: true);
          return ClientToolExecResult(
              ok: true, output: 'Created directory $path');
        case 'directory_tree':
          final base = _resolveClientPath(
              args['path'] ?? '.', workspaceRoot, bypass: bypass);
          final dir = Directory(base);
          if (!await dir.exists()) {
            return ClientToolExecResult(
                ok: false, error: 'Directory not found: $base');
          }
          final depth =
              (int.tryParse('${args['depth'] ?? 2}') ?? 2).clamp(1, 5);
          final lines = <String>[];
          Future<void> walk(Directory cur, int level) async {
            if (level > depth || lines.length >= 200) return;
            List<FileSystemEntity> children;
            try {
              children = await cur.list(followLinks: false).toList();
            } catch (_) {
              return;
            }
            children.sort((a, b) {
              final aDir = a is Directory ? 0 : 1;
              final bDir = b is Directory ? 0 : 1;
              if (aDir != bDir) return aDir.compareTo(bDir);
              return a.path.toLowerCase().compareTo(b.path.toLowerCase());
            });
            for (final entity in children) {
              if (!bypass &&
                  (entity is Link || FileSystemEntity.isLinkSync(entity.path))) {
                continue;
              }
              final name = entity.uri.pathSegments
                  .where((s) => s.isNotEmpty)
                  .lastOrNull ?? '';
              final canon = _canonicalClientComponent(name);
              if (_excludedSearchDirs.contains(canon)) continue;
              final indent = '  ' * (level - 1);
              final isDir = entity is Directory;
              lines.add('$indent$name${isDir ? '/' : ''}');
              if (lines.length >= 200) return;
              if (isDir && level < depth) await walk(entity, level + 1);
            }
          }
          await walk(dir, 1);
          return ClientToolExecResult(
            ok: true,
            output: lines.isEmpty ? '(empty)' : lines.join('\n'),
          );
        case 'file_find':
          final base = _resolveClientPath(
              args['root'] ?? '.', workspaceRoot, bypass: bypass);
          final dir = Directory(base);
          if (!await dir.exists()) {
            return ClientToolExecResult(
                ok: false, error: 'Directory not found: $base');
          }
          final pattern = (args['pattern']?.toString() ?? '*').trim();
          final regex = _globToRegex(pattern.isEmpty ? '*' : pattern);
          final rootForPolicy = _clientRoot(workspaceRoot);
          final matches = <String>[];
          await _walkClientFiles(dir, rootForPolicy,
              onFile: (entity, rel) async {
            final name = rel.split('/').last;
            if (regex.hasMatch(name) || regex.hasMatch(rel)) {
              matches.add(rel);
              if (matches.length >= 200) return false;
            }
            return true;
          });
          matches.sort();
          return ClientToolExecResult(
            ok: true,
            output: matches.isEmpty ? '(no matches)' : matches.join('\n'),
          );
        case 'text_search':
          final query = args['query']?.toString() ?? '';
          if (query.isEmpty) {
            return const ClientToolExecResult(
                ok: false, error: 'query is required');
          }
          final base = _resolveClientReadPath(
              args['root'] ?? '.', workspaceRoot, bypass: bypass);
          final dir = Directory(base);
          if (!await dir.exists()) {
            return ClientToolExecResult(
                ok: false, error: 'Directory not found: $base');
          }
          final globPat = (args['glob']?.toString() ?? '').trim();
          final globRegex =
              globPat.isEmpty ? null : _globToRegex(globPat);
          final rootForPolicy = _clientRoot(workspaceRoot);
          final matches = <String>[];
          await _walkClientFiles(dir, rootForPolicy,
              onFile: (entity, rel) async {
            final name = rel.split('/').last;
            if (globRegex != null && !globRegex.hasMatch(name)) return true;
            try {
              if (await entity.length() > 512 * 1024) return true;
              final content = await _readFileBoundedUtf8(entity, 512 * 1024);
              final fileLines = const LineSplitter().convert(content);
              for (var i = 0; i < fileLines.length; i++) {
                if (fileLines[i].contains(query)) {
                  final lineText = fileLines[i].trim();
                  final clipped = lineText.length <= 200
                      ? lineText
                      : lineText.substring(0, 200);
                  matches.add('$rel:${i + 1}: $clipped');
                  if (matches.length >= 100) return false;
                }
              }
            } catch (_) {}
            return matches.length < 100;
          });
          return ClientToolExecResult(
            ok: true,
            output: matches.isEmpty ? '(no matches)' : matches.join('\n'),
          );
        case 'run_code':
          final code = args['code']?.toString() ?? '';
          if (code.trim().isEmpty) {
            return const ClientToolExecResult(
                ok: false, error: 'code is required');
          }
          final lang =
              (args['language']?.toString() ?? 'python').trim().toLowerCase();
          final timeoutSec =
              (int.tryParse('${args['timeout'] ?? 15}') ?? 15).clamp(1, 60);
          final cwd = _resolveClientPath('.', workspaceRoot, bypass: bypass);
          late final String exe;
          late final List<String> procArgs;
          Directory? tempScriptDir;
          if (lang == 'python' || lang == 'py') {
            exe = Platform.isWindows ? 'python' : 'python3';
            procArgs = ['-c', code];
          } else if (lang == 'js' || lang == 'javascript' || lang == 'node') {
            exe = 'node';
            procArgs = ['-e', code];
          } else if (lang == 'powershell' || lang == 'ps1') {
            tempScriptDir = await Directory.systemTemp.createTemp('sonder_ps_');
            final tempScript = File(
                '${tempScriptDir.path}${Platform.pathSeparator}script.ps1');
            await tempScript.writeAsString(code);
            exe = 'powershell';
            procArgs = ['-NoProfile', '-File', tempScript.path];
          } else if (lang == 'bash' || lang == 'sh') {
            exe = 'bash';
            procArgs = ['-lc', code];
          } else {
            return ClientToolExecResult(
                ok: false, error: 'Unsupported language on client: $lang');
          }
          try {
            final res = await runBoundedProcess(
              exe,
              procArgs,
              workingDirectory: cwd,
              environment: clientProcessEnvironment(Platform.environment),
              includeParentEnvironment: false,
              timeout: Duration(seconds: timeoutSec),
              maxOutputChars: _maxClientProcessOutputChars,
            );
            return _fromProcessResult(res);
          } finally {
            try {
              if (tempScriptDir != null && await tempScriptDir.exists()) {
                await tempScriptDir.delete(recursive: true);
              }
            } catch (_) {}
          }
        case 'run_program':
          final command = (args['command']?.toString() ?? '').trim();
          if (command.isEmpty) {
            return const ClientToolExecResult(
                ok: false, error: 'command is required');
          }
          final cwd = _resolveClientPath(
              args['cwd'] ?? '.', workspaceRoot, bypass: bypass);
          final timeoutSec =
              (int.tryParse('${args['timeout'] ?? 30}') ?? 30).clamp(1, 120);
          final tokens = _splitCommand(command);
          if (tokens.isEmpty) {
            return const ClientToolExecResult(
                ok: false, error: 'command is required');
          }
          final res = await runBoundedProcess(
            tokens.first,
            tokens.skip(1).toList(),
            workingDirectory: cwd,
            environment: clientProcessEnvironment(Platform.environment),
            includeParentEnvironment: false,
            timeout: Duration(seconds: timeoutSec),
            maxOutputChars: _maxClientProcessOutputChars,
          );
          return _fromProcessResult(res);
        case 'test_run':
          final cwd = _resolveClientPath(
              args['root'] ?? '.', workspaceRoot, bypass: bypass);
          final procArgs = ['-m', 'pytest', '-p', 'no:cacheprovider', '-q'];
          final relPath = (args['path']?.toString() ?? '').trim();
          if (relPath.isNotEmpty) {
            procArgs.add(
                _resolveClientPath(relPath, cwd, bypass: bypass));
          }
          final pat = (args['pattern']?.toString() ?? '').trim();
          if (pat.isNotEmpty) {
            procArgs.addAll(['-k', pat]);
          }
          final res = await runBoundedProcess(
            Platform.isWindows ? 'python' : 'python3',
            procArgs,
            workingDirectory: cwd,
            environment: clientProcessEnvironment(Platform.environment),
            includeParentEnvironment: false,
            timeout: const Duration(seconds: 120),
            maxOutputChars: _maxClientProcessOutputChars,
          );
          return _fromProcessResult(res);
        case 'git_status':
        case 'git_diff':
        case 'git_log':
        case 'git_branch':
          final cwd = _resolveClientPath(
              args['cwd'] ?? '.', workspaceRoot, bypass: bypass);
          final gitArgs = <String>[];
          if (cleanTool == 'git_status') {
            gitArgs.addAll(['status', '--short']);
          } else if (cleanTool == 'git_diff') {
            gitArgs.addAll(['diff', '--no-ext-diff', '--no-textconv']);
            if (args['staged'] == true) gitArgs.add('--staged');
            final rel = (args['path']?.toString() ?? '').trim();
            if (rel.isNotEmpty) {
              gitArgs.addAll(
                  ['--', _resolveClientPath(rel, cwd, bypass: bypass)]);
            }
          } else if (cleanTool == 'git_log') {
            final limit =
                (int.tryParse('${args['limit'] ?? 20}') ?? 20).clamp(1, 100);
            gitArgs.addAll(
                ['log', '--no-ext-diff', '--no-textconv', '-n$limit', '--oneline']);
          } else {
            gitArgs.addAll(['branch', '-a']);
          }
          final env = _clientGitEnvironment();
          final git = _clientGitExecutable(_clientRoot(workspaceRoot), cwd);
          final hardened = await _clientHardenedGitArgs(git, cwd, env);
          final res = await runBoundedProcess(
            git,
            [...hardened, ...gitArgs],
            workingDirectory: cwd,
            environment: env,
            includeParentEnvironment: false,
            timeout: _gitTimeout,
          );
          return _fromProcessResult(res);
        default:
          return ClientToolExecResult(
            ok: false,
            denied: true,
            error: "Tool '$cleanTool' is not supported for client execution.",
          );
      }
    } on _ClientPathRefused catch (e) {
      return ClientToolExecResult(
        ok: false,
        denied: true,
        error: _boundClientText(e.message, _maxClientErrorChars),
      );
    } catch (e) {
      return ClientToolExecResult(
        ok: false,
        error: _boundClientText('$e', _maxClientErrorChars),
      );
    }
  }

  static ClientToolExecResult _fromProcessResult(BoundedProcessResult res) {
    final combined = res.output;
    if (res.exitCode == 0 && !res.timedOut) {
      return ClientToolExecResult(
        ok: true,
        output: _boundClientText(
          combined.isEmpty ? '(no output)' : combined,
          _maxClientOutputChars,
        ),
      );
    }
    return ClientToolExecResult(
      ok: false,
      output: _boundClientText(res.stdout, _maxClientOutputChars),
      error: _boundClientText(
        res.timedOut
            ? 'Command timed out.'
            : (combined.isEmpty ? 'Exit code ${res.exitCode}' : combined),
        _maxClientErrorChars,
      ),
    );
  }

  static RegExp _globToRegex(String glob) {
    final buf = StringBuffer('^');
    for (var i = 0; i < glob.length; i++) {
      final ch = glob[i];
      if (ch == '*') {
        buf.write('.*');
      } else if (ch == '?') {
        buf.write('.');
      } else if (r'\.+^$[]{}()|'.contains(ch)) {
        buf.write('\\$ch');
      } else {
        buf.write(ch);
      }
    }
    buf.write(r'$');
    return RegExp(buf.toString(), caseSensitive: !Platform.isWindows);
  }

  static List<String> _splitCommand(String command) {
    final out = <String>[];
    final buf = StringBuffer();
    String? quote;
    var hasToken = false;
    for (var i = 0; i < command.length; i++) {
      final ch = command[i];
      if (quote != null) {
        if (quote == '"' && ch == '\\' && i + 1 < command.length) {
          final next = command[i + 1];
          if (next == '"' || next == '\\') {
            buf.write(next);
            i++;
            continue;
          }
        }
        if (ch == quote) {
          quote = null;
        } else {
          buf.write(ch);
        }
      } else if (ch == '"') {
        quote = '"';
        hasToken = true;
      } else if (ch == "'" && (!Platform.isWindows || !hasToken)) {
        quote = "'";
        hasToken = true;
      } else if (ch.trim().isEmpty) {
        if (hasToken) {
          out.add(buf.toString());
          buf.clear();
          hasToken = false;
        }
      } else {
        buf.write(ch);
        hasToken = true;
      }
    }
    if (quote != null) {
      throw const FormatException('Unclosed quote in command.');
    }
    if (hasToken) out.add(buf.toString());
    return out;
  }
}

/// A write-class client tool targeted git metadata, a git control file or a
/// credential store; reported as a denial.
class _ClientPathRefused implements Exception {
  final String message;
  const _ClientPathRefused(this.message);

  @override
  String toString() => message;
}

/// A git tool refused to run (fail closed); reported as a tool error.
class _ClientGitRefused implements Exception {
  final String message;
  const _ClientGitRefused(this.message);

  @override
  String toString() => message;
}

/// The outcome of [LocalManager.runBoundedProcess].
class BoundedProcessResult {
  final int exitCode;
  final String stdout;
  final String stderr;

  /// The process outlived its timeout and its tree was killed.
  final bool timedOut;

  const BoundedProcessResult({
    required this.exitCode,
    this.stdout = '',
    this.stderr = '',
    this.timedOut = false,
  });

  /// Trimmed stdout then stderr, one per line.
  String get output => [
        if (stdout.trim().isNotEmpty) stdout.trim(),
        if (stderr.trim().isNotEmpty) stderr.trim(),
      ].join('\n');
}
