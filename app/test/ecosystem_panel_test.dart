// The Inference & Observatory panel on the Runtime page: every state shows
// its word as text, the actions call the launcher with sanitised URLs, and
// the panel is reachable by keyboard and screen reader.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/local_manager.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/theme.dart';

import 'runtime_fixtures.dart';

EcosystemReading _reading(Map<String, dynamic> json) =>
    EcosystemReading.parse(jsonDecode(jsonEncode(json)));

class _Launches {
  final calls = <List<String>>[];
  ObservatoryLaunchResult result = const ObservatoryLaunchResult(
    ok: true,
    mode: ObservatoryLaunchMode.executable,
    message: 'Opened the Observatory with 2 producers.',
  );

  Future<ObservatoryLaunchResult> call(List<String> urls) async {
    calls.add(urls);
    return result;
  }
}

Future<void> _pump(
  WidgetTester tester, {
  EcosystemReading? reading,
  Object? error,
  bool loading = false,
  String runtimeUrl = 'http://127.0.0.1:11435',
  bool canStartProcesses = true,
  _Launches? launches,
  ThemeData? theme,
  Size size = const Size(1000, 1400),
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  await tester.pumpWidget(MaterialApp(
    theme: theme ?? SonderTheme.dark,
    home: Scaffold(
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: EcosystemPanel(
          reading: reading,
          error: error,
          loading: loading,
          runtimeUrl: runtimeUrl,
          canStartProcesses: canStartProcesses,
          onLaunch: (launches ?? _Launches()).call,
        ),
      ),
    ),
  ));
  await tester.pumpAndSettle();
}

/// Records Clipboard.setData calls.
List<String> _captureClipboard(WidgetTester tester) {
  final copied = <String>[];
  tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(SystemChannels.platform, (call) async {
    if (call.method == 'Clipboard.setData') {
      copied.add((call.arguments as Map)['text'] as String);
    }
    return null;
  });
  addTearDown(() => tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(SystemChannels.platform, null));
  return copied;
}

