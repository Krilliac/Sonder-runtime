// Compile with dart compile js -O2 and run the output in a browser or Node.
// Import the production facade: this must select the browser implementation.
import 'package:sonder_runtime/local_manager.dart';

Future<void> main() async {
  if (LocalManager.canRunLocalTools ||
      LocalManager.platformLabel != 'Web browser') {
    throw StateError('Browser imported native local capabilities');
  }
  for (var refresh = 0; refresh < 4; refresh++) {
    final info = await LocalManager.inspect();
    if (info.canLaunch ||
        info.sharedHome.isNotEmpty ||
        info.systemDir.isNotEmpty) {
      throw StateError('Browser invented a local installation');
    }
    if (LocalManager.sharedHomePath().isNotEmpty ||
        await LocalManager.defaultServerReachable() ||
        await LocalManager.waitForServer()) {
      throw StateError('Browser invented native host access');
    }
  }
  for (final result in [
    await LocalManager.setupEngine(),
    await LocalManager.startServer(),
    await LocalManager.stopServers(),
    await LocalManager.startEndlessTraining(),
    await LocalManager.updateFromGit()
  ]) {
    if (result.ok) {
      throw StateError('Browser reported a native action succeeded');
    }
  }
  LocalManager.stopManagedServerNow();
  print(
      'PASS: production browser LocalManager imports no native platform APIs; repeated inspect, startup and lifecycle calls are safe.');
}
