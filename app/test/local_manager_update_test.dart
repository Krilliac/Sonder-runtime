// Git update safety: a bundled-system swap that fails halfway puts the live
// folder back, and an update process that outlives its timeout is killed
// (with its children) instead of being orphaned while the app reports a
// generic failure.
@TestOn('vm')
library;

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/local_manager_native.dart';

void main() {
  late Directory tmp;

  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('sonder_update_test');
  });

  tearDown(() async {
    if (await tmp.exists()) await tmp.delete(recursive: true);
  });

  Directory dir(String name) =>
      Directory('${tmp.path}${Platform.pathSeparator}$name');

  Future<Directory> withMarker(String name, String marker) async {
    final d = await dir(name).create();
    await File('${d.path}${Platform.pathSeparator}marker.txt')
        .writeAsString(marker);
    return d;
  }

  Future<String> markerOf(Directory d) =>
      File('${d.path}${Platform.pathSeparator}marker.txt').readAsString();

  test('a successful swap installs the new tree and keeps no backup', () async {
    final system = await withMarker('local-system', 'old');
    final next = await withMarker('local-system-next', 'new');
    final backup = dir('local-system-backup');
    await LocalManager.swapInBundledSystem(system, next, backup);
    expect(await markerOf(system), 'new');
    expect(await next.exists(), isFalse);
    expect(await backup.exists(), isFalse);
  });

  test('a failed second rename restores the live folder', () async {
    final system = await withMarker('local-system', 'old');
    final next = await withMarker('local-system-next', 'new');
    final backup = dir('local-system-backup');
    var calls = 0;
    Future<void> flakyRename(Directory from, String to) async {
      calls++;
      if (from.path == next.path) {
        throw const FileSystemException('Access is denied');
      }
      await from.rename(to);
    }

    await expectLater(
        LocalManager.swapInBundledSystem(system, next, backup,
            rename: flakyRename),
        throwsA(isA<FileSystemException>()));
    expect(await system.exists(), isTrue);
    expect(await markerOf(system), 'old');
    expect(await backup.exists(), isFalse);
    expect(calls, greaterThanOrEqualTo(3));
  });

  test('a process past its timeout is killed and reported as such', () async {
    final killed = <int>[];
    final started = DateTime.now();
    final result = await LocalManager.runBoundedProcess(
      Platform.isWindows ? 'cmd.exe' : 'sleep',
      Platform.isWindows ? ['/c', 'ping', '-n', '30', '127.0.0.1'] : ['30'],
      workingDirectory: tmp.path,
      timeout: const Duration(milliseconds: 500),
      onKillTree: (pid) {
        killed.add(pid);
        return LocalManager.killProcessTree(pid);
      },
    );
    expect(result.timedOut, isTrue);
    expect(killed, hasLength(1));
    expect(DateTime.now().difference(started),
        lessThan(const Duration(seconds: 15)));
  });

  test('a timed-out Unix updater cannot leave a child running', () async {
    final marker = File('${tmp.path}${Platform.pathSeparator}orphan.txt');
    const child = 'import sys,time; time.sleep(2); '
        'open(sys.argv[1], "w").write("orphaned")';
    const parent = 'import subprocess,sys,time; '
        'subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[1]]); '
        'print("child started", flush=True); '
        'time.sleep(30)';
    final result = await LocalManager.runBoundedProcess(
      'python3',
      ['-c', parent, marker.path, child],
      workingDirectory: tmp.path,
      timeout: const Duration(milliseconds: 500),
    );
    expect(result.timedOut, isTrue);
    expect(result.stdout, contains('child started'));
    await Future<void>.delayed(const Duration(seconds: 3));
    expect(await marker.exists(), isFalse);
  }, skip: Platform.isWindows);

  test('a process that finishes in time reports its output and exit code',
      () async {
    final result = await LocalManager.runBoundedProcess(
      Platform.isWindows ? 'cmd.exe' : 'sh',
      Platform.isWindows ? ['/c', 'echo hello'] : ['-c', 'echo hello'],
      workingDirectory: tmp.path,
      timeout: const Duration(seconds: 30),
    );
    expect(result.timedOut, isFalse);
    expect(result.exitCode, 0);
    expect(result.stdout.trim(), 'hello');
  });

  test('the timeout message says where local edits may be', () {
    expect(LocalManager.updateTimeoutMessage(const Duration(minutes: 8)),
        allOf(contains('stopped'), contains('git stash list')));
  });
}
