import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/local_manager_web.dart';

void main() {
  test('browser local capabilities are unavailable without invented host paths',
      () async {
    expect(LocalManager.canRunLocalTools, isFalse);
    expect(LocalManager.platformLabel, 'Web browser');
    final info = await LocalManager.inspect();
    expect(info.canLaunch, isFalse);
    expect(info.sharedHome, isEmpty);
    expect(info.systemDir, isEmpty);
    expect(info.defaultServerReachable, isFalse);
    expect(LocalManager.processEnvironment(), isEmpty);
    expect(await LocalManager.readServerLogTail(), isEmpty);
    var probed = false;
    final start =
        await LocalManager.startServer(managedReachabilityProbe: () async {
      probed = true;
      return true;
    });
    expect(start.ok, isFalse);
    expect(probed, isFalse);
    for (final result in [
      await LocalManager.setupEngine(),
      await LocalManager.stopServers(),
      await LocalManager.startEndlessTraining(),
      await LocalManager.updateFromGit()
    ]) {
      expect(result.ok, isFalse);
      expect(result.message, contains('unavailable in the browser'));
    }
    LocalManager.stopManagedServerNow();
  });

  // A non-loopback companion URL returns before any network call, so a
  // result without needsApproval means the local gate let the tool through.
  Future<ClientToolExecResult> exec(String tool, String risk, String mode) =>
      LocalManager.executeClientTool(
        callId: 'c',
        tool: tool,
        args: const {},
        risk: risk,
        mode: mode,
        companionUrl: 'http://example.com',
      );

  test('a declared risk cannot lower the web gate below the local class',
      () async {
    for (final (tool, risk, mode) in [
      ('run_program', 'read_only', 'manual'),
      ('run_program', 'write', 'acceptEdits'),
      ('file_write', 'read_only', 'manual'),
      ('file_delete', 'read_only', 'plan'),
    ]) {
      final res = await exec(tool, risk, mode);
      expect(res.needsApproval, isTrue, reason: '$tool $risk $mode');
      expect(res.ok, isFalse, reason: '$tool $risk $mode');
    }
  });

  test('a declared risk can raise the web gate', () async {
    final raised = await exec('file_read', 'execute', 'acceptEdits');
    expect(raised.needsApproval, isTrue);
    // Control: the same tool at its own class passes the gate and stops at
    // the companion URL check, so the prompts above are the gate.
    final control = await exec('file_read', 'read_only', 'manual');
    expect(control.needsApproval, isFalse);
    expect(control.companionUnavailable, isTrue);
  });
}
