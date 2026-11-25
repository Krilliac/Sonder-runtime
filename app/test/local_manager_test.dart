import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/local_manager.dart';
import 'package:sonder_runtime/local_manager_native.dart' as native_mgr;

/// The startup log is the only place a failed launch explains itself, so the
/// reader that surfaces it has to be total: a missing file, an empty file, a
/// huge file and a torn multi-byte sequence must each yield something the UI
/// can show rather than an exception that puts us back to a silent failure.
void main() {
  late Directory tmp;

  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('sonder_log_test');
  });

  tearDown(() async {
    if (await tmp.exists()) {
      await tmp.delete(recursive: true);
    }
  });

  String path(String name) => '${tmp.path}${Platform.pathSeparator}$name';

  test('readLogTail returns empty for a missing log', () async {
    expect(await LocalManager.readLogTail(path('absent.log')), '');
  });

  test('waitForServer backs off signed-health retries without weakening trust',
      () async {
    var attempts = 0;
    final waits = <Duration>[];

    final reachable = await LocalManager.waitForServer(
      timeout: const Duration(seconds: 10),
      interval: const Duration(milliseconds: 100),
      reachabilityProbe: () async => ++attempts == 5,
      delay: (duration) async => waits.add(duration),
    );

    expect(reachable, isTrue);
    expect(attempts, 5);
    expect(waits, const [
      Duration(milliseconds: 100),
      Duration(milliseconds: 200),
      Duration(milliseconds: 400),
      Duration(milliseconds: 800),
    ]);
  });

  test('waitForServer caps signed-health retry backoff', () async {
    var attempts = 0;
    final waits = <Duration>[];

    final reachable = await LocalManager.waitForServer(
      timeout: const Duration(seconds: 10),
      interval: const Duration(milliseconds: 500),
      reachabilityProbe: () async => ++attempts == 6,
      delay: (duration) async => waits.add(duration),
    );

    expect(reachable, isTrue);
    expect(waits, const [
      Duration(milliseconds: 500),
      Duration(seconds: 1),
      Duration(seconds: 2),
      Duration(seconds: 2),
      Duration(seconds: 2),
    ]);
  });

  test('waitForServer never probes after its timeout expires', () async {
    var now = DateTime.utc(2026, 1, 1);
    var attempts = 0;
    final waits = <Duration>[];

    final reachable = await LocalManager.waitForServer(
      timeout: const Duration(milliseconds: 150),
      interval: const Duration(milliseconds: 100),
      reachabilityProbe: () async {
        attempts++;
        return false;
      },
      delay: (duration) async {
        waits.add(duration);
        now = now.add(duration);
      },
      clock: () => now,
    );

    expect(reachable, isFalse);
    expect(attempts, 2);
    expect(waits, const [
      Duration(milliseconds: 100),
      Duration(milliseconds: 50),
    ]);
  });

  test('startServer refuses an unverified occupied local port', () async {
    final result = await LocalManager.startServer(
      managedReachabilityProbe: () async => false,
      portOccupiedProbe: () async => true,
    );

    expect(result.ok, isFalse);
    expect(result.message, contains('already listening'));
    expect(result.message, contains('not verified'));
  });

  test('readLogTail returns empty for an empty log', () async {
    final file = File(path('empty.log'));
    await file.writeAsString('');
    expect(await LocalManager.readLogTail(file.path), '');
  });

  test('readLogTail returns the last lines, not the first', () async {
    final file = File(path('many.log'));
    await file.writeAsString(
      List.generate(200, (i) => 'line $i').join('\n'),
    );
    final tail = await LocalManager.readLogTail(file.path, maxLines: 5);
    final lines = tail.split('\n');
    expect(lines.length, 5);
    expect(lines.first, 'line 195');
    expect(lines.last, 'line 199');
    expect(tail.contains('line 0'), isFalse);
  });

  test('readLogTail keeps a short log whole', () async {
    final file = File(path('short.log'));
    await file.writeAsString('only failure line');
    expect(await LocalManager.readLogTail(file.path), 'only failure line');
  });

  test('readLogTail drops blank lines', () async {
    final file = File(path('blanks.log'));
    await file.writeAsString('first\n\n\n   \nsecond\n');
    expect(await LocalManager.readLogTail(file.path), 'first\nsecond');
  });

  test('readLogTail survives a torn multi-byte sequence', () async {
    // The supervisor appends in binary mode, so the 64 KiB window can start
    // mid-character. A strict decode would throw and hide the diagnostic.
    final file = File(path('torn.log'));
    await file.writeAsBytes([
      0xE2, 0x9C, // truncated UTF-8 sequence
      ...'\nreal failure line\n'.codeUnits,
    ]);
    final tail = await LocalManager.readLogTail(file.path);
    expect(tail.contains('real failure line'), isTrue);
  });

  test('readLogTail reads only the tail window of a large log', () async {
    final file = File(path('large.log'));
    // Well over the 64 KiB window, so the offset read has to be exercised.
    final filler = List.generate(20000, (i) => 'noise line $i').join('\n');
    await file.writeAsString('$filler\nFINAL MARKER');
    final tail = await LocalManager.readLogTail(file.path, maxLines: 3);
    expect(tail.contains('FINAL MARKER'), isTrue);
    expect(tail.contains('noise line 0'), isFalse);
  });

  test('serverLogPath is absolute and names the supervisor log', () {
    final logPath = LocalManager.serverLogPath();
    expect(logPath, isNotEmpty);
    expect(logPath.endsWith('sonder_serve.log'), isTrue);
    expect(File(logPath).isAbsolute, isTrue);
  });

  test('LocalActionResult carries log detail only when present', () {
    const bare = LocalActionResult(true, 'ok');
    expect(bare.hasLogDetail, isFalse);

    const detailed = LocalActionResult(
      false,
      'failed',
      logPath: '/tmp/sonder_serve.log',
      logTail: 'ERROR: no python',
    );
    expect(detailed.hasLogDetail, isTrue);
    expect(detailed.logTail.contains('no python'), isTrue);
  });

  test('readLogTail returns empty for non-positive maxLines', () async {
    final file = File(path('zero_lines.log'));
    await file.writeAsString('line 1\nline 2\n');
    expect(await LocalManager.readLogTail(file.path, maxLines: 0), '');
    expect(await LocalManager.readLogTail(file.path, maxLines: -5), '');
  });

  test('readLogTail drops torn leading fragment when reading >64 KiB window',
      () async {
    final file = File(path('window_torn.log'));
    // Write >64 KiB of padding followed by a few distinct final lines so the
    // 64 KiB offset lands mid-line on the filler while fewer than maxLines
    // complete lines follow.
    final longFiller = List.filled(40, 'A' * 2000).join('\n');
    await file.writeAsString('$longFiller\nCOMPLETE_1\nCOMPLETE_2\n');
    final tail = await LocalManager.readLogTail(file.path, maxLines: 40);
    final lines = tail.split('\n');
    // The first kept line must be a complete filler line (2000 A's), not a
    // mid-line fragment shorter than 2000 chars.
    expect(lines.first.length, 2000);
    expect(lines.last, 'COMPLETE_2');
  });

  test('readLogTail still returns tail for a single >64 KiB line without newlines',
      () async {
    final file = File(path('single_giant_line.log'));
    await file.writeAsString('${'X' * 70000}TAIL_END');
    final tail = await LocalManager.readLogTail(file.path, maxLines: 5);
    expect(tail, endsWith('TAIL_END'));
    expect(tail.length, lessThanOrEqualTo(64 * 1024));
  });

  test('waitForServer retries past transient probe exceptions with backoff',
      () async {
    var attempts = 0;
    final waits = <Duration>[];

    final reachable = await LocalManager.waitForServer(
      timeout: const Duration(seconds: 10),
      interval: const Duration(milliseconds: 100),
      reachabilityProbe: () async {
        attempts++;
        if (attempts < 3) throw const SocketException('connection refused');
        return true;
      },
      delay: (duration) async => waits.add(duration),
    );

    expect(reachable, isTrue);
    expect(attempts, 3);
    expect(waits, const [
      Duration(milliseconds: 100),
      Duration(milliseconds: 200),
    ]);
  });

  test('waitForServer clamps non-positive interval instead of spinning at zero',
      () async {
    var now = DateTime.utc(2026, 1, 1);
    final waits = <Duration>[];

    final reachable = await LocalManager.waitForServer(
      timeout: const Duration(milliseconds: 200),
      interval: Duration.zero,
      reachabilityProbe: () async => false,
      delay: (duration) async {
        waits.add(duration);
        now = now.add(duration);
      },
      clock: () => now,
    );

    expect(reachable, isFalse);
    expect(waits, isNotEmpty);
    for (final w in waits) {
      expect(w, greaterThan(Duration.zero));
    }
    expect(waits.first, const Duration(milliseconds: 50));
    expect(waits[1], const Duration(milliseconds: 100));
  });

  test('waitForServer aborts early when shouldAbort signals launcher exit',
      () async {
    var attempts = 0;
    final waits = <Duration>[];

    final reachable = await LocalManager.waitForServer(
      timeout: const Duration(seconds: 25),
      interval: const Duration(milliseconds: 400),
      reachabilityProbe: () async {
        attempts++;
        return false;
      },
      shouldAbort: () => attempts >= 2,
      delay: (duration) async => waits.add(duration),
    );

    expect(reachable, isFalse);
    expect(attempts, 2);
    expect(waits, const [Duration(milliseconds: 400)]);
  });

  test('early launcher exit fails fast and preserves stderr output & exit code',
      () async {
    final failing = await Process.start(
      Platform.isWindows ? 'cmd.exe' : '/bin/sh',
      Platform.isWindows
          ? ['/c', 'echo FATAL_BOOT_ERR 1>&2 & exit /b 7']
          : ['-c', 'echo FATAL_BOOT_ERR >&2; exit 7'],
    );
    LocalManager.trackManagedServerForTest(failing);
    addTearDown(LocalManager.stopManagedServerNow);

    final started = DateTime.now();
    final result = await native_mgr.LocalManager.awaitTrackedServerForTest(
      timeout: const Duration(seconds: 25),
      reachabilityProbe: () async => false,
      watchedPid: failing.pid,
    );
    final elapsed = DateTime.now().difference(started);

    expect(result.ok, isFalse);
    expect(elapsed, lessThan(const Duration(seconds: 5)));
    expect(result.message, contains('exited early with code 7'));
    expect(result.logTail, contains('FATAL_BOOT_ERR'));
  });

  test('managed output capture bounds long lines and isolates across restarts',
      () async {
    final first = await Process.start(
      Platform.isWindows ? 'cmd.exe' : '/bin/sh',
      Platform.isWindows
          ? ['/c', 'echo FIRST_RUN_LINE']
          : ['-c', 'echo FIRST_RUN_LINE'],
    );
    LocalManager.trackManagedServerForTest(first);
    await first.exitCode;
    await Future<void>.delayed(const Duration(milliseconds: 100));
    expect(LocalManager.managedServerOutputTail(), contains('FIRST_RUN_LINE'));

    final second = await Process.start(
      Platform.isWindows ? 'cmd.exe' : '/bin/sh',
      Platform.isWindows
          ? ['/c', 'echo SECOND_RUN_LINE']
          : ['-c', 'echo SECOND_RUN_LINE'],
    );
    LocalManager.trackManagedServerForTest(second);
    addTearDown(LocalManager.stopManagedServerNow);
    await second.exitCode;
    await Future<void>.delayed(const Duration(milliseconds: 100));
    final tail = LocalManager.managedServerOutputTail();
    expect(tail, contains('SECOND_RUN_LINE'));
    expect(tail, isNot(contains('FIRST_RUN_LINE')));
  });

  test('isLoopbackHost enforces valid IPv4 octets', () {
    expect(isLoopbackHost('localhost'), isTrue);
    expect(isLoopbackHost('127.0.0.1'), isTrue);
    expect(isLoopbackHost('127.0.0.2'), isTrue);
    expect(isLoopbackHost('127.255.255.254'), isTrue);
    expect(isLoopbackHost('::1'), isTrue);
    expect(isLoopbackHost('[::1]'), isTrue);
    expect(isLoopbackHost('127.999.0.1'), isFalse);
    expect(isLoopbackHost('127.01.0.1'), isFalse);
    expect(isLoopbackHost('192.168.1.1'), isFalse);
  });

  test('sanitizeManagedOutputLine bounds huge lines and strips control chars',
      () {
    final huge = 'A' * 5000;
    final bounded = native_mgr.LocalManager.sanitizeManagedOutputLineForTest(
      huge,
    );
    expect(bounded.length, 2048);
    expect(bounded, endsWith('...'));

    const dirty = 'ok\x00line\x07\tkept\x1b[31m';
    final cleaned = native_mgr.LocalManager.sanitizeManagedOutputLineForTest(
      dirty,
    );
    expect(cleaned, 'okline\tkept[31m');
  });
}
