import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/composer.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/permission_mode.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/sheet.dart' show ToneButton;

import 'chat_fakes.dart';
import 'runtime_fixtures.dart';

void main() {
  group('PermissionMode.bypass', () {
    test('PermissionMode parses can_bypass and defaults to false', () {
      expect(PermissionMode.bypass, 'bypass');
      final restricted = PermissionMode.fromJson({'mode': 'manual'});
      expect(restricted.canBypass, isFalse);

      final admin = PermissionMode.fromJson({
        'mode': 'manual',
        'can_bypass': true,
      });
      expect(admin.canBypass, isTrue);
    });

    test('setPermissionMode sends confirm_bypass: true when confirmed', () async {
      final seen = <http.Request>[];
      await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435', apiKey: 'k')
            .setPermissionMode(PermissionMode.bypass, confirmBypass: true),
        () => MockClient((r) async {
          seen.add(r);
          return http.Response(
              jsonEncode({'mode': 'bypass', 'can_bypass': true}), 200);
        }),
      );
      expect(seen, hasLength(1));
      expect(seen.single.url.path, '/v1/permission-mode');
      expect(
        jsonDecode(seen.single.body),
        {'mode': 'bypass', 'confirm_bypass': true},
      );
    });

    test('canonicalMode recognises bypass and isModeRaise treats it as highest',
        () {
      expect(canonicalMode('bypass'), 'bypass');
      expect(resolvePermissionMode('BYPASS'), 'bypass');
      expect(resolvePermissionMode('by'), 'bypass');
      expect(isModeRaise('auto', 'bypass'), isTrue);
      expect(isModeRaise('manual', 'bypass'), isTrue);
      expect(isModeRaise('bypass', 'auto'), isFalse);
      expect(isModeRaise('bypass', 'manual'), isFalse);
    });

    testWidgets('RaiseModeSheet requires explicit checkbox ack for bypass',
        (tester) async {
      tester.view.physicalSize = const Size(420, 840);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);

      late BuildContext ctx;
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: Builder(builder: (c) {
            ctx = c;
            return const SizedBox();
          }),
        ),
      ));

      final result =
          confirmModeChange(ctx, from: 'auto', to: 'bypass', host: 'mypc');
      await tester.pumpAndSettle();

      expect(find.byType(RaiseModeSheet), findsOneWidget);
      expect(find.text('auto → bypass'), findsOneWidget);
      expect(find.byKey(const Key('raise-mode-bypass-ack')), findsOneWidget);

      // Confirm button is disabled until checkbox is ticked.
      final btnBefore = tester
          .widget<ToneButton>(find.byKey(const Key('raise-mode-confirm')));
      expect(btnBefore.onPressed, isNull);

      await tester.tap(find.byKey(const Key('raise-mode-bypass-ack')));
      await tester.pumpAndSettle();

      final btnAfter = tester
          .widget<ToneButton>(find.byKey(const Key('raise-mode-confirm')));
      expect(btnAfter.onPressed, isNotNull);

      await tester.tap(find.byKey(const Key('raise-mode-confirm')));
      await tester.pumpAndSettle();
      expect(await result, isTrue);
    });

    testWidgets('PermissionModeDialog hides bypass when canBypass is false',
        (tester) async {
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: PermissionModeDialog(
            state: permissionModeFor('manual', canBypass: false),
          ),
        ),
      ));
      expect(find.byKey(const Key('permission-mode-option-auto')),
          findsOneWidget);
      expect(find.byKey(const Key('permission-mode-option-bypass')),
          findsNothing);

      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: PermissionModeDialog(
            state: permissionModeFor('manual', canBypass: true),
          ),
        ),
      ));
      expect(find.byKey(const Key('permission-mode-option-bypass')),
          findsOneWidget);
    });

    testWidgets(
        'ChatController refuses bypass when !canBypass, never Shift+Tab cycles '
        'into bypass, and passes confirmBypass when confirmed', (tester) async {
      SharedPreferences.setMockInitialValues(<String, Object>{});
      ChatStore.backend = PrefsChatStoreBackend();
      ChatStore.resetCache();

      final backend = FakeChatBackend()
        ..mode = permissionModeFor('auto', canBypass: false);
      final c = ChatController(backend, model: 'sonder');
      await c.start();
      await tester.pump();

      // Shift+Tab cycle skips bypass.
      expect(c.nextModeInCycle(), 'plan');

      // Non-admin / !canBypass refuses locally before posting.
      final (deniedOutcome, _) = await c.requestModeChange(
        'bypass',
        confirm: (_, __) async => true,
      );
      expect(deniedOutcome, ModeChangeOutcome.readOnly);
      expect(backend.modeRequests, isEmpty);

      // Admin with canBypass: true sends confirmBypass: true.
      backend.mode = permissionModeFor('auto', canBypass: true);
      await c.refreshPermissionMode();
      expect(c.nextModeInCycle(), 'plan');

      final (okOutcome, _) = await c.requestModeChange(
        'bypass',
        confirm: (_, __) async => true,
      );
      expect(okOutcome, ModeChangeOutcome.changed);
      expect(backend.modeRequests, [('bypass', true)]);
      c.dispose();
    });

    testWidgets(
        'PermissionsPage shows bypass row for admin and hides/disables for '
        'non-admin user accounts', (tester) async {
      tester.view.physicalSize = const Size(1280, 1000);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);

      final adminData = FakeRuntimeData()
        ..mode = permissionModeFor('bypass', canBypass: true);
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: RuntimeScreen(
          settings: Settings(),
          initialInfo: healthySystemInfo(),
          liveUpdates: false,
          dataSource: adminData,
          initialCategory: 'permissions',
          onChangePermissionMode: () {},
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('permission-bypass-active')), findsOneWidget);
      expect(find.byKey(const Key('permission-bypass-row')), findsOneWidget);

      final userData = FakeRuntimeData()
        ..mode = permissionModeFor('manual', canBypass: false)
        ..approvalsError = SonderException('Forbidden', httpStatus: 403);
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: RuntimeScreen(
          settings: Settings(),
          initialInfo: healthySystemInfo(),
          liveUpdates: false,
          dataSource: userData,
          initialCategory: 'permissions',
          onChangePermissionMode: () {},
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('permission-bypass-row')), findsNothing);
      final changeBtn = tester.widget<OutlinedButton>(
          find.byKey(const Key('permission-mode-change')));
      expect(changeBtn.onPressed, isNull);
    });
  });
}
