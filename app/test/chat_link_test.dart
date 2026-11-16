import 'dart:async';
import 'dart:convert';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/chat/drawer.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/shell/app_shell.dart';
import 'package:sonder_runtime/shell/chat_link.dart';
import 'package:sonder_runtime/theme.dart';

import 'chat_fakes.dart';

const _id = 'chat-1794840126388000';
const _origin = 'http://127.0.0.1:8000';
final _now = DateTime(2026, 9, 30, 15);

Map<String, Object> _seed(
    {String bProject = 'engine', Map<String, int>? sessions}) {
  String thread(String id, String text, DateTime at,
          {String project = 'engine'}) =>
      jsonEncode({
        'id': id,
        'title': text,
        'project': project,
        'created_at': at.toIso8601String(),
        'updated_at': at.toIso8601String(),
        'messages': [
          ChatMessage(role: Role.user, content: text).toJson(),
          const ChatMessage(role: Role.assistant, content: 'ok').toJson(),
        ],
      });
  return {
    'sonder_chat_v2/thread-a.json': thread('a', 'Shader cache', _now),
    'sonder_chat_v2/thread-b.json': thread(
        'b', 'Pool on node1', _now.subtract(const Duration(hours: 2)),
        project: bProject),
    'sonder_chat_v2/thread-bad id.json': thread(
        'bad id', 'Odd id', _now.subtract(const Duration(hours: 4))),
    if (sessions != null) 'sonder_chat_v2/sessions.json': jsonEncode(sessions),
    'sonder_chat_v2/thread-$_id.json':
        thread(_id, 'Linked chat', _now.subtract(const Duration(hours: 3))),
    'sonder_chat_v2/migrated-v1': 'yes',
  };
}

Future<void> _settle(WidgetTester tester) async {
  for (var i = 0; i < 8; i++) {
    await tester.pump(const Duration(milliseconds: 60));
  }
}

/// Records what the app puts on the system clipboard.
List<String> _mockClipboard(WidgetTester tester, {bool fail = false}) {
  final copied = <String>[];
  tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(SystemChannels.platform, (call) async {
    if (call.method == 'Clipboard.setData') {
      if (fail) throw PlatformException(code: 'unavailable');
      copied.add((call.arguments as Map)['text'] as String);
    }
    return null;
  });
  addTearDown(() => tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(SystemChannels.platform, null));
  return copied;
}

final _navigator = GlobalKey<NavigatorState>();

Future<void> _pump(WidgetTester tester, ValueNotifier<ChatLinkRequest?> link,
    {Size size = const Size(1440, 900),
    Map<String, Object>? prefs,
    bool routed = false}) async {
  SharedPreferences.setMockInitialValues(prefs ?? _seed());
  ChatStore.backend = PrefsChatStoreBackend();
  ChatStore.resetCache();
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1.0;
  final settings = await Settings.load();
  await tester.pumpWidget(MaterialApp(
    navigatorKey: _navigator,
    onGenerateRoute: routed ? (s) => chatLinkRoute(s, into: link) : null,
    theme: SonderTheme.light,
    darkTheme: SonderTheme.dark,
    themeMode: ThemeMode.dark,
    home: AppShell(
      settings: settings,
      onSettingsChanged: (_) {},
      backendFactory: (_) => FakeChatBackend(),
      approvals: () async => null,
      clock: () => _now,
      deepLink: link,
      shareOrigin: _origin,
    ),
  ));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 50));
  await _settle(tester);
}

bool _selected(WidgetTester tester, String id) =>
    tester.widget<ThreadRow>(find.byKey(ValueKey(id))).selected;

