import 'package:flutter_test/flutter_test.dart';
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
}
