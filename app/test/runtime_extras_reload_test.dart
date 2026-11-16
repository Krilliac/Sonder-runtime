import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/theme.dart';

import 'runtime_fixtures.dart';

class _GatedRuntimeData extends FakeRuntimeData {
  final Completer<void> gate = Completer<void>();
  @override
  Future<List<WorkRun>> workRuns() async {
    await super.workRuns();
    await gate.future;
    return runs;
  }
}

Widget _app(FakeRuntimeData data, Settings settings) => MaterialApp(
      theme: SonderTheme.dark,
      home: RuntimeScreen(
        settings: settings,
        liveUpdates: false,
        dataSource: data,
        now: runtimeNow,
      ),
    );

void main() {
  testWidgets('W7: an identity change during an extras load reloads',
      (tester) async {
    tester.view.physicalSize = const Size(1280, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final oldData = _GatedRuntimeData();
    final newData = FakeRuntimeData();
    await tester
        .pumpWidget(_app(oldData, Settings(serverUrl: 'http://a:11435')));
    await tester.pump();
    expect(oldData.workRunReads, 1);

    await tester
        .pumpWidget(_app(newData, Settings(serverUrl: 'http://b:11435')));
    await tester.pump();
    oldData.gate.complete();
    await tester.pumpAndSettle();
    expect(newData.workRunReads, greaterThanOrEqualTo(1),
        reason: 'the new identity must be loaded after the stale load ends');
  });
}