void main() {
  testWidgets('ready + synthetic: state word, SYNTHETIC chip, identity',
      (tester) async {
    final copied = _captureClipboard(tester);
    await _pump(tester, reading: _reading(ecosystemReadySynthetic()));
    final state = find.byKey(const Key('ecosystem-inference-state'));
    expect(find.descendant(of: state, matching: find.text('ready')),
        findsOneWidget);
    expect(find.text('SYNTHETIC'), findsOneWidget);
    expect(find.text('mock · mock:tiny · none · ctx 4096'), findsOneWidget);
    expect(find.text('9f86d081884c…'), findsOneWidget);
    expect(find.text('0.4.0 · API v1'), findsOneWidget);
    expect(find.text('http://127.0.0.1:11437'), findsOneWidget);
    expect(find.text('mock:tiny'), findsOneWidget);
    expect(
        find.text('No fallback: requests fail while Sonder Inference is down.'),
        findsOneWidget);
    // Bindings: default, embedding and one chip per tier.
    expect(find.byKey(const Key('ecosystem-tier-code')), findsOneWidget);
    expect(find.text('code · Sonder Inference'), findsOneWidget);
    // Export on, with its counters.
    final export = find.byKey(const Key('ecosystem-export'));
    expect(
        find.descendant(of: export, matching: find.text('ok')), findsOneWidget);
    expect(
        find.text('1 subscribers · 1204 emitted · 0 dropped · 512/4096 '
            'retained'),
        findsOneWidget);
    // The full digest is what gets copied.
    await tester.tap(find.byTooltip('Copy model digest'));
    await tester.pump();
    expect(copied.single, mockIdentityJson()['model_digest']);
    expect(find.text('Copied the model digest.'), findsOneWidget);
  });

  testWidgets('unavailable without fallback says requests fail',
      (tester) async {
    await _pump(tester, reading: _reading(ecosystemUnavailable()));
    final state = find.byKey(const Key('ecosystem-inference-state'));
    expect(find.descendant(of: state, matching: find.text('unavailable')),
        findsOneWidget);
    expect(
        find.text('No fallback: requests fail while Sonder Inference is down.'),
        findsOneWidget);
    expect(find.text('SYNTHETIC'), findsNothing);
    expect(find.text('not measured'), findsOneWidget);
  });

  testWidgets('all-Ollama: Sonder Inference not configured, with env hint',
      (tester) async {
    await _pump(tester, reading: _reading(ecosystemAllOllama()));
    expect(find.text('not configured'), findsOneWidget);
    expect(
        find.textContaining('Sonder Inference not configured'), findsOneWidget);
    expect(find.text(inferenceEnvHint), findsOneWidget);
    expect(find.byKey(const Key('ecosystem-fallback')), findsNothing);
  });

  testWidgets('fallback configured names what Ollama serves', (tester) async {
    await _pump(tester, reading: _reading(ecosystemFallback()));
    expect(
        find.text('Ollama fallback: Ollama serves only requests that never '
            'reached Sonder Inference (used 2 times).'),
        findsOneWidget);
  });

  testWidgets('export disabled reads off', (tester) async {
    await _pump(tester, reading: _reading(ecosystemExportDisabled()));
    final export = find.byKey(const Key('ecosystem-export'));
    expect(find.descendant(of: export, matching: find.text('off')),
        findsOneWidget);
    expect(
        find.text('Runtime live export is off (SONDER_OBSERVATORY_EXPORT=0).'),
        findsOneWidget);
    // Inference still publishes telemetry, so it remains connectable.
    expect(find.text('http://127.0.0.1:11437'), findsWidgets);
  });

  testWidgets('warnings (such as CORS) are shown as warn notices',
      (tester) async {
    await _pump(
      tester,
      reading: _reading(ecosystemJson(
        inference: inferenceStatusJson(),
        corsOrigins: const [],
        warnings: const [
          'SONDER_OBSERVATORY_ORIGINS is empty: a browser Observatory cannot '
              'read the runtime stream.',
        ],
      )),
    );
    expect(find.textContaining('SONDER_OBSERVATORY_ORIGINS is empty'),
        findsOneWidget);
    expect(
        find.textContaining('none: browsers on other origins'), findsOneWidget);
  });

  for (final status in [401, 403]) {
    testWidgets('$status: administrator authorization is required',
        (tester) async {
      await _pump(tester,
          error: SonderException('Administrator authorization is required.',
              httpStatus: status));
      expect(find.byKey(const Key('ecosystem-admin-required')), findsOneWidget);
      expect(find.text('Administrator authorization is required.'),
          findsOneWidget);
    });
  }

  testWidgets('404: unsupported runtime', (tester) async {
    await _pump(tester, reading: const EcosystemReading.unsupportedRuntime());
    expect(find.byKey(const Key('ecosystem-unsupported')), findsOneWidget);
    expect(find.textContaining('does not report Sonder Inference'),
        findsOneWidget);
  });

  testWidgets('unknown schema: unsupported format, no crash', (tester) async {
    await _pump(tester, reading: _reading(ecosystemUnknownSchema()));
    expect(
        find.byKey(const Key('ecosystem-unsupported-schema')), findsOneWidget);
    expect(find.textContaining('sonder.runtime.ecosystem/2'), findsOneWidget);
  });

  testWidgets('unknown provider state reads unknown', (tester) async {
    await _pump(tester,
        reading: _reading(
            ecosystemJson(inference: inferenceStatusJson(state: 'rebooting'))));
    final state = find.byKey(const Key('ecosystem-inference-state'));
    expect(find.descendant(of: state, matching: find.text('unknown')),
        findsOneWidget);
  });

  testWidgets('loading, empty and error states', (tester) async {
    await _pump(tester, loading: true);
    expect(find.byKey(const Key('ecosystem-loading')), findsOneWidget);
    expect(find.text('working'), findsOneWidget);
    await _pump(tester);
    expect(find.byKey(const Key('ecosystem-empty')), findsOneWidget);
    await _pump(tester, error: SonderException('Cannot reach server: refused'));
    expect(find.byKey(const Key('ecosystem-error')), findsOneWidget);
    expect(find.text('Cannot reach server: refused'), findsOneWidget);
  });

  testWidgets('Open Observatory passes the connect URLs; Copy copies them',
      (tester) async {
    final copied = _captureClipboard(tester);
    final launches = _Launches();
    await _pump(tester,
        reading: _reading(ecosystemReadySynthetic()), launches: launches);
    await tester.tap(find.byKey(const Key('ecosystem-open-observatory')));
    await tester.pumpAndSettle();
    expect(launches.calls.single,
        ['http://127.0.0.1:11435', 'http://127.0.0.1:11437']);
    expect(
        find.text('Opened the Observatory with 2 producers.'), findsOneWidget);
    expect(find.text('✓ done'), findsOneWidget);

    await tester.tap(find.byKey(const Key('ecosystem-copy-urls')));
    await tester.pump();
    expect(copied.single, 'http://127.0.0.1:11435\nhttp://127.0.0.1:11437');
  });

  testWidgets('web: a link to copy instead of a process', (tester) async {
    final copied = _captureClipboard(tester);
    const link = 'http://127.0.0.1:4173/?fixture=0'
        '&connect=http%3A%2F%2F127.0.0.1%3A11435';
    final launches = _Launches()
      ..result = const ObservatoryLaunchResult(
        ok: false,
        mode: ObservatoryLaunchMode.unavailable,
        message: 'The browser cannot start the Observatory.',
        url: link,
      );
    await _pump(tester,
        reading: _reading(ecosystemReadySynthetic()),
        launches: launches,
        canStartProcesses: false);
    await tester.tap(find.text('Get Observatory link'));
    await tester.pumpAndSettle();
    expect(find.text(link), findsOneWidget);
    await tester.tap(find.byKey(const Key('ecosystem-copy-link')));
    await tester.pump();
    expect(copied.single, link);
  });

  testWidgets('a non-loopback runtime disables launching and says why',
      (tester) async {
    final launches = _Launches();
    await _pump(tester,
        reading: _reading(ecosystemReadySynthetic()),
        launches: launches,
        runtimeUrl: 'http://192.168.1.20:11435');
    final button = tester.widget<ButtonStyleButton>(
        find.byKey(const Key('ecosystem-open-observatory')));
    expect(button.onPressed, isNull);
    expect(find.text(observatoryRemoteExplanation), findsOneWidget);
    // Copying the URLs stays available.
    final copy = tester.widget<ButtonStyleButton>(
        find.byKey(const Key('ecosystem-copy-urls')));
    expect(copy.onPressed, isNotNull);
  });

  testWidgets('no connect URLs disables launching', (tester) async {
    await _pump(tester,
        reading: _reading(ecosystemJson(
            provider: 'ollama', exportEnabled: false, connectUrls: const [])));
    final button = tester.widget<ButtonStyleButton>(
        find.byKey(const Key('ecosystem-open-observatory')));
    expect(button.onPressed, isNull);
    expect(find.byKey(const Key('ecosystem-no-urls')), findsOneWidget);
  });

  testWidgets('semantics: words, not colours, and a labelled chip',
      (tester) async {
    final handle = tester.ensureSemantics();
    await _pump(tester, reading: _reading(ecosystemReadySynthetic()));
    expect(find.bySemanticsLabel(RegExp(r'^ready, Sonder Inference: ready')),
        findsOneWidget);
    expect(find.bySemanticsLabel(RegExp(r'^Synthetic: mock backend output')),
        findsOneWidget);
    expect(find.bySemanticsLabel('Tier fast uses Sonder Inference'),
        findsOneWidget);
    expect(
        find.bySemanticsLabel(RegExp(r'^ok, Live export: ')), findsOneWidget);
    handle.dispose();
  });

  testWidgets('Open Observatory is reachable and operable by keyboard',
      (tester) async {
    final launches = _Launches();
    await _pump(tester,
        reading: _reading(ecosystemReadySynthetic()), launches: launches);
    final target = find.byKey(const Key('ecosystem-open-observatory'));
    var focused = false;
    for (var i = 0; i < 40 && !focused; i++) {
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      final context = FocusManager.instance.primaryFocus?.context;
      focused = context != null &&
          find
              .descendant(of: target, matching: find.byWidget(context.widget))
              .evaluate()
              .isNotEmpty;
    }
    expect(focused, isTrue, reason: 'Tab never reached Open Observatory');
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pumpAndSettle();
    expect(launches.calls, hasLength(1));
  });

  testWidgets('light theme and phone width render without overflow',
      (tester) async {
    await _pump(tester,
        reading: _reading(ecosystemFallback()),
        theme: SonderTheme.light,
        size: const Size(390, 2400));
    expect(tester.takeException(), isNull);
    expect(find.text('unavailable'), findsOneWidget);
  });
}
