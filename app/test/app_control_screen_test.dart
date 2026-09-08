import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/app_control.dart';
import 'package:sonder_runtime/app_control_screen.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/chat_screen.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'app_control_test.dart' as fixture;

void main() {
  testWidgets(
      'narrow Chat opens app control without passing control to generic API',
      (tester) async {
    SharedPreferences.setMockInitialValues({});
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: ChatScreen(
            settings: Settings(
                serverUrl: 'https://host.test',
                accountSession: fixture.context().account),
            onSettingsChanged: (_) {},
          )));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.tap(find.byTooltip('Server conversations'));
      await tester.pumpAndSettle();
      expect(find.byType(AppControlScreen), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
        () => MockClient((request) async {
              expect(
                  request.headers.containsKey('X-Sonder-App-Control'), isFalse);
              return http.Response('{}', 200);
            }));
  });
  testWidgets(
      'unknown enrollment requires explicit same request and fresh step-up',
      (tester) async {
    tester.view.physicalSize = const Size(900, 1500);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final commands = <String>[];
    final client = AppControlClient(
        context: fixture.context,
        transportFactory: () => MockClient((r) async {
              if (r.url.path.endsWith('/enroll')) {
                commands
                    .add((jsonDecode(r.body) as Map)['command_id'] as String);
                if (commands.length == 1) {
                  return http.Response(
                      '{"ok":false,"error":{"code":"APP_CONTROL_OUTCOME_UNKNOWN"}}',
                      503);
                }
                if (commands.length == 2) {
                  return http.Response(
                      '{"ok":false,"error":{"code":"CREDENTIAL_DELIVERY_UNKNOWN"}}',
                      409);
                }
                return http.Response(jsonEncode(fixture.enrollment()), 201);
              }
              return http.Response(
                  '{"ok":false,"error":{"code":"APP_CONTROL_UNAVAILABLE"}}',
                  503);
            }));
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AppControlScreen(client: client, initialProject: 'demo')));
    await tester.enterText(
        find.byKey(const Key('control-password')), 'private-stepup');
    await tester.tap(find.widgetWithText(FilledButton, 'Enable app control'));
    await tester.pumpAndSettle();
    expect(commands.length, 1);
    expect(find.textContaining('outcome is unknown'), findsOneWidget);
    expect(
        tester
            .widget<TextFormField>(find.byKey(const Key('control-password')))
            .controller!
            .text,
        isEmpty);
    await tester.enterText(
        find.byKey(const Key('control-password')), 'private-stepup');
    await tester.tap(find.text('Check same enrollment'));
    await tester.pumpAndSettle();
    expect(commands[0], commands[1]);
    expect(find.textContaining('cannot be recovered'), findsOneWidget);
    await tester.enterText(
        find.byKey(const Key('control-password')), 'fresh-stepup');
    await tester.tap(find.widgetWithText(FilledButton, 'Enable app control'));
    await tester.pumpAndSettle();
    expect(commands[2], isNot(commands[0]));
    expect(find.text('No server conversations yet'), findsNothing);
    expect(find.textContaining('Conversations have not been loaded'),
        findsOneWidget);
    expect(find.textContaining('private-stepup'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
    client.dispose();
  });

  testWidgets(
      'narrow large-text step-up keeps validation and controls reachable',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    var calls = 0;
    final client = AppControlClient(
        context: fixture.context,
        transportFactory: () => MockClient((r) async {
              calls++;
              return http.Response('{}', 500);
            }));
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        builder: (context, child) => MediaQuery(
            data: MediaQuery.of(context)
                .copyWith(textScaler: const TextScaler.linear(2)),
            child: child!),
        home: AppControlScreen(client: client, initialProject: 'bad project')));
    final enable = find.widgetWithText(FilledButton, 'Enable app control');
    await tester.scrollUntilVisible(enable, 250,
        scrollable: find.byType(Scrollable).first);
    await tester.pumpAndSettle();
    await tester.ensureVisible(enable);
    await tester.pumpAndSettle();
    expect(tester.getCenter(enable).dy, lessThan(844));
    await tester.tap(enable);
    await tester.pumpAndSettle();
    expect(calls, 0);
    expect(find.text('Enter your account password.'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    client.dispose();
  });

  testWidgets('explicit enrollment creates and selects server conversation',
      (tester) async {
    tester.view.physicalSize = const Size(1000, 1600);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    var created = false, selected = false, cleared = false, revoked = false;
    final client = AppControlClient(
        context: fixture.context,
        transportFactory: () => MockClient((r) async {
              final path = r.url.path.split('/').last;
              if (path == 'enroll') {
                return http.Response(jsonEncode(fixture.enrollment()), 201);
              }
              if (r.method == 'POST') {
                final body = jsonDecode(r.body) as Map;
                if (path == 'bindings') created = true;
                if (path == 'select') {
                  selected = true;
                  expect(body['binding_id'], 'binding-1');
                  expect(body['expected_epoch'], 0);
                }
                if (path == 'clear') {
                  cleared = true;
                  selected = false;
                  expect(body['expected_epoch'], 1);
                }
                if (path == 'revoke') {
                  revoked = true;
                  expect(body['expected_revision'], 1);
                }
                return http.Response(
                    jsonEncode({
                      'ok': true,
                      'receipt': {
                        'command_id': body['command_id'],
                        'action': {
                          'bindings': 'create_binding',
                          'select': 'select_binding',
                          'clear': 'clear_selection',
                          'revoke': 'revoke_binding'
                        }[path],
                        'result_code': 'COMMITTED',
                        'entity_id': path == 'bindings' || path == 'revoke'
                            ? 'binding-1'
                            : 'selection-1',
                        'entity_revision': path == 'clear'
                            ? null
                            : path == 'revoke'
                                ? 2
                                : 1,
                        'selection_epoch':
                            path == 'bindings' || path == 'revoke'
                                ? null
                                : path == 'clear'
                                    ? 2
                                    : 1
                      }
                    }),
                    200);
              }
              if (path == 'selection') {
                return http.Response(
                    jsonEncode({
                      'ok': true,
                      'selection': selected
                          ? {
                              'selection_id': 'selection-1',
                              'binding_id': 'binding-1',
                              'binding_revision': 1,
                              'epoch': 1
                            }
                          : cleared
                              ? {
                                  'selection_id': 'selection-1',
                                  'epoch': 2,
                                  'binding_id': null,
                                  'binding_revision': null
                                }
                              : null
                    }),
                    200);
              }
              return http.Response(
                  jsonEncode({
                    'ok': true,
                    'items': created
                        ? [
                            {
                              'binding_id': 'binding-1',
                              'host_conversation_id': 'app-session:binding-1',
                              'project': 'demo',
                              'title': 'Review the renderer',
                              'local_history_alias': '',
                              'revision': revoked ? 2 : 1,
                              'expires_at': 4102444800,
                              'revoked': revoked
                            }
                          ]
                        : [],
                    'next_position': null
                  }),
                  200);
            }));
    addTearDown(client.dispose);
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AppControlScreen(client: client, initialProject: 'demo')));
    await tester.enterText(
        find.byKey(const Key('control-password')), 'password');
    await tester.tap(find.widgetWithText(FilledButton, 'Enable app control'));
    await tester.pumpAndSettle();
    expect(find.text('No server conversations yet'), findsOneWidget);
    await tester.enterText(
        find.byKey(const Key('control-title')), 'Review the renderer');
    await tester.tap(find.text('Create conversation'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(OutlinedButton, 'Select'));
    await tester.pumpAndSettle();
    expect(selected, isTrue);
    expect(find.text('Selected conversation'), findsOneWidget);
    expect(find.textContaining('Managed work requires separate server support'),
        findsOneWidget);
    expect(find.textContaining(fixture.controlToken), findsNothing);
    await tester.tap(find.text('Clear selection'));
    await tester.pumpAndSettle();
    expect(cleared, isTrue);
    expect(find.text('No conversation selected'), findsOneWidget);
    await tester.tap(find.text('Revoke'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(revoked, isFalse);
    await tester.tap(find.text('Revoke'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Revoke conversation'));
    await tester.pumpAndSettle();
    expect(revoked, isTrue);
    expect(find.text('Revoked'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    client.dispose();
  });
}
