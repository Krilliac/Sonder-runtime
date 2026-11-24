import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/local_manager.dart';

bool _alive(int pid) {
  final out = Process.runSync('tasklist', ['/FI', 'PID eq $pid', '/NH']);
  return '${out.stdout}'.contains('$pid');
}

void main() {
  test('closing the app kills the launcher shell and its child server',
      () async {
    final tmp = await Directory.systemTemp.createTemp('sonder_kill_test');
    addTearDown(() => tmp.delete(recursive: true));
    final pidFile = File('${tmp.path}${Platform.pathSeparator}child.pid');
    final launcher = await Process.start('cmd.exe', [
      '/c',
      'powershell',
      '-NoProfile',
      '-Command',
      r'$PID | Out-File -Encoding ascii '
          "'${pidFile.path}'; Start-Sleep -Seconds 120",
    ]);
    addTearDown(() => launcher.kill());
    for (var i = 0; i < 250 && !pidFile.existsSync(); i++) {
      await Future<void>.delayed(const Duration(milliseconds: 100));
    }
    final childText = pidFile.existsSync() ? pidFile.readAsStringSync() : '';
    final childPid = int.tryParse(childText.trim());
    expect(childPid, isNotNull, reason: 'child server never started');
    expect(_alive(childPid!), isTrue);

    LocalManager.trackManagedServerForTest(launcher);
    LocalManager.stopManagedServerNow();
    await Future<void>.delayed(const Duration(milliseconds: 500));

    expect(_alive(childPid), isFalse);
  }, skip: Platform.isWindows ? false : 'Windows tree-kill check');
}
