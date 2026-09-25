import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/approval_sheet.dart';
import 'package:sonder_runtime/ui/status_row.dart';
import 'package:sonder_runtime/ui/status_vocab.dart';
import 'package:sonder_runtime/workspace_ui.dart';

Widget _host(Widget child, {ThemeData? theme}) => MaterialApp(
    theme: theme ?? SonderTheme.dark,
    home: Scaffold(
        body: Padding(padding: const EdgeInsets.all(16), child: child)));

const _request = ApprovalRequest(
  tool: 'write_file',
  callId: '3f9a12c0d4e5',
  arguments: [
    ('path', 'src/render/pso_cache.cpp'),
    ('content', '(1,204 chars)'),
  ],
  refusedAt: '12:39',
  mode: 'manual',
  reason: 'nobody asked',
);

void _phone(WidgetTester tester) {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
}

void main() {
  group('WorkspaceNotice', () {
    testWidgets('the word precedes the title, visually and for readers',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(const WorkspaceNotice(
        kind: StatusKind.refused,
        title: '/write notes.txt',
        detail: 'File changes need a person to confirm.',
        hint: 'approve this call once',
      )));
      expect(find.text('⊘ refused'), findsOneWidget);
      expect(find.text('/write notes.txt'), findsOneWidget);
      expect(find.text('hint: approve this call once'), findsOneWidget);
      final label = tester
          .getSemantics(find.byType(WorkspaceNotice))
          .getSemanticsData()
          .label;
      expect(label, startsWith('refused: /write notes.txt'));
      expect(label, contains('hint: approve this call once'));
      expect(find.bySemanticsLabel(RegExp(r'^refused: ')), findsOneWidget);
      handle.dispose();
    });

    testWidgets('legacy tones map to kinds and keep their message text',
        (tester) async {
      await tester.pumpWidget(_host(const Column(children: [
        WorkspaceNotice(message: 'Saved', tone: NoticeTone.success),
        WorkspaceNotice(message: 'Check the server', tone: NoticeTone.warning),
        WorkspaceNotice(message: 'FYI'),
      ])));
      expect(find.text('Saved'), findsOneWidget);
      expect(find.text('✓ ok'), findsOneWidget);
      expect(find.text('! warn'), findsOneWidget);
      expect(find.text('· note'), findsOneWidget);
      final warn = tester.widget<Text>(find.text('! warn'));
      // Warn is the warn tone now, not danger (P2-2).
      expect(warn.style?.color, SonderTokens.dark.warn);
      final notices =
          tester.widgetList<WorkspaceNotice>(find.byType(WorkspaceNotice));
      expect(notices.map((n) => n.kind),
          [StatusKind.ok, StatusKind.warn, StatusKind.note]);
    });

    testWidgets('word synonyms and actions', (tester) async {
      var tapped = 0;
      await tester.pumpWidget(_host(WorkspaceNotice(
        kind: StatusKind.warn,
        word: 'needs you',
        title: 'mypc.local is not allowed',
        actions: [
          FilledButton(onPressed: () => tapped++, child: const Text('Retry')),
          OutlinedButton(onPressed: () {}, child: const Text('Settings')),
        ],
      )));
      expect(find.text('! needs you'), findsOneWidget);
      await tester.tap(find.text('Retry'));
      expect(tapped, 1);
    });
  });

  group('status rows', () {
    testWidgets('wide rows align in columns; narrow rows stack',
        (tester) async {
      await tester.pumpWidget(_host(const SizedBox(
          width: 700,
          child: StatusRow(
              kind: StatusKind.ok,
              label: 'Server',
              value: 'mypc · 127.0.0.1:11435'))));
      final wideLabel = tester.getTopLeft(find.text('Server'));
      final wideValue = tester.getTopLeft(find.text('mypc · 127.0.0.1:11435'));
      expect((wideLabel.dy - wideValue.dy).abs(), lessThan(4));

      await tester.pumpWidget(_host(const SizedBox(
          width: 320,
          child: StatusRow(
              kind: StatusKind.ok,
              label: 'Server',
              value: 'mypc · 127.0.0.1:11435'))));
      final label = tester.getTopLeft(find.text('Server'));
      final value = tester.getTopLeft(find.text('mypc · 127.0.0.1:11435'));
      expect(value.dy, greaterThan(label.dy + 8));
    });

    testWidgets('a row is one node that reads word, label, value',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(const StatusRow(
          kind: StatusKind.skipped,
          word: 'off',
          label: 'Autopilot',
          value: 'off by design')));
      expect(find.bySemanticsLabel('off, Autopilot: off by design'),
          findsOneWidget);
      handle.dispose();
    });
  });

  group('raise sheet', () {
    testWidgets('lowering never asks; raising asks and Cancel returns false',
        (tester) async {
      _phone(tester);
      late BuildContext ctx;
      await tester.pumpWidget(_host(Builder(builder: (context) {
        ctx = context;
        return const SizedBox();
      })));
      expect(await confirmModeChange(ctx, from: 'auto', to: 'plan'), isTrue);
      expect(find.byType(RaiseModeSheet), findsNothing);

      final result =
          confirmModeChange(ctx, from: 'manual', to: 'auto', host: 'mypc');
      await tester.pumpAndSettle();
      expect(find.byType(RaiseModeSheet), findsOneWidget);
      expect(find.text('raise mode'), findsOneWidget);
      expect(find.text('manual → auto'), findsOneWidget);
      expect(
          find.textContaining('every chat and agent on mypc'), findsOneWidget);
      expect(
          find.text('Destructive tools still need a person.'), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(await result, isFalse);
    });

    testWidgets('Switch to <mode> confirms; auto is danger, edits warn',
        (tester) async {
      _phone(tester);
      late BuildContext ctx;
      await tester.pumpWidget(_host(Builder(builder: (context) {
        ctx = context;
        return const SizedBox();
      })));
      final result = showRaiseModeSheet(ctx, from: 'manual', to: 'auto');
      await tester.pumpAndSettle();
      final autoButton = tester.widget<ToneButton>(find.byType(ToneButton));
      expect(autoButton.role, StatusRole.danger);
      await tester.tap(find.text('Switch to auto'));
      await tester.pumpAndSettle();
      expect(await result, isTrue);

      final edits = showRaiseModeSheet(ctx, from: 'manual', to: 'acceptEdits');
      await tester.pumpAndSettle();
      expect(tester.widget<ToneButton>(find.byType(ToneButton)).role,
          StatusRole.warning);
      await tester.tap(find.text('Switch to acceptEdits'));
      await tester.pumpAndSettle();
      expect(await edits, isTrue);
    });

    testWidgets('wide windows show a dialog instead of a bottom sheet',
        (tester) async {
      tester.view.physicalSize = const Size(1440, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      late BuildContext ctx;
      await tester.pumpWidget(_host(Builder(builder: (context) {
        ctx = context;
        return const SizedBox();
      })));
      showRaiseModeSheet(ctx, from: 'manual', to: 'auto');
      await tester.pumpAndSettle();
      expect(find.byType(Dialog), findsOneWidget);
      expect(find.byType(BottomSheet), findsNothing);
    });
  });

  group('approval sheet', () {
    testWidgets('shows the exact call and returns the chosen lifetime',
        (tester) async {
      _phone(tester);
      late BuildContext ctx;
      await tester.pumpWidget(_host(Builder(builder: (context) {
        ctx = context;
        return const SizedBox();
      })));
      final result = showApprovalSheet(ctx, request: _request);
      await tester.pumpAndSettle();
      expect(find.text('approve'), findsOneWidget);
      expect(find.text('write_file · call 3f9a12c0'), findsOneWidget);
      expect(find.text('src/render/pso_cache.cpp'), findsOneWidget);
      expect(find.text('12:39 · manual mode · nobody asked'), findsOneWidget);
      expect(find.textContaining('within 15 minutes'), findsOneWidget);
      expect(find.textContaining('Your mode stays manual.'), findsOneWidget);
      expect(find.text('OK'), findsNothing);

      await tester.tap(find.text('15 min'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('1 h').last);
      await tester.pumpAndSettle();
      expect(find.textContaining('within 1 hour'), findsOneWidget);
      await tester.tap(find.text('Approve once'));
      await tester.pumpAndSettle();
      expect(await result, const Duration(minutes: 60));
    });

    testWidgets('Cancel approves nothing', (tester) async {
      _phone(tester);
      late BuildContext ctx;
      await tester.pumpWidget(_host(Builder(builder: (context) {
        ctx = context;
        return const SizedBox();
      })));
      final result = showApprovalSheet(ctx, request: _request);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(await result, isNull);
    });

    testWidgets('console fallback offers /approve with Copy, no Approve',
        (tester) async {
      _phone(tester);
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
      await tester.pumpWidget(_host(SingleChildScrollView(
        child: ApprovalSheet(
          request: _request,
          consoleFallback: true,
          onApprove: (_) => fail('must not approve'),
          onCancel: () {},
        ),
      )));
      expect(find.text('Approve from the console: /approve 3f9a12c0d4e5'),
          findsOneWidget);
      expect(find.text('Approve once'), findsNothing);
      await tester.tap(find.text('Copy'));
      await tester.pumpAndSettle();
      expect(copied, ['/approve 3f9a12c0d4e5']);
      expect(find.text('Copied'), findsOneWidget);
    });

    testWidgets('busy disables both actions; errors read as error notices',
        (tester) async {
      _phone(tester);
      var approvals = 0;
      await tester.pumpWidget(_host(SingleChildScrollView(
        child: ApprovalSheet(
          request: _request,
          busy: true,
          error: 'Approvals need a developer or admin account',
          onApprove: (_) => approvals++,
          onCancel: () {},
        ),
      )));
      expect(find.text('✗ error'), findsOneWidget);
      expect(find.text('Approvals need a developer or admin account'),
          findsOneWidget);
      final cancel =
          tester.widget<TextButton>(find.widgetWithText(TextButton, 'Cancel'));
      expect(cancel.onPressed, isNull);
      await tester.tap(find.byType(ToneButton));
      expect(approvals, 0);
    });

    testWidgets('receipt names the call once and offers explicit actions',
        (tester) async {
      var retries = 0;
      await tester.pumpWidget(_host(ApprovalReceipt(
        tool: 'write_file',
        callId: '3f9a12c0d4e5',
        nonce: 'n_c41a',
        ttl: const Duration(minutes: 15),
        onRetry: () => retries++,
        onRevoke: () {},
      )));
      expect(find.text('✓ approved'), findsOneWidget);
      expect(
          find.text(
              'write_file call 3f9a12c0 once · nonce n_c41a · valid 15 minutes'),
          findsOneWidget);
      expect(retries, 0);
      await tester.tap(find.text('Retry the request'));
      expect(retries, 1);
      expect(find.text('Revoke'), findsOneWidget);
    });
  });

  group('SonderSymbols', () {
    testWidgets('the font is declared, bundled and loadable', (tester) async {
      final manifest = await tester
          .runAsync(() => rootBundle.loadString('FontManifest.json'));
      final families = [
        for (final entry in jsonDecode(manifest!) as List)
          (entry as Map)['family'] as String
      ];
      expect(families, contains(SonderTheme.symbols));
      await tester.runAsync(() async {
        final bytes = await rootBundle.load('assets/fonts/SonderSymbols.ttf');
        expect(bytes.lengthInBytes, greaterThan(1000));
        final loader = FontLoader(SonderTheme.symbols)
          ..addFont(Future.value(bytes));
        await loader.load();
      });
    });
  });
}
