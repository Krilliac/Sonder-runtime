import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/composer.dart';
import 'package:sonder_runtime/chat/permission_mode.dart';
import 'package:sonder_runtime/settings_screen.dart';

import 'chat_fakes.dart';

void main() {
  group('classifyIntercept', () {
    test('account commands open Settings and keep only the user name', () {
      final i = classifyIntercept('/login bob hunter2');
      expect(i, isA<AccountIntercept>());
      expect((i as AccountIntercept).username, 'bob');
      expect(classifyIntercept('/register'), isA<AccountIntercept>());
      expect(
          classifyIntercept('/ADMIN_LOGIN root pw'), isA<AccountIntercept>());
    });

    test('mode commands go to the chip flow', () {
      expect((classifyIntercept('/mode auto') as ModeIntercept).target, 'auto');
      expect(
          (classifyIntercept('/permission_mode acceptedits') as ModeIntercept)
              .target,
          'acceptEdits');
      expect((classifyIntercept('/mode') as ModeIntercept).target, isNull);
      expect((classifyIntercept('/permissions plan') as ModeIntercept).target,
          'plan');
      expect(classifyIntercept('/elevate'), isA<ModeIntercept>());
      expect(
          (classifyIntercept('/perms auto') as ModeIntercept).target, 'auto');
    });

    test('everything else is sent', () {
      expect(classifyIntercept('/permissions'), isNull);
      expect(classifyIntercept('/permissions list'), isNull);
      expect(classifyIntercept('/stats'), isNull);
      expect(classifyIntercept('please /login for me'), isNull);
      expect(classifyIntercept('hello'), isNull);
    });

    test('raise rules', () {
      expect(isModeRaise('manual', 'acceptEdits'), isTrue);
      expect(isModeRaise('manual', 'auto'), isTrue);
      expect(isModeRaise('acceptEdits', 'auto'), isTrue);
      expect(isModeRaise('plan', 'auto'), isTrue);
      expect(isModeRaise('plan', 'manual'), isFalse);
      expect(isModeRaise('auto', 'plan'), isFalse);
      expect(isModeRaise('auto', 'manual'), isFalse);
      expect(isModeRaise('manual', 'someNewMode'), isTrue);
    });

    test('palette labels say where intercepted commands go', () {
      expect(interceptLabel('/login'), 'opens Settings');
      expect(interceptLabel('/mode'), 'opens mode');
      expect(interceptLabel('/stats'), isNull);
    });
  });

  testWidgets('/login with a password never reaches chat or the store',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await tester.enterText(find.byType(TextField), '/login bob hunter2');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));

    expect(backend.turns, isEmpty);
    expect(backend.feedback, isEmpty);
    expect(find.byType(SettingsScreen), findsOneWidget);
    expect(await storedChatText(), isNot(contains('hunter2')));
    expect(find.textContaining('hunter2'), findsNothing);
    await unmountChat(tester);
  });

  testWidgets('/mode auto opens the raise sheet; Cancel sends nothing',
      (tester) async {
    final backend = FakeChatBackend()..mode = permissionModeFor('manual');
    await pumpChat(tester, backend);
    await tester.enterText(find.byType(TextField), '/mode auto');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('raise-mode-sheet')), findsOneWidget);
    expect(find.textContaining('manual → auto', findRichText: true),
        findsOneWidget);
    expect(find.text('Switch to auto'), findsOneWidget);
    await tester.tap(find.byKey(const Key('raise-mode-cancel')));
    await tester.pumpAndSettle();
    expect(backend.modePosts, isEmpty);
    expect(backend.turns, isEmpty);
    await unmountChat(tester);
  });

  testWidgets('Shift+Tab from manual opens the sheet; Confirm posts once',
      (tester) async {
    final backend = FakeChatBackend()..mode = permissionModeFor('manual');
    await pumpChat(tester, backend);
    await tester.tap(find.byType(TextField));
    await tester.pump();

    await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
    await tester.pumpAndSettle();

    // manual -> acceptEdits is a raise.
    expect(find.byKey(const Key('raise-mode-sheet')), findsOneWidget);
    expect(find.textContaining('manual → acceptEdits', findRichText: true),
        findsOneWidget);
    await tester.tap(find.byKey(const Key('raise-mode-cancel')));
    await tester.pumpAndSettle();
    expect(backend.modePosts, isEmpty);

    await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('raise-mode-confirm')));
    await tester.pumpAndSettle();
    expect(backend.modePosts, ['acceptEdits']);
    final chip = find.byKey(const Key('permission-mode-chip'));
    expect(find.descendant(of: chip, matching: find.text('acceptEdits')),
        findsOneWidget);
    await unmountChat(tester);
  });

  testWidgets('auto -> plan from the picker posts with no sheet',
      (tester) async {
    final backend = FakeChatBackend()..mode = permissionModeFor('auto');
    await pumpChat(tester, backend);
    await tester.tap(find.byKey(const Key('permission-mode-chip')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('permission-mode-option-plan')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('raise-mode-sheet')), findsNothing);
    expect(backend.modePosts, ['plan']);
    await unmountChat(tester);
  });

  testWidgets('after a 403 the chip is read-only and nothing shows a map',
      (tester) async {
    final backend = FakeChatBackend()
      ..mode = permissionModeFor('auto')
      ..modeWriteError = SonderException(
          '{message: only an administrator can change the mode, code: FORBIDDEN}');
    await pumpChat(tester, backend);
    await tester.tap(find.byKey(const Key('permission-mode-chip')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('permission-mode-option-plan')));
    await tester.pumpAndSettle();

    expect(
        find.text('Only an administrator can change the mode'), findsOneWidget);
    expect(find.textContaining('{'), findsNothing);
    final chip =
        tester.widget<InkWell>(find.byKey(const Key('permission-mode-chip')));
    expect(chip.onTap, isNull);
    expect(find.byTooltip('Only an administrator can change the mode'),
        findsOneWidget);

    // Tapping does nothing: no dialog, no POST.
    await tester.tap(find.byKey(const Key('permission-mode-chip')),
        warnIfMissed: false);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('permission-mode-picker')), findsNothing);
    expect(backend.modePosts, ['plan']);
    await unmountChat(tester);
  });

  testWidgets('the raise sheet is a bottom sheet on phones', (tester) async {
    final backend = FakeChatBackend()..mode = permissionModeFor('manual');
    await pumpChat(tester, backend, size: const Size(390, 844));
    await tester.enterText(find.byType(TextField), '/permissions auto');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pumpAndSettle();
    expect(find.byType(BottomSheet), findsOneWidget);
    expect(find.byKey(const Key('raise-mode-sheet')), findsOneWidget);
    await tester.tap(find.byKey(const Key('raise-mode-confirm')));
    await tester.pumpAndSettle();
    expect(backend.modePosts, ['auto']);
    await unmountChat(tester);
  });
}
