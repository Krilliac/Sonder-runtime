import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/system_screen.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/workspace_ui.dart';

void main() {
  test('read recovery distinguishes account, protocol and transient failures',
      () {
    for (final status in [401, 403]) {
      final failure = RequestFailure.read(
          SonderException('denied', httpStatus: status),
          resource: 'conversations');
      expect(failure.settingsRequired, isTrue);
      expect(failure.retryable, isFalse);
    }
    expect(
        RequestFailure.read(const FormatException(), resource: 'conversations')
            .retryable,
        isFalse);
    expect(
        RequestFailure.read(SonderException('missing', httpStatus: 404),
                resource: 'conversations')
            .retryable,
        isFalse);
    expect(
        RequestFailure.read(TimeoutException('timeout'),
                resource: 'conversations')
            .retryable,
        isTrue);
    final busy = RequestFailure.read(
        SonderException('busy', httpStatus: 429, retryAfterSeconds: 12),
        resource: 'conversations');
    expect(busy.retryAfterSeconds, 12);
    expect(busy.retryable, isTrue);
  });

  testWidgets('Settings peer navigation respects unsaved edits',
      (tester) async {
    WorkspaceDestination? destination;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: SettingsScreen(
            settings: Settings(),
            onChanged: (_) {},
            onNavigate: (value) => destination = value)));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byType(TextField).first, 'http://127.0.0.1:1234');
    await tester.tap(find.byTooltip('Workspace navigation'));
    await tester.pumpAndSettle();
    await tester.tap(
        find.widgetWithText(PopupMenuItem<WorkspaceDestination>, 'Agents'));
    await tester.pumpAndSettle();
    expect(find.text('Discard unsaved settings?'), findsOneWidget);
    expect(destination, isNull);
    await tester.tap(find.text('Keep editing'));
    await tester.pumpAndSettle();
    expect(find.text('http://127.0.0.1:1234'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('Runtime exposes peer navigation with current page disabled',
      (tester) async {
    tester.view.physicalSize = const Size(1280, 900);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    WorkspaceDestination? destination;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: SystemScreen(
            settings: Settings(),
            liveUpdates: false,
            onNavigate: (value) => destination = value)));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Workspace navigation'));
    await tester.pumpAndSettle();
    final current = tester.widget<PopupMenuItem<WorkspaceDestination>>(
        find.byWidgetPredicate((widget) =>
            widget is PopupMenuItem<WorkspaceDestination> &&
            widget.value == WorkspaceDestination.runtime));
    expect(current.enabled, isFalse);
    await tester.tap(
        find.widgetWithText(PopupMenuItem<WorkspaceDestination>, 'Agents'));
    await tester.pumpAndSettle();
    expect(destination, WorkspaceDestination.agents);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });
}
