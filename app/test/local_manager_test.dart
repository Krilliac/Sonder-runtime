import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/local_manager.dart';

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
}