void main() {
  group('format', () {
    test('code and link', () {
      expect(chatShareCode(_id), 'sonder:chat/$_id');
      expect(chatShareLink(_id, 'http://127.0.0.1:8000'),
          'http://127.0.0.1:8000/app/#/c/$_id');
      expect(chatShareLink(_id, 'https://box.local:9443/'),
          'https://box.local:9443/app/#/c/$_id');
      expect(chatShareLink(_id, 'http://localhost'),
          'http://localhost/app/#/c/$_id');
      expect(chatShareLink(_id, null), isNull);
      expect(chatShareLink(_id, ''), isNull);
      expect(chatShareLink(_id, 'file:///C:/app'), isNull);
    });

    test('id validation matches the server rule', () {
      for (final ok in [_id, 'a', 'chat-1-2', 'a.b_c-d', 'x' * 128]) {
        expect(isValidChatRef(ok), isTrue, reason: ok);
      }
      for (final bad in [
        '',
        '.',
        '..',
        'a/b',
        r'a\b',
        '../x',
        'a b',
        'a%2fb',
        'x' * 129,
        'caf\u00e9',
        'a\n',
      ]) {
        expect(isValidChatRef(bad), isFalse, reason: bad);
      }
    });

    test('route parsing', () {
      expect(chatRefFromRoute('/c/$_id'), _id);
      expect(chatRefFromRoute('#/c/$_id'), _id);
      expect(chatRefFromRoute('/c/$_id/'), _id);
      expect(chatRefFromRoute('/c/$_id?x=1'), _id);
      expect(chatRefFromRoute('/c/%61bc'), '%61bc');
      expect(isValidChatRef('%61bc'), isFalse);
      expect(chatRefFromRoute('/c/'), isNull);
      expect(chatRefFromRoute('/c'), isNull);
      expect(chatRefFromRoute('/settings'), isNull);
      expect(chatRefFromRoute(null), isNull);
    });
  });

  group('share menu', () {
    testWidgets('right click offers Copy link and Copy chat code',
        (tester) async {
      final copied = _mockClipboard(tester);
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link);
      final row = find.byKey(const ValueKey('b'));

      await tester.tap(row, buttons: kSecondaryButton);
      await _settle(tester);
      expect(find.text('Copy link'), findsOneWidget);
      expect(find.text('Copy chat code'), findsOneWidget);
      await tester.tap(find.byKey(const Key('thread-menu-copy-link')));
      await _settle(tester);
      expect(copied, ['$_origin/app/#/c/b']);
      expect(find.text('Copied'), findsOneWidget);

      await tester.tap(row, buttons: kSecondaryButton);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('thread-menu-copy-code')));
      await _settle(tester);
      expect(copied, ['$_origin/app/#/c/b', 'sonder:chat/b']);
      await unmountChat(tester);
    });

    testWidgets('the ... button shows on hover and opens the same menu',
        (tester) async {
      final copied = _mockClipboard(tester);
      await _pump(tester, ValueNotifier<ChatLinkRequest?>(null));
      final row = find.byKey(const ValueKey(_id));
      expect(
          find.descendant(
              of: row, matching: find.byKey(const Key('thread-menu-button'))),
          findsNothing);
      final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
      await mouse.addPointer(location: Offset.zero);
      addTearDown(mouse.removePointer);
      await mouse.moveTo(tester.getCenter(row));
      await tester.pump();
      final button = find.descendant(
          of: row, matching: find.byKey(const Key('thread-menu-button')));
      expect(button, findsOneWidget);
      await tester.tap(button);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('thread-menu-copy-code')));
      await _settle(tester);
      expect(copied, ['sonder:chat/$_id']);
      await unmountChat(tester);
    });

    testWidgets('touch: long press opens the menu and Delete stays in it',
        (tester) async {
      final copied = _mockClipboard(tester);
      await _pump(tester, ValueNotifier<ChatLinkRequest?>(null),
          size: const Size(390, 844));
      await tester.tap(find.byKey(const Key('shell-menu')));
      await _settle(tester);
      // Touch has no hover: the button is on every row.
      expect(find.byKey(const Key('thread-menu-button')), findsWidgets);
      await tester.longPress(find.byKey(const ValueKey('b')));
      await _settle(tester);
      expect(find.text('Copy link'), findsOneWidget);
      expect(find.text('Delete chat'), findsOneWidget);
      await tester.tap(find.byKey(const Key('thread-menu-copy-link')));
      await _settle(tester);
      expect(copied, ['$_origin/app/#/c/b']);
      await unmountChat(tester);
    });

    testWidgets(
        'no usable origin: the link item says so, the code still copies',
        (tester) async {
      final copied = _mockClipboard(tester);
      SharedPreferences.setMockInitialValues(_seed());
      ChatStore.backend = PrefsChatStoreBackend();
      ChatStore.resetCache();
      tester.view.physicalSize = const Size(1440, 900);
      tester.view.devicePixelRatio = 1.0;
      final settings = await Settings.load();
      settings.serverUrl = 'not a url';
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AppShell(
          settings: settings,
          onSettingsChanged: (_) {},
          backendFactory: (_) => FakeChatBackend(),
          approvals: () async => null,
          clock: () => _now,
          deepLink: ValueNotifier<ChatLinkRequest?>(null),
        ),
      ));
      await _settle(tester);
      await tester.tap(find.byKey(const ValueKey('b')),
          buttons: kSecondaryButton);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('thread-menu-copy-link')));
      await _settle(tester);
      expect(copied, isEmpty);
      expect(find.textContaining('No link is available'), findsOneWidget);
      await unmountChat(tester);
    });
  });

  group('deep link', () {
    testWidgets('a known id selects that chat', (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link);
      expect(_selected(tester, 'a'), isTrue);
      ChatDeepLink.request(_id, into: link);
      await _settle(tester);
      expect(_selected(tester, _id), isTrue);
      expect(_selected(tester, 'a'), isFalse);
      expect(find.byKey(const Key('chat-not-found')), findsNothing);
      await unmountChat(tester);
    });

    testWidgets('a link present before chats load is applied after',
        (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      ChatDeepLink.request('b', into: link);
      await _pump(tester, link);
      expect(_selected(tester, 'b'), isTrue);
      await unmountChat(tester);
    });

    testWidgets('an unknown id shows the not-found state', (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link);
      ChatDeepLink.request('chat-9999', into: link);
      await _settle(tester);
      expect(find.byKey(const Key('chat-not-found')), findsOneWidget);
      expect(find.text('Chat not found'), findsOneWidget);
      expect(_selected(tester, 'a'), isTrue);
      await unmountChat(tester);
    });

    testWidgets('a malformed id is rejected without a lookup', (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link);
      ChatDeepLink.request('../etc/passwd', into: link);
      await _settle(tester);
      expect(find.byKey(const Key('chat-not-found')), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('the /c/<id> route hands the id over and leaves no page',
        (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      final navigator = GlobalKey<NavigatorState>();
      await tester.pumpWidget(MaterialApp(
        navigatorKey: navigator,
        onGenerateRoute: (settings) => chatLinkRoute(settings, into: link),
        home: const Scaffold(body: Text('home')),
      ));
      unawaited(navigator.currentState!.pushNamed('/c/b'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 50));
      expect(link.value?.ref, 'b');
      expect(find.text('home'), findsOneWidget);
      expect(navigator.currentState!.canPop(), isFalse);
    });
  });

  group('review fixes', () {
    testWidgets('a cold start at /c/<id>/ opens the chat once', (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      final requests = <String>[];
      link.addListener(() => requests.add(link.value!.ref));
      final navigator = GlobalKey<NavigatorState>();
      await tester.pumpWidget(MaterialApp(
        navigatorKey: navigator,
        initialRoute: '/c/b/',
        onGenerateRoute: (settings) => chatLinkRoute(settings, into: link),
        home: const Text('home'),
      ));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 50));
      expect(requests, ['b']);
      expect(navigator.currentState!.canPop(), isFalse);
      expect(find.text('home'), findsOneWidget);
    });

    testWidgets('a link outside the project filter resets it, not mid-build',
        (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link, prefs: _seed(bProject: 'release'), routed: true);
      await tester.tap(find.byKey(const Key('shell-project-filter')));
      await _settle(tester);
      await tester.tap(find.text('engine').last);
      await _settle(tester);
      expect(find.byKey(const ValueKey('b')), findsNothing);
      unawaited(_navigator.currentState!.pushNamed('/c/b'));
      await _settle(tester);
      expect(tester.takeException(), isNull);
      expect(find.byKey(const ValueKey('b')), findsOneWidget);
      expect(_selected(tester, 'b'), isTrue);
      await unmountChat(tester);
    });

    testWidgets('a clipboard that refuses says so', (tester) async {
      final copied = _mockClipboard(tester, fail: true);
      await _pump(tester, ValueNotifier<ChatLinkRequest?>(null));
      await tester.tap(find.byKey(const ValueKey('b')),
          buttons: kSecondaryButton);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('thread-menu-copy-code')));
      await _settle(tester);
      expect(copied, isEmpty);
      expect(tester.takeException(), isNull);
      expect(find.text('Could not copy'), findsOneWidget);
      expect(find.text('Copied'), findsNothing);
      await unmountChat(tester);
    });

    testWidgets('a rotated chat shares its server session and that opens it',
        (tester) async {
      final copied = _mockClipboard(tester);
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link, prefs: _seed(sessions: {'b': 2}));
      await tester.tap(find.byKey(const ValueKey('b')),
          buttons: kSecondaryButton);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('thread-menu-copy-link')));
      await _settle(tester);
      expect(copied, ['$_origin/app/#/c/b-2']);
      // Open the copied link from another chat.
      expect(_selected(tester, 'a'), isTrue);
      final route = copied.single.substring(copied.single.indexOf('#') + 1);
      ChatDeepLink.request(chatRefFromRoute(route)!, into: link);
      await _settle(tester);
      expect(find.byKey(const Key('chat-not-found')), findsNothing);
      expect(_selected(tester, 'b'), isTrue);
      await unmountChat(tester);
    });

    testWidgets('the thread id of a rotated chat still opens it',
        (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link, prefs: _seed(sessions: {'b': 2}));
      ChatDeepLink.request('b', into: link);
      await _settle(tester);
      expect(_selected(tester, 'b'), isTrue);
      await unmountChat(tester);
    });

    testWidgets('a session suffix the chat never reached does not match',
        (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link, prefs: _seed(sessions: {'b': 2}));
      ChatDeepLink.request('b-1', into: link);
      await _settle(tester);
      expect(find.byKey(const Key('chat-not-found')), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('a handled link is not reopened by later chat changes',
        (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link);
      ChatDeepLink.request('b', into: link);
      await _settle(tester);
      expect(_selected(tester, 'b'), isTrue);
      await tester.tap(find.byKey(const ValueKey('a')));
      await _settle(tester);
      expect(_selected(tester, 'a'), isTrue);
      expect(_selected(tester, 'b'), isFalse);
      await unmountChat(tester);
    });

    testWidgets('an id that fails the format check is refused even if stored',
        (tester) async {
      final link = ValueNotifier<ChatLinkRequest?>(null);
      await _pump(tester, link);
      expect(find.byKey(const ValueKey('bad id')), findsOneWidget);
      ChatDeepLink.request('bad id', into: link);
      await _settle(tester);
      expect(find.byKey(const Key('chat-not-found')), findsOneWidget);
      expect(_selected(tester, 'bad id'), isFalse);
      await unmountChat(tester);
    });
  });
}
