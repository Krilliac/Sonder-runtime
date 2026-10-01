import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/drawer.dart';
import 'package:sonder_runtime/chat_screen.dart';
import 'package:sonder_runtime/main.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/shell/app_shell.dart';
import 'package:sonder_runtime/shell/preferences.dart';
import 'package:sonder_runtime/ui/kit.dart';
import 'package:sonder_runtime/ui/sonder_mark.dart';
import 'package:sonder_runtime/workspace_ui.dart';

import 'chat_fakes.dart';

final _now = DateTime(2026, 9, 30, 15);
DateTime _clock() => _now;

const _desk = Size(1440, 900);
const _phone = Size(390, 844);

/// Conversations spread over every date group and two projects.
Map<String, Object> _seed({bool twoProjects = true, bool emptyNewest = false}) {
  Map<String, Object?> thread(String id, String text, DateTime at,
          {String project = 'engine', bool empty = false}) =>
      {
        'id': id,
        'title': text,
        'project': project,
        'created_at': at.toIso8601String(),
        'updated_at': at.toIso8601String(),
        'messages': [
          if (!empty) ...[
            ChatMessage(role: Role.user, content: text).toJson(),
            const ChatMessage(role: Role.assistant, content: 'ok').toJson(),
          ],
        ],
      };
  final threads = {
    if (emptyNewest)
      'n': thread('n', 'New chat', _now.add(const Duration(minutes: 1)),
          empty: true),
    'a': thread('a', 'Shader cache refactor', _now),
    'b': thread('b', 'Ollama pool on node1',
        _now.subtract(const Duration(hours: 2))),
    'c': thread('c', 'Fleet run review', _now.subtract(const Duration(days: 1))),
    'd': thread('d', 'Release notes draft',
        _now.subtract(const Duration(days: 3)),
        project: twoProjects ? 'release' : 'engine'),
    'e': thread(
        'e',
        'A very long conversation title that keeps going about pipeline '
            'state caches, hot reload and the driver',
        _now.subtract(const Duration(days: 30))),
  };
  return {
    for (final e in threads.entries)
      'sonder_chat_v2/thread-${e.key}.json': jsonEncode(e.value),
    'sonder_chat_v2/migrated-v1': 'yes',
  };
}

/// A stand-in destination page: its title, the section it was opened at,
/// and the shell's menu button on narrow layouts.
class _StandIn extends StatelessWidget {
  final String title;
  final ShellSection? section;
  final List<Widget> children;

  const _StandIn(this.title, {this.section, this.children = const []});

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          leading:
              ShellMenuButton.shown(context) ? const ShellMenuButton() : null,
          title: Text('$title page'),
        ),
        body: Column(children: [
          Text('section: ${section?.id ?? 'none'}'),
          ...children,
        ]),
      );
}

/// Settings with unsaved edits: leaving asks, like the real page.
class _DirtySettings extends StatelessWidget {
  final ValueNotifier<bool> dirty;
  const _DirtySettings(this.dirty);

  @override
  Widget build(BuildContext context) => ShellLeaveGuard(
        canLeave: () async {
          if (!dirty.value) return true;
          final discard = await showDialog<bool>(
            context: context,
            builder: (context) => AlertDialog(
              title: const Text('Discard unsaved settings?'),
              actions: [
                TextButton(
                    onPressed: () => Navigator.of(context).pop(false),
                    child: const Text('Keep editing')),
                FilledButton(
                    onPressed: () => Navigator.of(context).pop(true),
                    child: const Text('Discard changes')),
              ],
            ),
          );
          return discard == true;
        },
        child: const _StandIn('Settings'),
      );
}

ShellPageBuilder _standIns({
  Widget? settings,
  List<Widget> runtimeChildren = const [],
}) =>
    (context, destination, section) => switch (destination) {
          WorkspaceDestination.agents => _StandIn('Agents', section: section),
          WorkspaceDestination.runtime =>
            _StandIn('Runtime', section: section, children: runtimeChildren),
          WorkspaceDestination.settings =>
            settings ?? _StandIn('Settings', section: section),
          WorkspaceDestination.chat => null,
        };

