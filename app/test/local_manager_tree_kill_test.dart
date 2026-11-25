import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/local_manager.dart';

/// Exact PID check via `tasklist /FO CSV /NH` so substring matches against
/// unrelated memory columns or messages cannot false-positive.
bool _alive(int pid) {
  final out = Process.runSync(
    'tasklist',
    ['/FI', 'PID eq $pid', '/FO', 'CSV', '/NH'],
  );
  final stdout = '${out.stdout}';
  for (final line in stdout.split(RegExp(r'\r?\n'))) {
    final trimmed = line.trim();
    if (!trimmed.startsWith('"')) continue;
    // CSV columns: "Image Name","PID","Session Name","Session#","Mem Usage"
    if (trimmed.contains('","$pid","')) return true;
  }
  return false;
}

Future<int?> _waitForChildPid(
  File pidFile,
  Process launcher, {
  Duration timeout = const Duration(seconds: 30),
}) async {
  int? launcherExit;
  launcher.exitCode.then((code) => launcherExit = code);
  final deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    try {
      if (pidFile.existsSync()) {
        final parsed = int.tryParse(pidFile.readAsStringSync().trim());
        if (parsed != null && parsed > 0) return parsed;
      }
    } on FileSystemException {
      // Tolerate transient Windows sharing violations while the child finishes
      // moving the PID file into place.
    }
    if (launcherExit != null) {
      // One final read in case the PID file landed right before exit.
      try {
        if (pidFile.existsSync()) {
          final parsed = int.tryParse(pidFile.readAsStringSync().trim());
          if (parsed != null && parsed > 0) return parsed;
        }
      } on FileSystemException {
        // Fall through to report exit.
      }
      return null;
    }
    await Future<void>.delayed(const Duration(milliseconds: 50));
  }
  return null;
}

Future<bool> _waitForProcessExit(
  int pid, {
  Duration timeout = const Duration(seconds: 10),
}) async {
  final deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    if (!_alive(pid)) return true;
    await Future<void>.delayed(const Duration(milliseconds: 100));
  }
  return !_alive(pid);
}

Future<Process> _spawnShellWithChild(File pidFile) {
  // Pass the target path via environment (immune to spaces/quotes in %TEMP%)
  // and write via temp file + atomic Move so the test never reads an empty
  // 0-byte file created before Out-File flushes its buffer.
  return Process.start(
    'cmd.exe',
    [
      '/c',
      'powershell',
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      r'$p = $env:SONDER_TEST_PID_FILE; '
          r'$t = "$p.tmp"; '
          r'[System.IO.File]::WriteAllText($t, "$PID"); '
          r'[System.IO.File]::Move($t, $p); '
          r'Start-Sleep -Seconds 120',
    ],
    environment: {
      ...Platform.environment,
      'SONDER_TEST_PID_FILE': pidFile.path,
    },
  );
}

void main() {
  test('closing the app kills the launcher shell and its child server',
      () async {
    final tmp = await Directory.systemTemp.createTemp('sonder_kill_test');
    addTearDown(() async {
      try {
        if (await tmp.exists()) await tmp.delete(recursive: true);
      } catch (_) {}
    });
    final pidFile = File('${tmp.path}${Platform.pathSeparator}child.pid');
    final launcher = await _spawnShellWithChild(pidFile);
    LocalManager.trackManagedServerForTest(launcher);
    addTearDown(() {
      LocalManager.stopManagedServerNow();
      try {
        Process.runSync('taskkill', ['/PID', '${launcher.pid}', '/T', '/F']);
      } catch (_) {}
    });

    final childPid = await _waitForChildPid(pidFile, launcher);
    expect(
      childPid,
      isNotNull,
      reason: 'child server never started '
          '(output: ${LocalManager.managedServerOutputTail()})',
    );
    expect(_alive(childPid!), isTrue);

    LocalManager.stopManagedServerNow();
    expect(await _waitForProcessExit(childPid), isTrue);
    expect(await _waitForProcessExit(launcher.pid), isTrue);
  }, skip: Platform.isWindows ? false : 'Windows tree-kill check');

  test('stopServers kills tracked process tree and tolerates already-exited PID',
      () async {
    final tmp = await Directory.systemTemp.createTemp('sonder_stop_test');
    addTearDown(() async {
      try {
        if (await tmp.exists()) await tmp.delete(recursive: true);
      } catch (_) {}
    });
    final pidFile = File('${tmp.path}${Platform.pathSeparator}child.pid');
    final launcher = await _spawnShellWithChild(pidFile);
    LocalManager.trackManagedServerForTest(launcher);
    addTearDown(() {
      LocalManager.stopManagedServerNow();
      try {
        Process.runSync('taskkill', ['/PID', '${launcher.pid}', '/T', '/F']);
      } catch (_) {}
    });

    final childPid = await _waitForChildPid(pidFile, launcher);
    expect(
      childPid,
      isNotNull,
      reason: 'child server never started '
          '(output: ${LocalManager.managedServerOutputTail()})',
    );
    expect(_alive(childPid!), isTrue);

    final result = await LocalManager.stopServers();
    expect(result.ok, isTrue, reason: result.message);
    expect(await _waitForProcessExit(childPid), isTrue);
    expect(await _waitForProcessExit(launcher.pid), isTrue);
  }, skip: Platform.isWindows ? false : 'Windows tree-kill check');
}
