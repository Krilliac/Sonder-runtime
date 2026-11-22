// Admin pages follow the server's allowed_features.host_admin, never a role
// string: the real /v1/sonder/status payload has no top-level `role`.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/theme.dart';

import 'runtime_fixtures.dart';

SystemInfo _info({required bool hostAdmin}) => SystemInfo.fromJson({
      'status': 'ready',
      'allowed_features': {'host_admin': hostAdmin},
    });

Future<void> _pump(WidgetTester tester, SystemInfo info) async {
  tester.view.physicalSize = const Size(1280, 1000);
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  await tester.pumpWidget(MaterialApp(
    theme: SonderTheme.dark,
    home: RuntimeScreen(
      settings: Settings(serverUrl: 'http://192.168.1.20:11435'),
      initialInfo: info,
      liveUpdates: false,
      dataSource: FakeRuntimeData(),
      now: runtimeNow,
    ),
  ));
  await tester.pump(const Duration(milliseconds: 500));
  await tester.pump(const Duration(milliseconds: 500));
}

void main() {
  testWidgets('host_admin true shows the admin pages with no role reported',
      (tester) async {
    final info = _info(hostAdmin: true);
    expect(info.role, isEmpty);
    await _pump(tester, info);
    expect(find.byKey(const Key('category-server')), findsOneWidget);
    expect(find.byKey(const Key('category-memory')), findsOneWidget);
    expect(find.byKey(const Key('category-saas-config')), findsOneWidget);
  });

  testWidgets('host_admin false hides the admin pages', (tester) async {
    await _pump(tester, _info(hostAdmin: false));
    expect(find.byKey(const Key('category-server')), findsNothing);
    expect(find.byKey(const Key('category-memory')), findsNothing);
    expect(find.byKey(const Key('category-saas-config')), findsNothing);
  });
}