Finder _destination(WorkspaceDestination d) =>
    find.byKey(Key('shell-destination-${d.name}'));

Future<void> _settle(WidgetTester tester) async {
  for (var i = 0; i < 8; i++) {
    await tester.pump(const Duration(milliseconds: 60));
  }
}

Future<void> _keys(WidgetTester tester, List<LogicalKeyboardKey> held,
    LogicalKeyboardKey key) async {
  for (final k in held) {
    await tester.sendKeyDownEvent(k);
  }
  await tester.sendKeyEvent(key);
  for (final k in held.reversed) {
    await tester.sendKeyUpEvent(k);
  }
  await _settle(tester);
}

Future<void> _ctrl(WidgetTester tester, LogicalKeyboardKey key) =>
    _keys(tester, [LogicalKeyboardKey.controlLeft], key);

/// The composer: the only text field Chat shows.
Finder get _composer => find.descendant(
    of: find.byType(ChatScreen), matching: find.byType(TextField));

void main() {
  tearDown(() => debugDefaultTargetPlatformOverride = null);

  group('wide layout', () {
    testWidgets('sidebar: brand, New chat, Search, destinations, chats by date',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);

      expect(find.byKey(const Key('shell-sidebar')), findsOneWidget);
      expect(tester.getSize(find.byKey(const Key('shell-sidebar'))).width,
          264);
      expect(find.byType(SonderMark), findsWidgets);
      expect(find.text('Sonder'), findsOneWidget);
      expect(find.byKey(const Key('shell-new-chat')), findsOneWidget);
      expect(find.text('Search chats'), findsOneWidget);
      for (final d in WorkspaceDestination.values) {
        expect(_destination(d), findsOneWidget);
      }
      for (final group in ['Today', 'Yesterday', 'Previous 7 days', 'Older']) {
        expect(find.text(group), findsOneWidget, reason: group);
      }
      // Today holds the two newest; the 30-day-old one is under Older.
      final today = tester.getTopLeft(find.text('Today')).dy;
      final older = tester.getTopLeft(find.text('Older')).dy;
      final shader = tester.getTopLeft(find.byKey(const ValueKey('a'))).dy;
      final long = tester.getTopLeft(find.byKey(const ValueKey('e'))).dy;
      expect(shader, greaterThan(today));
      expect(shader, lessThan(older));
      expect(long, greaterThan(older));
      await unmountChat(tester);
    });

    testWidgets('long titles stay on one line, ellipsized', (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);
      final title = tester.widget<Text>(find.descendant(
          of: find.byKey(const ValueKey('e')),
          matching: find.textContaining('A very long conversation')));
      expect(title.maxLines, 1);
      expect(title.overflow, TextOverflow.ellipsis);
      final row = tester.getSize(find.byKey(const ValueKey('e')));
      expect(row.height, lessThan(60));
      expect(tester.takeException(), isNull);
      await unmountChat(tester);
    });

    testWidgets('destinations switch the page and mark the selection',
        (tester) async {
      final handle = tester.ensureSemantics();
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);

      for (final (d, title) in [
        (WorkspaceDestination.agents, 'Agents page'),
        (WorkspaceDestination.runtime, 'Runtime page'),
        (WorkspaceDestination.settings, 'Settings page'),
      ]) {
        await tester.tap(_destination(d));
        await _settle(tester);
        expect(find.text(title), findsOneWidget);
        expect(
            tester.getSemantics(find.descendant(
                of: _destination(d), matching: find.byType(HoverSurface))),
            isSemantics(
                isSelected: true, isButton: true, label: d.label),
            reason: d.label);
        // Inside the shell a page keeps no menu button on wide layouts.
        expect(find.byKey(const Key('shell-menu')), findsNothing);
      }
      await tester.tap(_destination(WorkspaceDestination.chat));
      await _settle(tester);
      expect(find.text('Settings page'), findsNothing);
      expect(find.byType(ChatScreen), findsOneWidget);
      handle.dispose();
      await unmountChat(tester);
    });

    testWidgets('Chat stays mounted: the draft and the page state survive',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);
      await tester.enterText(_composer, 'half-written question');
      final state = tester.state(find.byType(ChatScreen));

      await tester.tap(_destination(WorkspaceDestination.agents));
      await _settle(tester);
      // Hidden, not gone: offstage, out of hit testing and focus.
      expect(find.byType(ChatScreen), findsNothing);
      expect(find.byType(ChatScreen, skipOffstage: false), findsOneWidget);

      await tester.tap(_destination(WorkspaceDestination.chat));
      await _settle(tester);
      expect(identical(tester.state(find.byType(ChatScreen)), state), isTrue);
      expect(tester.widget<TextField>(_composer).controller!.text,
          'half-written question');
      await unmountChat(tester);
    });

    testWidgets('a streaming turn keeps running behind another destination',
        (tester) async {
      final handle = tester.ensureSemantics();
      final backend = FakeChatBackend();
      await pumpShell(tester, backend,
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);
      await tester.enterText(_composer, 'summarise the fleet run');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      backend.lastTurn.delta('Three agents ');

      await tester.tap(_destination(WorkspaceDestination.runtime));
      await _settle(tester);
      expect(find.text('Runtime page'), findsOneWidget);
      // The rail says a reply is being written, in words.
      expect(find.bySemanticsLabel(RegExp(r'^Chat, a reply is working')),
          findsOneWidget);

      backend.lastTurn.delta('finished.');
      backend.lastTurn.done('Three agents finished.');
      await _settle(tester);
      expect(find.bySemanticsLabel(RegExp(r'^Chat, a reply')), findsNothing);

      await tester.tap(_destination(WorkspaceDestination.chat));
      await _settle(tester);
      expect(find.text('Three agents finished.', findRichText: true),
          findsWidgets);
      expect(backend.turns.single.cancels, 0);
      handle.dispose();
      await unmountChat(tester);
    });

    testWidgets('leave guard: unsaved settings ask before the shell leaves',
        (tester) async {
      final dirty = ValueNotifier<bool>(true);
      await pumpShell(tester, FakeChatBackend(),
          size: _desk,
          prefs: _seed(),
          clock: _clock,
          pageBuilder: _standIns(settings: _DirtySettings(dirty)));
      await _settle(tester);
      await tester.tap(_destination(WorkspaceDestination.settings));
      await _settle(tester);
      expect(find.text('Settings page'), findsOneWidget);

      // Sidebar: asks, and Keep editing stays.
      await tester.tap(_destination(WorkspaceDestination.agents));
      await _settle(tester);
      expect(find.text('Discard unsaved settings?'), findsOneWidget);
      await tester.tap(find.text('Keep editing'));
      await _settle(tester);
      expect(find.text('Settings page'), findsOneWidget);
      expect(find.text('Agents page'), findsNothing);

      // A shortcut asks too.
      await _ctrl(tester, LogicalKeyboardKey.digit1);
      expect(find.text('Discard unsaved settings?'), findsOneWidget);
      await tester.tap(find.text('Keep editing'));
      await _settle(tester);
      expect(find.text('Settings page'), findsOneWidget);

      // Discard leaves.
      await tester.tap(_destination(WorkspaceDestination.agents));
      await _settle(tester);
      await tester.tap(find.text('Discard changes'));
      await _settle(tester);
      expect(find.text('Agents page'), findsOneWidget);

      // Nothing unsaved: no question.
      dirty.value = false;
      await tester.tap(_destination(WorkspaceDestination.settings));
      await _settle(tester);
      await tester.tap(_destination(WorkspaceDestination.runtime));
      await _settle(tester);
      expect(find.text('Discard unsaved settings?'), findsNothing);
      expect(find.text('Runtime page'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('collapses to a 64 px rail and back, and remembers',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);
      final sidebar = find.byKey(const Key('shell-sidebar'));

      await tester.tap(find.byKey(const Key('shell-collapse')));
      await _settle(tester);
      expect(tester.getSize(sidebar).width, 64);
      expect(await ShellPreferences.sidebarCollapsed(), isTrue);
      // The chat list is gone from the rail; the destinations remain.
      expect(find.byType(ThreadRow).hitTestable(), findsNothing);
      await tester.tap(_destination(WorkspaceDestination.chat));
      await _settle(tester);

      await _ctrl(tester, LogicalKeyboardKey.keyB);
      expect(tester.getSize(sidebar).width, 264);
      expect(await ShellPreferences.sidebarCollapsed(), isFalse);
      expect(find.byType(ThreadRow).hitTestable(), findsWidgets);

      // The rail's top cell expands it again.
      await _ctrl(tester, LogicalKeyboardKey.keyB);
      expect(tester.getSize(sidebar).width, 64);
      await tester.tap(find.byKey(const Key('shell-expand')));
      await _settle(tester);
      expect(tester.getSize(sidebar).width, 264);
      await unmountChat(tester);
    });

    testWidgets('starts collapsed when that was remembered', (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock, collapsed: true);
      await _settle(tester);
      expect(tester.getSize(find.byKey(const Key('shell-sidebar'))).width, 64);
      await unmountChat(tester);
    });

    testWidgets('badges: running agents and approvals waiting, in words',
        (tester) async {
      final handle = tester.ensureSemantics();
      var reads = 0;
      final backend = FakeChatBackend()
        ..statusInfo = SystemInfo.fromJson({
          'agents': {'active_agents': 2},
          'autopilot': {'active_runs': 1},
        });
      await pumpShell(tester, backend,
          size: _desk,
          prefs: _seed(),
          clock: _clock,
          approvals: () async {
            reads++;
            return ApprovalsSnapshot.fromJson(const {
              'pending': [
                {'call_id': '3f9a12c0', 'tool': 'write_file', 'digest': 'd'},
              ],
            });
          });
      await _settle(tester);
      expect(find.bySemanticsLabel('Agents, 3 running'), findsOneWidget);
      expect(find.bySemanticsLabel('Runtime, 1 approval waiting'),
          findsOneWidget);
      expect(reads, 1);
      // The visible badges carry the glyph and the number, not colour alone.
      expect(find.text('3'), findsWidgets);
      expect(find.text('1'), findsWidgets);

      // The same badges on the collapsed rail.
      await _ctrl(tester, LogicalKeyboardKey.keyB);
      expect(find.bySemanticsLabel('Agents, 3 running'), findsOneWidget);
      expect(find.bySemanticsLabel('Runtime, 1 approval waiting'),
          findsOneWidget);

      // Slow cadence: the next read comes 20 s later, not with every poll.
      await tester.pump(const Duration(seconds: 21));
      expect(reads, 2);
      handle.dispose();
      await unmountChat(tester);
    });

    testWidgets('an account that may not read approvals stops asking',
        (tester) async {
      final handle = tester.ensureSemantics();
      var reads = 0;
      await pumpShell(tester, FakeChatBackend(),
          size: _desk,
          prefs: _seed(),
          clock: _clock,
          approvals: () async {
            reads++;
            throw SonderException('Approvals need a developer or admin '
                'account.', httpStatus: 403);
          });
      await _settle(tester);
      await tester.pump(const Duration(minutes: 2));
      expect(reads, 1);
      expect(find.bySemanticsLabel(RegExp('approval')), findsNothing);
      handle.dispose();
      await unmountChat(tester);
    });

    testWidgets('delete is revealed on hover and can be undone',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);
      final row = find.byKey(const ValueKey('b'));
      Finder deleteIn(Finder f) => find.descendant(
          of: f, matching: find.byTooltip('Delete chat'));
      // Only the open conversation shows Delete before any hover.
      expect(deleteIn(row), findsNothing);
      expect(deleteIn(find.byKey(const ValueKey('a'))), findsOneWidget);

      final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
      await mouse.addPointer(location: Offset.zero);
      addTearDown(mouse.removePointer);
      await mouse.moveTo(tester.getCenter(row));
      await tester.pump();
      expect(deleteIn(row), findsOneWidget);

      await tester.tap(deleteIn(row));
      await _settle(tester);
      expect(row, findsNothing);
      expect(find.text('Chat deleted.'), findsOneWidget);
      await tester.tap(find.widgetWithText(SnackBarAction, 'Undo'));
      await _settle(tester);
      expect(find.byKey(const ValueKey('b')), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('opening a conversation from the sidebar shows it in Chat',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);
      await tester.tap(_destination(WorkspaceDestination.runtime));
      await _settle(tester);
      await tester.tap(find.byKey(const ValueKey('c')));
      await _settle(tester);
      expect(find.byType(ChatScreen), findsOneWidget);
      expect(
          find.descendant(
              of: find.byType(AppBar), matching: find.text('Fleet run review')),
          findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('the project filter narrows the list to one project',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);
      expect(find.byType(ThreadRow), findsNWidgets(5));
      await tester.tap(find.byKey(const Key('shell-project-filter')));
      await _settle(tester);
      await tester.tap(find.text('release').last);
      await _settle(tester);
      expect(find.byType(ThreadRow), findsOneWidget);
      expect(find.byKey(const ValueKey('d')), findsOneWidget);

      await tester.tap(find.byKey(const Key('shell-project-filter')));
      await _settle(tester);
      await tester.tap(find.text('All projects').last);
      await _settle(tester);
      expect(find.byType(ThreadRow), findsNWidgets(5));
      await unmountChat(tester);
    });

    testWidgets('one project: no filter to choose from', (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(twoProjects: false), clock: _clock);
      await _settle(tester);
      expect(find.byKey(const Key('shell-project-filter')), findsNothing);
      await unmountChat(tester);
    });

    testWidgets('the connection footer opens Runtime, or Settings when offline',
        (tester) async {
      final backend = FakeChatBackend();
      await pumpShell(tester, backend,
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);
      final footer = find.byKey(const Key('rail-connection'));
      expect(find.descendant(of: footer, matching: find.text('connected')),
          findsOneWidget);
      await tester.tap(find.byKey(const Key('shell-connection')));
      await _settle(tester);
      expect(find.text('Runtime page'), findsOneWidget);

      backend.statusError =
          SonderException('Cannot reach server: SocketException');
      await tester.pump(const Duration(seconds: 6));
      await _settle(tester);
      expect(find.descendant(of: footer, matching: find.text("can't reach")),
          findsOneWidget);
      await tester.tap(find.byKey(const Key('shell-connection')));
      await _settle(tester);
      expect(find.text('Settings page'), findsOneWidget);
      expect(find.text('section: connection'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('Search finds a conversation by title; Enter opens it',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('shell-search')));
      await _settle(tester);
      expect(find.byKey(const Key('thread-switcher')), findsOneWidget);
      await tester.enterText(
          find.byKey(const Key('thread-switcher-search')), 'release');
      await _settle(tester);
      expect(find.byKey(const ValueKey('switcher-d')), findsOneWidget);
      expect(find.byKey(const ValueKey('switcher-a')), findsNothing);
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await _settle(tester);
      expect(find.byKey(const Key('thread-switcher')), findsNothing);
      expect(
          find.descendant(
              of: find.byType(AppBar),
              matching: find.text('Release notes draft')),
          findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('sidebar meets the tap-target and label guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      // A stand-in page beside the sidebar, so only the shell's own
      // controls are measured (Chat is offstage).
      await pumpShell(tester, FakeChatBackend(),
          size: _desk,
          prefs: _seed(emptyNewest: true),
          clock: _clock,
          pageBuilder: _standIns());
      await _settle(tester);
      await tester.tap(_destination(WorkspaceDestination.runtime));
      await _settle(tester);
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await _ctrl(tester, LogicalKeyboardKey.keyB);
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      handle.dispose();
      await unmountChat(tester);
    });
  });

  group('narrow layouts', () {
    for (final size in [_phone, const Size(800, 1000)]) {
      testWidgets(
          'the menu opens the drawer and reaches every destination '
          '(${size.width.round()} px)', (tester) async {
        await pumpShell(tester, FakeChatBackend(),
            size: size, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
        await _settle(tester);
        expect(find.byKey(const Key('shell-sidebar')), findsNothing);

        Future<void> go(WorkspaceDestination d) async {
          await tester.tap(find.byKey(const Key('shell-menu')));
          await _settle(tester);
          expect(find.byType(Drawer), findsOneWidget);
          await tester.tap(_destination(d));
          await _settle(tester);
          expect(find.byType(Drawer), findsNothing);
        }

        await go(WorkspaceDestination.agents);
        expect(find.text('Agents page'), findsOneWidget);
        await go(WorkspaceDestination.runtime);
        expect(find.text('Runtime page'), findsOneWidget);
        await go(WorkspaceDestination.settings);
        expect(find.text('Settings page'), findsOneWidget);
        await go(WorkspaceDestination.chat);
        expect(find.byType(ChatScreen), findsOneWidget);
        await unmountChat(tester);
      });
    }

    testWidgets('a conversation picked in the drawer opens and closes it',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _phone, prefs: _seed(), clock: _clock);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('shell-menu')));
      await _settle(tester);
      await tester.tap(find.byKey(const ValueKey('c')));
      await _settle(tester);
      expect(find.byType(Drawer), findsNothing);
      expect(
          find.descendant(
              of: find.byType(AppBar), matching: find.text('Fleet run review')),
          findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('drawer meets the tap-target and label guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      await pumpShell(tester, FakeChatBackend(),
          size: _phone, prefs: _seed(emptyNewest: true), clock: _clock);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('shell-menu')));
      await _settle(tester);
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      handle.dispose();
      await unmountChat(tester);
    });

    testWidgets('large text: chat header and drawer do not overflow',
        (tester) async {
      tester.platformDispatcher.textScaleFactorTestValue = 2.0;
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      await pumpShell(tester, FakeChatBackend(),
          size: _phone, prefs: _seed(), clock: _clock);
      await _settle(tester);
      expect(tester.takeException(), isNull);
      await tester.tap(find.byKey(const Key('shell-menu')));
      await _settle(tester);
      expect(tester.takeException(), isNull);
      expect(find.text('Search chats'), findsOneWidget);
      await unmountChat(tester);
    });
  });

  group('keyboard', () {
    testWidgets('Ctrl+1…4, Ctrl+comma and the Ctrl+D alias pick destinations',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);
      await _ctrl(tester, LogicalKeyboardKey.digit2);
      expect(find.text('Agents page'), findsOneWidget);
      await _ctrl(tester, LogicalKeyboardKey.digit3);
      expect(find.text('Runtime page'), findsOneWidget);
      await _ctrl(tester, LogicalKeyboardKey.digit4);
      expect(find.text('Settings page'), findsOneWidget);
      await _ctrl(tester, LogicalKeyboardKey.digit1);
      expect(find.byType(ChatScreen), findsOneWidget);
      await _ctrl(tester, LogicalKeyboardKey.comma);
      expect(find.text('Settings page'), findsOneWidget);
      await _ctrl(tester, LogicalKeyboardKey.keyD);
      expect(find.text('Runtime page'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('Ctrl+N opens a new chat once, from anywhere', (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);
      await _ctrl(tester, LogicalKeyboardKey.digit3);
      await _ctrl(tester, LogicalKeyboardKey.keyN);
      expect(find.byType(ChatScreen), findsOneWidget);
      expect(find.byType(ThreadRow), findsNWidgets(6));
      // A second Ctrl+N keeps the untouched new chat instead of piling up.
      await _ctrl(tester, LogicalKeyboardKey.keyN);
      expect(find.byType(ThreadRow), findsNWidgets(6));
      // And the composer has the caret.
      expect(
          FocusManager.instance.primaryFocus?.context
              ?.findAncestorWidgetOfExactType<TextField>(),
          isNotNull);
      await unmountChat(tester);
    });

    testWidgets('Ctrl+K opens the command browser from another destination',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);
      await _ctrl(tester, LogicalKeyboardKey.digit4);
      await _ctrl(tester, LogicalKeyboardKey.keyK);
      expect(find.byKey(const Key('command-browser')), findsOneWidget);
      expect(find.byType(ChatScreen), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('Ctrl+P opens Search', (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);
      await _ctrl(tester, LogicalKeyboardKey.keyP);
      expect(find.byKey(const Key('thread-switcher')), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('Ctrl+/ shows the shortcut guide with every shortcut',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);
      await _ctrl(tester, LogicalKeyboardKey.slash);
      final guide = find.byKey(const Key('shortcut-guide'));
      expect(guide, findsOneWidget);
      for (final action in [
        'New chat',
        'Search chats',
        'Browse commands',
        'Chat, Agents, Runtime, Settings',
        'Settings',
        'Show or hide the sidebar',
        'Show keyboard shortcuts',
        'Send',
        'New line',
        'Next permission mode',
        'Send a follow-up',
        'Find a conversation',
      ]) {
        expect(find.descendant(of: guide, matching: find.text(action)),
            findsOneWidget,
            reason: action);
      }
      expect(find.descendant(of: guide, matching: find.text('same as Ctrl+3')),
          findsOneWidget);
      await tester.tap(find.byTooltip('Close'));
      await _settle(tester);
      expect(guide, findsNothing);
      // The sidebar button opens it too.
      await tester.tap(find.byKey(const Key('shell-shortcuts')));
      await _settle(tester);
      expect(guide, findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('Shift+Tab cycles the mode only from the composer',
        (tester) async {
      final backend = FakeChatBackend()..mode = permissionModeFor('manual');
      await pumpShell(tester, backend,
          size: _desk, prefs: _seed(), clock: _clock);
      await _settle(tester);

      // In the composer: manual → acceptEdits is a raise, so it asks first.
      await tester.tap(_composer);
      await tester.pump();
      await _keys(tester, [LogicalKeyboardKey.shiftLeft], LogicalKeyboardKey.tab);
      expect(find.byKey(const Key('raise-mode-sheet')), findsOneWidget);
      await tester.tap(find.byKey(const Key('raise-mode-cancel')));
      await _settle(tester);
      expect(backend.modePosts, isEmpty);

      // Anywhere else Shift+Tab moves focus backwards and changes nothing.
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump();
      await _keys(tester, [LogicalKeyboardKey.shiftLeft], LogicalKeyboardKey.tab);
      expect(find.byKey(const Key('raise-mode-sheet')), findsNothing);
      expect(backend.modePosts, isEmpty);
      expect(FocusManager.instance.primaryFocus, isNotNull);
      await unmountChat(tester);
    });

    testWidgets('on macOS the shortcuts use ⌘, not Ctrl', (tester) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.macOS;
      await pumpShell(tester, FakeChatBackend(),
          size: _desk, prefs: _seed(), clock: _clock, pageBuilder: _standIns());
      await _settle(tester);
      await _ctrl(tester, LogicalKeyboardKey.digit2);
      expect(find.text('Agents page'), findsNothing);
      await _keys(tester, [LogicalKeyboardKey.metaLeft],
          LogicalKeyboardKey.digit2);
      expect(find.text('Agents page'), findsOneWidget);
      await unmountChat(tester);
      debugDefaultTargetPlatformOverride = null;
    });
  });

  group('back and pushed routes', () {
    testWidgets('system back from a destination returns to Chat, guarded',
        (tester) async {
      final dirty = ValueNotifier<bool>(true);
      await pumpShell(tester, FakeChatBackend(),
          size: _desk,
          prefs: _seed(),
          clock: _clock,
          pageBuilder: _standIns(settings: _DirtySettings(dirty)));
      await _settle(tester);
      await tester.tap(_destination(WorkspaceDestination.settings));
      await _settle(tester);
      await tester.binding.handlePopRoute();
      await _settle(tester);
      expect(find.text('Discard unsaved settings?'), findsOneWidget);
      await tester.tap(find.text('Discard changes'));
      await _settle(tester);
      expect(find.byType(ChatScreen), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('a page that pops itself goes back to Chat, not to nothing',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk,
          prefs: _seed(),
          clock: _clock,
          pageBuilder: _standIns(runtimeChildren: [
            Builder(
              builder: (context) => TextButton(
                onPressed: () => Navigator.of(context).pop(),
                child: const Text('Back to chat (old page)'),
              ),
            ),
          ]));
      await _settle(tester);
      await tester.tap(_destination(WorkspaceDestination.runtime));
      await _settle(tester);
      await tester.tap(find.text('Back to chat (old page)'));
      await _settle(tester);
      expect(find.byType(ChatScreen), findsOneWidget);
      expect(find.byKey(const Key('shell-sidebar')), findsOneWidget);
      expect(tester.takeException(), isNull);
      await unmountChat(tester);
    });

    testWidgets('routes a page pushes stay beside the sidebar; back pops them',
        (tester) async {
      await pumpShell(tester, FakeChatBackend(),
          size: _desk,
          prefs: _seed(),
          clock: _clock,
          pageBuilder: _standIns(runtimeChildren: [
            Builder(
              builder: (context) => TextButton(
                onPressed: () => Navigator.of(context).push(
                    MaterialPageRoute<void>(
                        builder: (_) => const Scaffold(
                            body: Center(child: Text('Sub page'))))),
                child: const Text('Open sub page'),
              ),
            ),
          ]));
      await _settle(tester);
      await tester.tap(_destination(WorkspaceDestination.runtime));
      await _settle(tester);
      await tester.tap(find.text('Open sub page'));
      await _settle(tester);
      expect(find.text('Sub page'), findsOneWidget);
      expect(find.byKey(const Key('shell-sidebar')), findsOneWidget);
      final sub = tester.getTopLeft(find.byType(Scaffold).last).dx;
      expect(sub, greaterThanOrEqualTo(264));

      await tester.binding.handlePopRoute();
      await _settle(tester);
      expect(find.text('Sub page'), findsNothing);
      expect(find.text('Runtime page'), findsOneWidget);
      await unmountChat(tester);
    });
  });

  group('boot', () {
    testWidgets('a branded splash, never a bare spinner, then the shell',
        (tester) async {
      SharedPreferences.setMockInitialValues(<String, Object>{});
      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      expect(find.byKey(const Key('shell-splash')), findsOneWidget);
      expect(find.byType(SonderMark), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsNothing);
      await tester.pumpAndSettle();
      expect(find.byType(AppShell), findsOneWidget);
      expect(find.byKey(const Key('shell-splash')), findsNothing);
    });

    testWidgets('the remembered rail state applies from the first frame',
        (tester) async {
      tester.view.physicalSize = _desk;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      SharedPreferences.setMockInitialValues(<String, Object>{
        ShellPreferences.sidebarCollapsedKey: true,
      });
      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();
      expect(tester.getSize(find.byKey(const Key('shell-sidebar'))).width, 64);
      await tester.pumpWidget(const SizedBox());
    });
  });
}
