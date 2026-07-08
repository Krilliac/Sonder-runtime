import 'dart:io';

class LocalActionResult {
  final bool ok;
  final String message;

  const LocalActionResult(this.ok, this.message);
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

  static Future<LocalActionResult> startServer() async {
    if (!canRunLocalTools) {
      return LocalActionResult(
        false,
        'Local process startup is not available on $platformLabel. Run the server on a desktop or LAN host.',
      );
    }
    final system = bundledSystemDirectory();
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
            runInShell: true,
          );
          return const LocalActionResult(true, 'Endless training started.');
        }
      }
      await Process.start(
        Platform.isWindows ? 'python.exe' : 'python3',
        ['endless_train.py'],
        workingDirectory: system.path,
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
