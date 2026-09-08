import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/app_control.dart';
import 'package:sonder_runtime/app_work_screen.dart';
import 'package:sonder_runtime/theme.dart';
import 'app_control_test.dart' as fixture;
import 'app_work_test.dart' as data;

void main() {
  for (final narrow in [false, true]) {
    testWidgets(
        'actual managed work prepare approval status ${narrow ? 'narrow' : 'wide'}',
        (tester) async {
      tester.view.physicalSize =
          narrow ? const Size(390, 1100) : const Size(1000, 1350);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      await tester.runAsync(() async {
        final icons = FontLoader('MaterialIcons')
          ..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'));
        await icons.load();
        for (final family in ['Sans', 'Mono']) {
          final loader = FontLoader('IBM Plex $family');
          for (final weight in ['Regular', 'Medium', 'SemiBold']) {
            loader.addFont(rootBundle.load('fonts/IBMPlex$family-$weight.ttf'));
          }
          await loader.load();
        }
      });
      var executes = 0;
      final client = AppControlClient(
          context: fixture.context,
          transportFactory: () => MockClient((request) async {
                if (request.url.path.endsWith('/enroll')) {
                  return http.Response(jsonEncode(fixture.enrollment()), 201);
                }
                if (request.url.path.endsWith('/selection')) {
                  return http.Response(
                      jsonEncode({
                        'ok': true,
                        'selection': {
                          'selection_id': 's1',
                          'binding_id': 'b1',
                          'binding_revision': 1,
                          'epoch': 1
                        }
                      }),
                      200);
                }
                if (request.url.path.endsWith('/execute')) {
                  executes++;
                  return http.Response(
                      jsonEncode({
                        'ok': false,
                        'error': {'code': 'APP_WORK_APPROVAL_PENDING'},
                        'pending': {
                          'tool': 'workspace_run',
                          'surface': 'app-control',
                          'call_digest': 'b' * 64,
                          'call_id': 'b' * 16,
                          'expires_at': 4102444800
                        }
                      }),
                      409);
                }
                if (request.method == 'GET') {
                  return http.Response(
                      jsonEncode({
                        'ok': true,
                        'work': data.work(state: 'unknown', revision: 3)
                      }),
                      200);
                }
                final body = jsonDecode(request.body) as Map;
                return http.Response(
                    jsonEncode({
                      'ok': true,
                      'work': data.work(),
                      'receipt': {
                        'command_id': body['command_id'],
                        'action': 'prepare_work',
                        'result_code': 'COMMITTED',
                        'entity_id': 'a' * 64,
                        'entity_revision': 1,
                        'selection_epoch': null
                      }
                    }),
                    200);
              }));
      await client.enroll(project: 'demo', password: 'disposable-preview');
      await client.loadSelection();
      final key = GlobalKey();
      await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home:
              RepaintBoundary(key: key, child: AppWorkScreen(client: client))));
      await tester.enterText(find.byKey(const Key('work-prompt')),
          'Review the repository tests and describe the smallest safe repair.');
      await tester.tap(find.text('Prepare task'));
      await tester.pumpAndSettle();
      expect(executes, 0);
      expect(find.text('Prepared · not started'), findsOneWidget);
      await tester.tap(find.text('Run prepared task'));
      await tester.pumpAndSettle();
      expect(executes, 1);
      expect(find.text('Retry after host approval'), findsOneWidget);
      expect(find.text('Host approval is required before this work can run.'),
          findsOneWidget);
      expect(tester.takeException(), isNull);
      final output = Platform.environment['SONDER_APP_WORK_PREVIEW'];
      if (output != null) {
        await tester.runAsync(() async {
          final boundary =
              key.currentContext!.findRenderObject()! as RenderRepaintBoundary;
          final image = await boundary.toImage(pixelRatio: 1);
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          await File(narrow
                  ? output.replaceFirst(RegExp(r'\.png$'), '-narrow.png')
                  : output)
              .writeAsBytes(bytes!.buffer.asUint8List(), flush: true);
          image.dispose();
        });
      }
      await tester.ensureVisible(find.text('Refresh status'));
      await tester.tap(find.text('Refresh status'));
      await tester.pumpAndSettle();
      expect(find.text('Outcome unknown'), findsOneWidget);
      expect(find.text('Run prepared task'), findsNothing);
      expect(executes, 1);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      client.dispose();
    });
  }
}
