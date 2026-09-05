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
import 'package:sonder_runtime/app_control_screen.dart';
import 'package:sonder_runtime/theme.dart';
import 'app_control_test.dart' as fixture;

void main() {
  for (final narrow in [false, true]) {
    testWidgets(
        'real app-control widget and bundled fonts ${narrow ? 'narrow' : 'wide'}',
        (tester) async {
      tester.view.physicalSize =
          narrow ? const Size(390, 844) : const Size(1000, 1600);
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
      final client = AppControlClient(
          context: fixture.context,
          transportFactory: () => MockClient((r) async {
                if (r.url.path.endsWith('/enroll')) {
                  return http.Response(jsonEncode(fixture.enrollment()), 201);
                }
                if (r.url.path.endsWith('/selection')) {
                  return http.Response(
                      jsonEncode({
                        'ok': true,
                        'selection': {
                          'selection_id': 'selection-demo',
                          'binding_id': 'renderer-review',
                          'binding_revision': 1,
                          'epoch': 1
                        }
                      }),
                      200);
                }
                return http.Response(
                    jsonEncode({
                      'ok': true,
                      'items': [
                        for (final row in [
                          ('renderer-review', 'Review the renderer'),
                          ('workspace-notes', 'Workspace notes')
                        ])
                          {
                            'binding_id': row.$1,
                            'host_conversation_id': 'app-session:${row.$1}',
                            'project': 'workbench',
                            'title': row.$2,
                            'local_history_alias': '',
                            'revision': 1,
                            'expires_at': 4102444800,
                            'revoked': false
                          }
                      ],
                      'next_position': null
                    }),
                    200);
              }));
      final key = GlobalKey();
      await client.enroll(
          project: 'workbench', password: 'disposable-preview-password');
      await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: RepaintBoundary(
              key: key,
              child: AppControlScreen(
                  client: client, initialProject: 'workbench'))));
      await tester.pumpAndSettle();
      expect(find.text('Selected conversation'), findsOneWidget);
      expect(tester.takeException(), isNull);
      final output = Platform.environment['SONDER_APP_CONTROL_PREVIEW'];
      if (output != null) {
        await tester.runAsync(() async {
          final boundary =
              key.currentContext!.findRenderObject()! as RenderRepaintBoundary;
          final image = await boundary.toImage(pixelRatio: 1);
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          final file = File(narrow
              ? output.replaceFirst(RegExp(r'\.png$'), '-narrow.png')
              : output);
          await file.writeAsBytes(bytes!.buffer.asUint8List(), flush: true);
          image.dispose();
        });
      }
      await tester.pumpWidget(const SizedBox.shrink());
      client.dispose();
    });
  }
}
