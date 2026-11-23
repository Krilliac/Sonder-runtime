import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/composer.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/session_persona.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings/persona_choice.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/theme.dart';

import 'chat_fakes.dart';
import 'fixtures/server_fixtures.dart';
import 'goldens/golden_fonts.dart';
import 'settings_connect_test.dart'
    show FakeConnection, pumpSettings, saveSettings;

http.StreamedResponse _jsonStreamed(String text) => http.StreamedResponse(
      Stream.value(utf8.encode(jsonEncode({
        'choices': [
          {
            'message': {'role': 'assistant', 'content': text},
            'finish_reason': 'stop',
          }
        ]
      }))),
      200,
      headers: const {'content-type': 'application/json'},
    );

SystemInfo _context(int used, int limit) => SystemInfo.fromJson({
      'context': {'context_limit': limit, 'estimated_tokens': used},
    });

void main() {
  setUpAll(loadGoldenFonts);

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
  });

  group('persona normalization and parsing', () {
    test('exposes exact canonical names and options', () {
      expect(personaNames, const [
        'architect',
        'coder',
        'concise',
        'creative',
        'explainer',
        'mathematician',
        'reviewer',
        'security',
        'teacher',
      ]);
      expect(personaOptions, const [null, 'off', ...personaNames]);
    });

    test('normalizePersona handles canonical names, aliases, auto, and unknown',
        () {
      expect(normalizePersona(null), isNull);
      expect(normalizePersona(''), isNull);
      expect(normalizePersona('   '), isNull);
      expect(normalizePersona('auto'), isNull);
      expect(normalizePersona('DEFAULT'), isNull);
      expect(normalizePersona('reset'), isNull);
      expect(normalizePersona('bogus'), isNull);

      expect(normalizePersona('off'), 'off');
      expect(normalizePersona('none'), 'off');
      expect(normalizePersona('disabled'), 'off');
      expect(normalizePersona('DISABLE'), 'off');

      expect(normalizePersona('math'), 'mathematician');
      expect(normalizePersona('sec'), 'security');
      expect(normalizePersona('arch'), 'architect');
      expect(normalizePersona('review'), 'reviewer');
      expect(normalizePersona('explain'), 'explainer');
      expect(normalizePersona('teach'), 'teacher');
      expect(normalizePersona('brief'), 'concise');
      expect(normalizePersona('short'), 'concise');
      expect(normalizePersona('terse'), 'concise');

      for (final name in personaNames) {
        expect(normalizePersona('  ${name.toUpperCase()}  '), name);
      }
    });

    test('parsePersonaInput distinguishes recognized values from unknown', () {
      expect(parsePersonaInput(null), (false, null));
      expect(parsePersonaInput(''), (false, null));
      expect(parsePersonaInput('   '), (false, null));
      expect(parsePersonaInput('auto'), (true, null));
      expect(parsePersonaInput('default'), (true, null));
      expect(parsePersonaInput('reset'), (true, null));
      expect(parsePersonaInput('off'), (true, 'off'));
      expect(parsePersonaInput('none'), (true, 'off'));
      expect(parsePersonaInput('disabled'), (true, 'off'));
      expect(parsePersonaInput('math'), (true, 'mathematician'));
      expect(parsePersonaInput('sec'), (true, 'security'));
      expect(parsePersonaInput('arch'), (true, 'architect'));
      expect(parsePersonaInput('review'), (true, 'reviewer'));
      expect(parsePersonaInput('explain'), (true, 'explainer'));
      expect(parsePersonaInput('teach'), (true, 'teacher'));
      expect(parsePersonaInput('brief'), (true, 'concise'));
      expect(parsePersonaInput('short'), (true, 'concise'));
      expect(parsePersonaInput('terse'), (true, 'concise'));
      expect(parsePersonaInput('CODER'), (true, 'coder'));
      expect(parsePersonaInput('wizard'), (false, null));
    });

    test('labels and descriptions cover every persona option', () {
      expect(personaLabel(null), 'auto');
      expect(personaLabel('off'), 'off');
      expect(personaLabel('math'), 'mathematician');

      expect(personaShortLabel(null), 'Auto');
      expect(personaShortLabel('off'), 'Off');
      expect(personaShortLabel('architect'), 'Architect');
      expect(personaShortLabel('coder'), 'Coder');
      expect(personaShortLabel('concise'), 'Concise');
      expect(personaShortLabel('creative'), 'Creative');
      expect(personaShortLabel('explainer'), 'Explainer');
      expect(personaShortLabel('mathematician'), 'Math');
      expect(personaShortLabel('reviewer'), 'Reviewer');
      expect(personaShortLabel('security'), 'Security');
      expect(personaShortLabel('teacher'), 'Teacher');

      expect(personaDescription(null), 'Auto-route by tier and prompt');
      for (final opt in personaOptions) {
        expect(personaDescription(opt), isNotEmpty, reason: opt ?? 'auto');
      }
    });
  });

  group('/persona composer intercept', () {
    test('classifyIntercept parses bare, valid, alias, auto, off, and invalid',
        () {
      final bare = classifyIntercept('/persona');
      expect(bare, isA<PersonaIntercept>());
      expect((bare as PersonaIntercept).hasValue, isFalse);
      expect(bare.target, isNull);
      expect(bare.invalidArg, isNull);

      final setCoder = classifyIntercept('/persona coder') as PersonaIntercept;
      expect(setCoder.hasValue, isTrue);
      expect(setCoder.target, 'coder');

      final setAlias = classifyIntercept('/persona math') as PersonaIntercept;
      expect(setAlias.hasValue, isTrue);
      expect(setAlias.target, 'mathematician');

      final setOff = classifyIntercept('/persona none') as PersonaIntercept;
      expect(setOff.hasValue, isTrue);
      expect(setOff.target, 'off');

      final setAuto = classifyIntercept('/persona auto') as PersonaIntercept;
      expect(setAuto.hasValue, isTrue);
      expect(setAuto.target, isNull);

      final bad = classifyIntercept('/persona pirate') as PersonaIntercept;
      expect(bad.hasValue, isFalse);
      expect(bad.invalidArg, 'pirate');
    });

    test('interceptLabel identifies /persona', () {
      expect(interceptLabel('/persona'), 'sets persona');
      expect(interceptLabel('/persona coder'), 'sets persona');
    });
  });

  group('SessionPersonaSync and wire payload', () {
    test('omits untouched auto, sends pinned values, and sends auto on reset',
        () {
      final sync = SessionPersonaSync();
      expect(sync.pending('s1', null), isNull);

      // Pinned values always send while pinned.
      expect(sync.pending('s1', 'coder'), 'coder');
      sync.confirm('s1', 'coder');
      expect(sync.pending('s1', 'coder'), 'coder');

      // Switching back to Auto sends 'auto' once until confirmed.
      expect(sync.pending('s1', null), 'auto');
      sync.forget('s1', sent: 'auto');
      expect(sync.pending('s1', null), 'auto');
      sync.confirm('s1', 'auto');
      expect(sync.pending('s1', null), isNull);

      // Explicit Auto on an untouched session still sends 'auto' once.
      sync.requestAuto('s2');
      expect(sync.pending('s2', null), 'auto');
      sync.confirm('s2', 'auto');
      expect(sync.pending('s2', null), isNull);

      sync.reset();
      expect(sync.pending('s1', null), isNull);
      expect(sync.pending('s1', 'off'), 'off');
    });

    test('SonderApi omits persona unless provided on chatStream and chatDetailed',
        () async {
      final streamBodies = <Map<String, dynamic>>[];
      await recordStreamingClients(() async {
        final backend =
            SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435');
        for (final p in <String?>[null, 'coder', 'off', 'auto']) {
          final turn = backend.startTurn(TurnRequest(
            history: const [ChatMessage(role: Role.user, content: 'hi')],
            model: 'sonder',
            sessionId: 's1',
            persona: p,
          ));
          await turn.events.drain<void>();
        }
      }, (request, body) async {
        streamBodies
            .add(jsonDecode(await body.bytesToString()) as Map<String, dynamic>);
        return _jsonStreamed('ok');
      });

      expect(streamBodies, hasLength(4));
      expect(streamBodies[0].containsKey('persona'), isFalse);
      expect(streamBodies[1]['persona'], 'coder');
      expect(streamBodies[2]['persona'], 'off');
      expect(streamBodies[3]['persona'], 'auto');

      final detailedBodies = <Map<String, dynamic>>[];
      await recordClients(() async {
        final api = SonderApi(baseUrl: 'http://127.0.0.1:11435');
        const hi = [ChatMessage(role: Role.user, content: 'hi')];
        await api.chatDetailed(hi);
        await api.chatDetailed(hi, persona: 'security');
      }, (request) async {
        detailedBodies.add(jsonDecode(request.body) as Map<String, dynamic>);
        return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'ok'}
              }
            ]
          }),
          200,
        );
      });
      expect(detailedBodies[0].containsKey('persona'), isFalse);
      expect(detailedBodies[1]['persona'], 'security');
    });

    test('ChatController omits untouched persona, sends pin, and sends auto reset',
        () async {
      final backend = FakeChatBackend()..autoReply = 'ok';
      final controller = ChatController(backend, model: 'sonder');
      await controller.loadThreads();

      // Default untouched Auto leaves persona out of the request.
      await controller.send('default turn');
      expect(backend.turns.last.request.persona, isNull);

      // Pinning sends on every turn while pinned.
      controller.selectPersona('sec');
      expect(controller.persona, 'security');
      await controller.send('audit this');
      expect(backend.turns.last.request.persona, 'security');
      await controller.send('check headers too');
      expect(backend.turns.last.request.persona, 'security');

      // Switching to off sends 'off'.
      controller.selectPersona('off');
      await controller.send('plain reply');
      expect(backend.turns.last.request.persona, 'off');

      // Toggling back to Auto sends 'auto' once, then omits once confirmed.
      controller.selectPersona(null);
      expect(controller.persona, isNull);
      await controller.send('auto again');
      expect(backend.turns.last.request.persona, 'auto');
      await controller.send('still auto');
      expect(backend.turns.last.request.persona, isNull);

      controller.dispose();
    });
  });

  group('Settings persistence', () {
    test('persists persona under sonder_persona and clears back to Auto',
        () async {
      final s = Settings(persona: 'math');
      expect(s.persona, 'mathematician');
      await s.save();
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('sonder_persona'), 'mathematician');

      final loaded = await Settings.load();
      expect(loaded.persona, 'mathematician');

      loaded.persona = 'brief';
      expect(loaded.persona, 'concise');
      await loaded.save();
      expect((await Settings.load()).persona, 'concise');

      final cleared = loaded.copyWith(autoPersona: true);
      expect(cleared.persona, isNull);
      await cleared.save();
      expect(prefs.containsKey('sonder_persona'), isFalse);
      expect((await Settings.load()).persona, isNull);
    });

    test('garbled sonder_persona falls back to Auto', () async {
      SharedPreferences.setMockInitialValues({'sonder_persona': 'not-real'});
      final loaded = await Settings.load();
      expect(loaded.persona, isNull);
    });
  });

  group('widgets and composer integration', () {
    testWidgets('PersonaSelector and PersonaPickerButton update selection',
        (tester) async {
      String? picked = 'coder';
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: StatefulBuilder(
              builder: (context, setState) => Column(
                children: [
                  PersonaSelector(
                    value: picked,
                    onChanged: (v) => setState(() => picked = v),
                  ),
                  PersonaPickerButton(
                    value: picked,
                    onSelected: (v) => setState(() => picked = v),
                  ),
                ],
              ),
            ),
          ),
        ),
      );

      expect(find.text('Persona: Coder'), findsOneWidget);

      await tester.tap(find.byKey(const Key('settings-persona-security')));
      await tester.pumpAndSettle();
      expect(picked, 'security');
      expect(find.text('Persona: Security'), findsOneWidget);

      await tester.tap(find.byKey(const Key('composer-persona-picker')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('persona-option-mathematician')));
      await tester.pumpAndSettle();
      expect(picked, 'mathematician');
      expect(find.text('Persona: Math'), findsOneWidget);

      await tester.tap(find.byKey(const Key('composer-persona-picker')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('persona-option-auto')));
      await tester.pumpAndSettle();
      expect(picked, isNull);
      expect(find.text('Persona: Auto'), findsOneWidget);
    });

    testWidgets('/persona command updates ChatScreen state, payload, and pill',
        (tester) async {
      final backend = FakeChatBackend()..autoReply = 'ok';
      final settings =
          await pumpChat(tester, backend, size: const Size(1200, 900));

      expect(find.byKey(const Key('composer-persona-picker')), findsOneWidget);
      expect(find.text('Persona: Auto'), findsOneWidget);

      await tester.enterText(find.byType(TextField), '/persona coder');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(backend.turns, isEmpty);
      expect(settings.persona, 'coder');
      expect(find.text('Persona: Coder'), findsOneWidget);

      // Next real chat message carries persona: 'coder'.
      await tester.enterText(find.byType(TextField), 'write a parser');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(backend.turns, hasLength(1));
      expect(backend.lastTurn.request.persona, 'coder');

      // Invalid /persona shows feedback and does not send a turn.
      await tester.enterText(find.byType(TextField), '/persona bogus');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(backend.turns, hasLength(1));
      expect(find.textContaining("Unknown persona 'bogus'"), findsOneWidget);

      // /persona auto resets and sends persona: 'auto' on the next turn.
      await tester.enterText(find.byType(TextField), '/persona auto');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(settings.persona, isNull);
      expect(find.text('Persona: Auto'), findsOneWidget);

      await tester.enterText(find.byType(TextField), 'back to auto');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(backend.turns, hasLength(2));
      expect(backend.lastTurn.request.persona, 'auto');

      await unmountChat(tester);
    });

    testWidgets('composer strip has zero overflow across narrow to desktop widths',
        (tester) async {
      for (final size in const [
        Size(320, 700),
        Size(390, 844),
        Size(500, 800),
        Size(800, 900),
        Size(1200, 900),
      ]) {
        final backend = FakeChatBackend()
          ..mode = permissionModeFor('acceptEdits')
          ..models = const ['sonder', 'qwen2.5-coder:7b-instruct-q4_K_M']
          ..statusInfo = _context(2100, 8192);
        await pumpChat(
          tester,
          backend,
          size: size,
          prefs: const {'sonder_persona': 'architect'},
        );
        await tester.pump(const Duration(milliseconds: 200));
        expect(tester.takeException(), isNull, reason: 'overflow at $size');
        if (size.width >= 500) {
          expect(find.byKey(const Key('composer-persona-picker')),
              findsOneWidget,
              reason: 'picker visible at $size');
        }
        await unmountChat(tester);
      }
    });

    testWidgets('Settings General page stages and saves Persona',
        (tester) async {
      await pumpSettings(
        tester,
        connection: FakeConnection(),
        category: SettingsCategory.general,
      );
      final chip = find.byKey(const Key('settings-persona-reviewer'));
      await tester.ensureVisible(chip);
      await tester.tap(chip);
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('settings-save')), findsOneWidget);
      await saveSettings(tester);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('sonder_persona'), 'reviewer');
    });
  });
}
