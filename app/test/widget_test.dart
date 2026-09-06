import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/main.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/system_screen.dart';

void main() {
  testWidgets('desktop workspace navigation connects chat, agents and settings', (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    tester.view.physicalSize = const Size(1200, 850);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final client = MockClient((request) async => http.Response('{}', 200));
    await http.runWithClient(() async {
      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Agents'));
      await tester.pumpAndSettle();
      expect(find.text('Conversations'), findsOneWidget);
      await tester.tap(find.byTooltip('Workspace navigation'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Settings'));
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
      await tester.tap(find.text('Chat'));
      await tester.pumpAndSettle();
      expect(find.text('New chat'), findsWidgets);
      await tester.pumpWidget(const SizedBox());
    }, () => client);
    tester.view.resetPhysicalSize(); tester.view.resetDevicePixelRatio();
  });

  testWidgets('App boots to the chat screen', (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
    await tester.pumpAndSettle();

    // Model picker in the title bar shows the local model once settings resolve.
    expect(find.textContaining('sonder'), findsWidgets);
    expect(find.text('Sonder Runtime'), findsOneWidget);
    expect(find.textContaining('Not a standalone model'), findsOneWidget);
    expect(find.textContaining('served locally by Ollama'), findsOneWidget);
    // Empty state shows the message composer.
    expect(find.byType(TextField), findsOneWidget);
    // The telemetry strip stays legible and truthful before a server reply:
    // unavailable data is an em dash, never a fabricated zero.
    expect(find.byKey(const Key('status-metric-context')), findsOneWidget);
    expect(find.byKey(const Key('status-metric-activity')), findsOneWidget);
    expect(find.byKey(const Key('status-metric-route')), findsOneWidget);
  });

  testWidgets('Desktop command shortcut opens the command browser', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byType(TextField));
    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.keyK);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('command-browser')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Desktop chat layout keeps the conversation rail visible', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
    await tester.pumpAndSettle();

    // Wide windows get a persistent rail instead of hiding chat navigation
    // behind the mobile drawer gesture.
    expect(find.textContaining('Local-first workspace'), findsOneWidget);
    expect(find.text('Chats'), findsOneWidget);
  });

  testWidgets(
    'Commands browser opens on categories and searches the whole catalog',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 900));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      SharedPreferences.setMockInitialValues(<String, Object>{});

      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Commands'));
      await tester.pumpAndSettle();

      // No server in a widget test, so this is the offline fallback catalog --
      // and it says so rather than passing a short list off as the real surface.
      expect(find.byKey(const Key('command-browser')), findsOneWidget);
      expect(
        find.byKey(const Key('command-browser-categories')),
        findsOneWidget,
      );
      expect(find.byKey(const Key('command-category-quick')), findsOneWidget);
      expect(find.textContaining('Server catalog unavailable'), findsOneWidget);

      // Search cuts across every category, not just the open one.
      await tester.enterText(
        find.byKey(const Key('command-browser-search')),
        'asset',
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('/asset office-suite'), findsOneWidget);
      expect(find.textContaining('/asset media-suite'), findsOneWidget);
      expect(find.textContaining('/asset rigged-character'), findsOneWidget);
      expect(
        find.text('Generate a grounded editable media kit'),
        findsOneWidget,
      );
      expect(
        find.text('Generate a grounded animated humanoid character'),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'Commands browser drills into a category and fills the composer',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 900));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      SharedPreferences.setMockInitialValues(<String, Object>{});

      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Commands'));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('command-category-quick')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('command-browser-commands')), findsOneWidget);
      expect(find.byKey(const Key('command-browser-categories')), findsNothing);

      // Scoped to the dialog: the empty state behind it also offers "/stats".
      await tester.tap(
        find.descendant(
          of: find.byKey(const Key('command-browser')),
          matching: find.text('/stats'),
        ),
      );
      await tester.pumpAndSettle();

      // Picking loads the command into the composer rather than sending it,
      // because most commands still need arguments typed.
      expect(find.byKey(const Key('command-browser')), findsNothing);
      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '/stats ');
    },
  );

  testWidgets('Typing "/" browses popular commands, more characters narrow', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
    await tester.pumpAndSettle();

    // The empty state also offers a "/stats" chip, so every palette
    // assertion is scoped to the palette itself.
    Finder inPalette(Finder matching) => find.descendant(
          of: find.byKey(const Key('command-palette')),
          matching: matching,
        );

    await tester.enterText(find.byType(TextField), '/');
    await tester.pumpAndSettle();

    // A bare slash is a browse: the popular shortlist, labelled by category.
    expect(find.byKey(const Key('command-palette')), findsOneWidget);
    expect(inPalette(find.textContaining('QUICK')), findsOneWidget);
    expect(inPalette(find.text('/help')), findsOneWidget);
    expect(inPalette(find.text('List commands')), findsOneWidget);
    // Popular is a shortlist, not the whole catalog.
    expect(inPalette(find.text('/emotion')), findsNothing);

    // Every further character narrows, and the category heading drops away
    // once the list is a ranked search rather than a browse.
    await tester.enterText(find.byType(TextField), '/stat');
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('command-palette')), findsOneWidget);
    expect(inPalette(find.text('/stats')), findsOneWidget);
    expect(inPalette(find.text('/help')), findsNothing);
    expect(inPalette(find.textContaining('QUICK')), findsNothing);

    // Nothing starts with "/memory", so the looser pass matches summaries.
    await tester.enterText(find.byType(TextField), '/memory');
    await tester.pumpAndSettle();

    expect(inPalette(find.text('/quality')), findsOneWidget);
    expect(inPalette(find.text('/privacy')), findsOneWidget);
  });

  testWidgets('Palette keeps arrow/Enter selection and shows usage lines', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), '/c');
    await tester.pumpAndSettle();

    // /context, /compact, /commands, /checklist, /capacity — in catalog order.
    expect(find.text('/context'), findsOneWidget);
    expect(find.text('/compact'), findsOneWidget);

    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.pumpAndSettle();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pumpAndSettle();

    final field = tester.widget<TextField>(find.byType(TextField));
    expect(field.controller!.text, '/compact ');
    expect(find.byKey(const Key('command-palette')), findsNothing);

    // Commands whose name carries an example payload keep it on the usage
    // line, so a user can see the arguments before picking.
    await tester.enterText(find.byType(TextField), '/asset');
    await tester.pumpAndSettle();
    expect(find.textContaining('/asset office-suite DOCX'), findsOneWidget);
  });

  testWidgets('Palette prefers the server catalog when one is reachable', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    final client = MockClient((request) async {
      if (request.url.path == '/v1/commands') {
        return http.Response(
          jsonEncode({
            'commands': [
              {
                'name': '/task_plan',
                'aliases': ['/plan'],
                'tool': 'task_plan',
                'category': 'planning',
                'risk': 'safe',
                'summary': 'Plan a task',
                'usage': '/task_plan <title> <steps>',
              },
            ],
            'categories': {'planning': 'Plans and tasks'},
            'popular': ['/task_plan'],
          }),
          200,
        );
      }
      return http.Response('{}', 503);
    });

    await http.runWithClient(() async {
      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), '/');
      await tester.pumpAndSettle();

      // Server rows, not the built-in fallback ones.
      expect(find.text('/task_plan'), findsOneWidget);
      expect(find.text('Plan a task'), findsOneWidget);
      expect(find.textContaining('/task_plan <title> <steps>'), findsOneWidget);
      expect(find.text('/help'), findsNothing);

      // The alias resolves through the cached catalog, client-side.
      await tester.enterText(find.byType(TextField), '/plan');
      await tester.pumpAndSettle();
      expect(find.text('/task_plan'), findsOneWidget);
      expect(find.text('aliases: /plan'), findsOneWidget);
    }, () => client);
  });

  testWidgets('System always has an explicit return to main chat', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('System'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('System'), findsOneWidget);
    expect(find.text('Runtime architecture'), findsOneWidget);
    expect(
      find.textContaining('not a standalone foundation model'),
      findsOneWidget,
    );
    expect(
      find.textContaining('training runs through PEFT/Hugging Face'),
      findsOneWidget,
    );
    expect(find.byTooltip('Back to chat'), findsOneWidget);
    expect(find.text('Chat'), findsOneWidget);
    expect(find.byKey(const Key('system-section-nav')), findsOneWidget);
    expect(
      find.descendant(
        of: find.byKey(const Key('system-section-nav')),
        matching: find.text('Learning'),
      ),
      findsOneWidget,
    );

    await tester.tap(find.byTooltip('Back to chat'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('New chat'), findsOneWidget);
  });

  testWidgets('System exposes persistent autopilot goal controls', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('System'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    final list = find.byType(Scrollable).first;
    await tester.scrollUntilVisible(
      find.byKey(const Key('autopilot-goal')),
      240,
      scrollable: list,
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('Autopilot'), findsOneWidget);
    expect(find.byKey(const Key('autopilot-goal')), findsOneWidget);
    expect(find.byKey(const Key('autopilot-plan')), findsOneWidget);
    expect(find.byKey(const Key('autopilot-run')), findsOneWidget);
    expect(find.text('Workspace'), findsOneWidget);
    expect(find.text('Observe only'), findsOneWidget);
  });

  testWidgets('System shows the shared local runtime policy', (tester) async {
    await tester.binding.setSurfaceSize(const Size(1280, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final captureKey = GlobalKey();
    final info = SystemInfo.fromJson({
      'status': 'Ollama local runtime ready',
      'runtime_policy': {
        'revision': 4,
        'path': r'C:\Users\example\AppData\Local\sonder\runtime_policy.json',
        'source': 'runtime_policy_update',
        'error': '',
        'local_models': {
          'fast': 'qwen2.5:3b',
          'code': 'sonder:latest',
          'general': 'qwen2.5:7b-instruct',
        },
        'routing': {
          'router': 'fast',
          'workbench': 'code',
          'autopilot': 'code',
          'fleet': 'code',
          'review': 'general',
        },
        'missing_models': const [],
      },
      'mcp_runtime': {
        'status': 'current',
        'enabled': true,
        'module': '__main__',
        'path': r'C:\sonder\server.py',
        'loaded_digest': '1234567890abcdef',
        'current_digest': '1234567890abcdef',
        'source_changed': false,
        'registered_tools': 108,
        'refresh_count': 3,
        'last_refresh_ts': 1783731000,
        'last_surface_changed': true,
        'last_error': '',
        'last_notification_error': '',
        'protocol_list_changed': true,
      },
      'learning_health': {
        'status': 'healthy',
        'interactions': 4416,
        'outcomes': 3710,
        'outcome_interactions': 3710,
        'good_outcomes': 3596,
        'bad_outcomes': 114,
        'outcome_coverage_percent': 84.0,
        'positive_percent': 96.9,
        'lessons': 974,
        'facts': 8,
        'grounded_lessons': 461,
        'synthetic_lessons': 513,
        'lessons_per_interaction': 0.221,
        'distillation_yield': 0.128,
        'lesson_sources': {'interaction': 461, 'seed': 513},
        'signals': [
          {
            'signal': 'tests_passed',
            'count': 3559,
            'average_reward': 1.0,
            'good': true,
          },
          {
            'signal': 'failed',
            'count': 99,
            'average_reward': -1.0,
            'good': false,
          },
        ],
        'quality': {
          'exact_duplicate_groups': 0,
          'exact_duplicate_prunable': 0,
          'no_embedding': 0,
          'vague_without_anchor': 0,
          'path_or_secret_like': 0,
          'missing_source_interaction': 0,
          'missing_fts': 0,
          'orphan_fts': 0,
          'embedding_percent': 100.0,
        },
      },
      'models': const [],
    });

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData.dark(useMaterial3: true),
        home: RepaintBoundary(
          key: captureKey,
          child: SystemScreen(
            settings: Settings(),
            initialInfo: info,
            liveUpdates: false,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.scrollUntilVisible(
      find.byKey(const Key('runtime-policy-panel')),
      360,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.text('Local Runtime Policy'), findsOneWidget);
    expect(find.text('Shared policy r4'), findsOneWidget);
    expect(find.text('fast  qwen2.5:3b'), findsOneWidget);
    expect(find.text('review  general'), findsOneWidget);
    expect(
      find.textContaining('/runtime set workbench=general'),
      findsOneWidget,
    );

    await tester.scrollUntilVisible(
      find.byKey(const Key('mcp-runtime-panel')),
      280,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.text('Runtime Convergence'), findsOneWidget);
    expect(find.text('MCP current'), findsOneWidget);
    expect(find.text('108 tools'), findsOneWidget);
    expect(find.text('3 atomic refreshes'), findsOneWidget);
    expect(find.text('Live tool-list updates'), findsOneWidget);

    await tester.scrollUntilVisible(
      find.byKey(const Key('learning-health-panel')),
      300,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.text('Learning Quality'), findsOneWidget);
    expect(find.text('Learning healthy'), findsOneWidget);
    expect(find.text('974 lessons'), findsOneWidget);
    expect(find.text('3710 outcomes'), findsOneWidget);
    expect(find.text('interaction  461'), findsOneWidget);
    expect(find.text('seed  513'), findsOneWidget);
    expect(find.text('tests passed  3559'), findsOneWidget);
    expect(find.textContaining('Memory hygiene is clean'), findsOneWidget);

    if (Platform.environment['SONDER_CAPTURE_UI'] == '1') {
      await tester.runAsync(() async {
        final boundary = captureKey.currentContext!.findRenderObject()!
            as RenderRepaintBoundary;
        final image = await boundary.toImage(pixelRatio: 1);
        final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
        final output = File('build/ui-smoke-runtime-policy.png');
        await output.parent.create(recursive: true);
        await output.writeAsBytes(bytes!.buffer.asUint8List(), flush: true);
        image.dispose();
      });
    }
  });

  testWidgets('System shows deployment profile and honest takeover limits', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(900, 1000));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final info = SystemInfo.fromJson({
      'status': 'ready',
      'deployment': {
        'profile': 'pooled-pair',
        'profile_id': 'two-pc',
        'local_node': 'secondary',
        'configured_members': ['secondary', 'primary'],
        'preferred_primary': 'primary',
        'control_state_scope': 'local-instance',
        'preference_confers_authority': false,
        'partition_policy': 'no_promotion_without_fencing_and_acknowledged_data',
        'capabilities': {
          'private_compute': {
            'available': true,
            'reason': 'Configured private-node compute is enabled.',
          },
          'automatic_takeover': {
            'available': false,
            'reason': 'Fencing and acknowledged replication are not integrated.',
          },
          'acknowledged_state_replication': {
            'available': false,
            'reason': 'No replication backend is integrated.',
          },
          'worker_epoch_fencing': {
            'available': false,
            'reason': 'Ownership epochs are not integrated.',
          },
          'quorum': {
            'available': false,
            'reason': 'No quorum provider is integrated.',
          },
        },
      },
    });

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData.dark(useMaterial3: true),
        home: SystemScreen(
          settings: Settings(),
          initialInfo: info,
          liveUpdates: false,
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('deployment-panel')),
      240,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.text('Deployment profile'), findsOneWidget);
    expect(find.text('Two PC (pooled-pair)'), findsOneWidget);
    expect(find.text('secondary, primary'), findsOneWidget);
    expect(
      find.textContaining('Unavailable — Fencing and acknowledged replication'),
      findsOneWidget,
    );
    expect(
      find.text(
        'Primary preference is advisory; it never grants promotion authority.',
      ),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('System shows distributed capability boundaries', (tester) async {
    await tester.binding.setSurfaceSize(const Size(900, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final info = SystemInfo.fromJson({
      'status': 'ready',
      'operational_capabilities': {
        'schema_version': 1,
        'control': {
          'managed_app_work': {
            'available': true,
            'reason': 'Owned dispatcher is installed.',
          },
        },
        'inference': {
          'request_level_pooling': {
            'available': true,
            'reason': 'Requests may route to one worker.',
          },
          'model_sharding': {
            'available': false,
            'reason': 'Tensor sharding is not integrated.',
          },
          'pool': {
            'worker_count': 2,
            'healthy_worker_count': 1,
            'remote_worker_count': 1,
          },
        },
        'compute': {
          'local_node': 'node-a',
          'configured_peer_count': 1,
          'remote_enabled': true,
          'whole_job_placement': {
            'available': true,
            'reason': 'Complete jobs are placed on one node.',
          },
          'indefinite_scale': {
            'available': false,
            'reason': 'External provider required.',
          },
        },
        'mobility': {
          'memory_replication_transport': {
            'available': true,
            'reason': 'Receiver injected.',
          },
          'artifact_transfer_transport': {
            'available': false,
            'reason': 'Explicit grant is disabled.',
          },
          'automatic_memory_migration': {
            'available': false,
            'reason': 'Ownership is not integrated.',
          },
          'automatic_artifact_migration': {
            'available': false,
            'reason': 'Explicit transfer only.',
          },
        },
      },
    });

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData.dark(useMaterial3: true),
        home: SystemScreen(
          settings: Settings(),
          initialInfo: info,
          liveUpdates: false,
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('operational-capabilities-panel')),
      240,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.text('Distributed capability surface'), findsOneWidget);
    expect(
      find.textContaining('Available — Owned dispatcher is installed.'),
      findsOneWidget,
    );
    expect(find.text('1/2 healthy workers; Available — Requests may route to one worker.'), findsOneWidget);
    expect(find.textContaining('Unavailable — Tensor sharding is not integrated.'), findsOneWidget);
    expect(find.textContaining('Unavailable — External provider required.'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('System shows caller-judged work, never the blended rate alone', (
    tester,
  ) async {
    // The blend is dominated by autograded outcomes -- the runtime marking its
    // own curriculum. On the real store it reads 96% where caller-judged work
    // succeeds 53% of the time, and this screen is what a non-CLI user looks
    // at, so a near-full green "Positive" bar was the most misleading thing it
    // could show.
    await tester.binding.setSurfaceSize(const Size(900, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final info = SystemInfo.fromJson({
      'status': 'ready',
      'learning_health': {
        'status': 'attention',
        'interactions': 7865,
        'outcomes': 6831,
        'outcome_interactions': 6826,
        'good_outcomes': 6562,
        'bad_outcomes': 269,
        'outcome_coverage_percent': 86.8,
        'positive_percent': 96.1,
        'reviewed_positive_percent': 52.7,
        'reviewed_outcomes': 186,
        'autograded_positive_percent': 97.3,
        'autograded_outcomes': 6645,
        'lessons': 1061,
        'grounded_lessons': 533,
        'synthetic_lessons': 528,
        'lesson_sources': {'interaction': 533, 'seed': 528},
        'signals': const [],
        'quality': {
          'exact_duplicate_groups': 0,
          'exact_duplicate_prunable': 0,
          'no_embedding': 0,
          'vague_without_anchor': 0,
          'path_or_secret_like': 0,
          'missing_source_interaction': 0,
          'missing_fts': 0,
          'orphan_fts': 0,
          'embedding_percent': 100.0,
        },
      },
      'models': const [],
    });

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData.dark(useMaterial3: true),
        home: SystemScreen(
          settings: Settings(),
          initialInfo: info,
          liveUpdates: false,
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('learning-health-panel')),
      300,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    // The honest number is the one on the meter.
    expect(find.text('Caller-judged'), findsOneWidget);
    expect(find.textContaining('186 judged by a caller'), findsOneWidget);
    // Autograded is shown, but labelled as self-marked rather than as quality.
    expect(find.textContaining('self-marked'), findsOneWidget);
    // The old label must not come back.
    expect(find.text('Positive'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('System learning quality surfaces hygiene warnings', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(900, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final info = SystemInfo.fromJson({
      'status': 'ready',
      'learning_health': {
        'status': 'attention',
        'interactions': 20,
        'outcomes': 10,
        'outcome_interactions': 10,
        'good_outcomes': 5,
        'bad_outcomes': 5,
        'outcome_coverage_percent': 50.0,
        'positive_percent': 50.0,
        'lessons': 4,
        'grounded_lessons': 2,
        'synthetic_lessons': 2,
        'lesson_sources': {'interaction': 2, 'seed': 2},
        'signals': const [],
        'quality': {
          'exact_duplicate_groups': 1,
          'exact_duplicate_prunable': 2,
          'no_embedding': 1,
          'vague_without_anchor': 0,
          'path_or_secret_like': 1,
          'missing_source_interaction': 0,
          'missing_fts': 0,
          'orphan_fts': 0,
          'embedding_percent': 75.0,
        },
      },
      'models': const [],
    });

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData.dark(useMaterial3: true),
        home: SystemScreen(
          settings: Settings(),
          initialInfo: info,
          liveUpdates: false,
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('learning-health-panel')),
      300,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.text('Learning attention'), findsOneWidget);
    expect(find.textContaining('Memory hygiene needs review'), findsOneWidget);
    expect(find.textContaining('2 duplicate rows'), findsOneWidget);
    expect(find.textContaining('1 missing embeddings'), findsOneWidget);
    expect(find.textContaining('1 privacy flags'), findsOneWidget);
  });

  testWidgets('Completed autopilot run renders its persisted ledger', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1280, 1800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final info = SystemInfo.fromJson({
      'status': 'Ollama local runtime ready',
      'stats': '805 checks passed',
      'learn_tiers': 'local tiers: fast, code, general',
      'improvements': 'No urgent improvement items detected.',
      'autopilot': {
        'active_runs': 0,
        'resumable_runs': 0,
        'total_runs': 1,
        'total_listed': 1,
        'latest': {
          'id': 'auto-885ca53e8ef6',
          'objective':
              'Inspect the autonomous controller and verify its completion gates.',
          'project': 'sonder',
          'tier': 'code',
          'policy': 'observe',
          'allow_web': false,
          'status': 'completed',
          'phase': 'completed',
          'cycles': 3,
          'failures': 0,
          'checkpoints': 1,
          'replans': 1,
          'max_failures': 2,
          'max_tasks': 3,
          'max_replans': 2,
          'adaptive': true,
          'summary': 'Objective completed with host-verified task evidence.',
          'final_report': 'autopilot end report\n3 tasks passed\n0 failures',
          'last_error': '',
          'criteria': [
            'Persistence service exists.',
            'Completion gates are enforced.',
          ],
          'plan': [
            {
              'id': 'task-01',
              'title': 'Verify file existence',
              'instruction': 'Inspect both modules.',
              'kind': 'inspect',
              'status': 'passed',
              'attempts': 1,
            },
            {
              'id': 'task-02',
              'title': 'Check persistence',
              'instruction': 'Read the lifecycle store.',
              'kind': 'research',
              'status': 'passed',
              'attempts': 1,
            },
            {
              'id': 'task-03',
              'title': 'Validate completion gates',
              'instruction': 'Ground every success criterion.',
              'kind': 'validate',
              'status': 'passed',
              'attempts': 1,
            },
          ],
        },
        'runs': const [],
        'events': [
          {'event_id': 1, 'kind': 'created', 'message': 'goal created'},
          {'event_id': 2, 'kind': 'planned', 'message': 'plan accepted'},
          {
            'event_id': 3,
            'kind': 'completed',
            'message': 'evidence gates passed',
          },
        ],
      },
      'models': const [],
    });
    final captureKey = GlobalKey();
    final scheme = ColorScheme.fromSeed(
      seedColor: const Color(0xFF63D6C8),
      brightness: Brightness.dark,
    );

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData(
          useMaterial3: true,
          colorScheme: scheme,
          brightness: Brightness.dark,
          scaffoldBackgroundColor: const Color(0xFF0B1117),
          cardTheme: CardThemeData(
            elevation: 0,
            color: const Color(0xFF121B23),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(18),
              side: const BorderSide(color: Color(0xFF24343D)),
            ),
          ),
        ),
        home: RepaintBoundary(
          key: captureKey,
          child: SystemScreen(
            settings: Settings(),
            initialInfo: info,
            liveUpdates: false,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Persistent checklist'), findsOneWidget);
    expect(
      find.text(
        '3/3 tasks settled • 3 cycles • 0/2 failures • '
        '1 checkpoint • 1/2 replans',
      ),
      findsOneWidget,
    );
    expect(find.textContaining('Validate completion gates'), findsWidgets);

    if (Platform.environment['SONDER_CAPTURE_UI'] == '1') {
      await tester.runAsync(() async {
        final boundary = captureKey.currentContext!.findRenderObject()!
            as RenderRepaintBoundary;
        final image = await boundary.toImage(pixelRatio: 1);
        final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
        final output = File('build/ui-smoke-autopilot.png');
        await output.parent.create(recursive: true);
        await output.writeAsBytes(bytes!.buffer.asUint8List(), flush: true);
        image.dispose();
      });
    }
  });

  testWidgets('Settings always has an explicit return to main chat', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Settings'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('Settings'), findsOneWidget);
    expect(find.byTooltip('Back to chat'), findsOneWidget);
    expect(find.text('Chat'), findsOneWidget);

    await tester.tap(find.text('Chat'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('New chat'), findsOneWidget);
  });

  testWidgets('Settings guards unsaved changes before leaving', (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    final settings = Settings();

    await tester.pumpWidget(
      MaterialApp(
        home: SettingsScreen(settings: settings, onChanged: (_) {}),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Runtime architecture'), findsNothing);
    expect(
      find.textContaining('not a standalone foundation model'),
      findsNothing,
    );
    expect(
      find.textContaining('training uses PEFT/Hugging Face'),
      findsNothing,
    );

    await tester.enterText(find.byType(TextField).first, 'http://127.0.0.1:1');
    await tester.pump();
    await tester.tap(find.text('Chat'));
    await tester.pumpAndSettle();

    expect(find.text('Discard unsaved settings?'), findsOneWidget);
    await tester.tap(find.text('Keep editing'));
    await tester.pumpAndSettle();
    expect(find.text('Settings'), findsOneWidget);
  });

  testWidgets('Approximate location is explicit opt-in and persists', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    final settings = Settings();

    await tester.pumpWidget(
      MaterialApp(
        home: SettingsScreen(settings: settings, onChanged: (_) {}),
      ),
    );
    await tester.pumpAndSettle();

    final label = find.text('Allow approximate IP location');
    expect(find.byType(ListView), findsOneWidget);
    await tester.drag(find.byType(ListView), const Offset(0, -600));
    await tester.pumpAndSettle();
    expect(label, findsOneWidget);
    final tile = find.widgetWithText(
      SwitchListTile,
      'Allow approximate IP location',
    );
    expect(tester.widget<SwitchListTile>(tile).value, isFalse);

    await tester.ensureVisible(tile);
    await tester.pumpAndSettle();
    await tester.tap(tile);
    await tester.pump();
    expect(tester.widget<SwitchListTile>(tile).value, isTrue);

    final save = find.text('Save');
    await tester.drag(find.byType(ListView), const Offset(0, -700));
    await tester.pumpAndSettle();
    expect(save, findsOneWidget);
    await tester.tap(save);
    await tester.pumpAndSettle();

    final preferences = await SharedPreferences.getInstance();
    expect(preferences.getBool('sonder_allow_approximate_location'), isTrue);
  });

  testWidgets(
    'Assistant messages render markdown and collapse activity evidence',
    (tester) async {
      final now = DateTime(2026, 7, 10);
      final thread = ChatThread(
        id: 'markdown-test',
        title: 'Rendered response',
        project: 'ui',
        createdAt: now,
        updatedAt: now,
        messages: const [
          ChatMessage(role: Role.user, content: 'show formatting'),
          ChatMessage(
            role: Role.assistant,
            content: '**Bold answer**\n\n```python\nprint("ok")\n```\n\n'
                '=== ACTIVITY (observable work) ===\ntool calls: 1\n=== END ACTIVITY ===',
            responseMetadata: ChatResponseMetadata(
              requestId: 'req_saved_turn',
              model: 'qwen:latest',
              tier: 'code',
              status: 'complete',
              cache: 'hit',
              elapsedMs: 800,
              totalTokens: 42,
            ),
          ),
        ],
      );
      SharedPreferences.setMockInitialValues(<String, Object>{
        'sonder_chat_threads_v1': jsonEncode([thread.toJson()]),
      });

      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();

      expect(find.byType(MarkdownBody), findsOneWidget);
      expect(find.text('Bold answer'), findsOneWidget);
      expect(find.text('Activity evidence'), findsOneWidget);
      expect(find.text('Response details - cached replay'), findsOneWidget);
      expect(find.textContaining('**Bold answer**'), findsNothing);

      await tester.tap(find.text('Response details - cached replay'));
      await tester.pumpAndSettle();
      expect(find.textContaining('request: req_saved_turn'), findsOneWidget);
      expect(find.textContaining('cache: hit (replayed)'), findsOneWidget);
    },
  );

  testWidgets('Workbench activity panel renders checklist and exact actions', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final response = ActivityResponse.fromJson({
      'id': 'r1',
      'label': 'agent:code',
      'status': 'complete',
      'elapsed_ms': 250,
      'tool_calls': 1,
      'model_calls': 2,
      'result_summary': 'Created and validated demo.py',
      'events': [
        {
          'kind': 'tool_call',
          'tool': 'script_run',
          'title': 'Ran Script',
          'command': 'python demo.py',
          'output': 'DEMO_OK',
          'ok': true,
          'elapsed_ms': 90,
        },
      ],
      'checklist': {
        'title': 'Build demo',
        'status': 'done',
        'items': [
          {'id': 'a', 'title': 'Inspect files', 'status': 'done'},
          {'id': 'b', 'title': 'Run validation', 'status': 'done'},
        ],
      },
    });

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData.dark(useMaterial3: true),
        home: Scaffold(
          body: SingleChildScrollView(
            child: WorkbenchActivityPanel(
              response: response,
              totalToolCalls: 7,
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Build demo'), findsOneWidget);
    expect(find.text('Ran Script'), findsOneWidget);
    expect(find.textContaining('DEMO_OK'), findsOneWidget);
    expect(find.text('2/2'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Live execution feed bounds rows and shows unknown or offline', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(900, 1400));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final events = List<Map<String, dynamic>>.generate(
      14,
      (index) => {
        'response_id': 'response-widget',
        'response_status': 'running',
        'seq': index == 13 ? 15 : index,
        'kind': 'other',
        'phase': 'completed',
        'ok': true,
        'summary_preview': {
          'state': 'available',
          'text': 'bounded event $index',
          'chars': 16,
          'truncated': false,
          'redacted': false,
        },
        if (index == 13) ...{
          'kind': 'file_change',
          'action': 'edit',
          'path': 'harness.dart',
          'lines_added': 4,
          'content_preview': {
            'state': 'available',
            'text': 'token=private-value candidate preview',
            'chars': 37,
            'truncated': false,
            'redacted': true,
          },
        },
      },
    );
    final feed = ExecutionFeed.fromJson({
      'known': true,
      'schema_version': 1,
      'runtime_id': 'runtime-test',
      'active_responses': 1,
      'truncated': true,
      'redaction_applied': true,
      'oldest_seq': 0,
      'next_seq': 16,
      'dropped_events': 2,
      'sequence_gap': 1,
      'limits': {'events': 20, 'preview_chars': 1000},
      'error': '',
      'bytes': 2048,
      'events': events,
    });
    final unknown = ExecutionFeed.fromJson({
      'known': true,
      'schema_version': 1,
      'limits': {'events': 20, 'preview_chars': 1000},
      'events': const [],
    });
    final noDetails = ExecutionFeed.fromJson({
      'known': true,
      'schema_version': 1,
      'limits': {'events': 20, 'preview_chars': 1000},
      'events': [
        {
          'response_id': 'r-disabled',
          'response_status': 'running',
          'seq': 99,
          'kind': 'model_call',
          'phase': 'completed',
          'model': 'sonder:latest',
          'response_preview': {'state': 'disabled'},
        },
      ],
    });
    final errored = ExecutionFeed.fromJson({
      'known': true,
      'schema_version': 1,
      'limits': {'events': 20, 'preview_chars': 1000},
      'error': 'feed projection unavailable',
      'events': const [],
    });

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData.dark(useMaterial3: true),
        home: Scaffold(
          body: SingleChildScrollView(
            child: Column(
              children: [
                LiveExecutionFeed(feed: feed),
                const LiveExecutionFeed(feed: null),
                LiveExecutionFeed(feed: unknown),
                LiveExecutionFeed(feed: noDetails),
                LiveExecutionFeed(feed: errored),
                LiveExecutionFeed(feed: feed, offline: true),
              ],
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    final primaryFeed = find.byType(LiveExecutionFeed).first;
    Finder inPrimary(Finder matching) =>
        find.descendant(of: primaryFeed, matching: matching);
    expect(find.text('bounded event 0'), findsNothing);
    expect(find.text('bounded event 1'), findsNothing);
    // SelectableText owns both a visible Text and an internal EditableText;
    // assert the single public widget instead of counting implementation
    // descendants as duplicate feed rows.
    expect(
      inPrimary(find.widgetWithText(SelectableText, 'bounded event 2')),
      findsOneWidget,
    );
    expect(inPrimary(find.text('edit harness.dart')), findsOneWidget);
    expect(inPrimary(find.text('12/14 events')), findsOneWidget);
    expect(inPrimary(find.text('Sequence gap')), findsOneWidget);
    expect(inPrimary(find.text('window 0 → 16')), findsOneWidget);
    expect(inPrimary(find.text('2 dropped')), findsOneWidget);
    expect(inPrimary(find.text('History truncated')), findsOneWidget);
    expect(inPrimary(find.text('Redaction applied')), findsOneWidget);
    expect(inPrimary(find.textContaining('file edit')), findsOneWidget);
    expect(inPrimary(find.textContaining('lines +4 ~0 -0')), findsOneWidget);
    expect(inPrimary(find.textContaining('<redacted>')), findsOneWidget);
    expect(find.textContaining('private-value'), findsNothing);
    expect(find.byType(SelectableText), findsWidgets);
    expect(find.text('Unavailable'), findsNWidgets(2));
    expect(
      find.text('The runtime reported an execution feed error.'),
      findsOneWidget,
    );
    expect(find.text('Details disabled'), findsOneWidget);
    expect(find.text('Unknown'), findsOneWidget);
    expect(find.text('Offline'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('System keeps one return affordance and no stacked tooltip', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(900, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      MaterialApp(home: SystemScreen(settings: Settings(), liveUpdates: false)),
    );
    await tester.pumpAndSettle();

    expect(find.byTooltip('Back to chat'), findsOneWidget);
    expect(find.text('Chat'), findsOneWidget);
    // The action button's own hover tooltip used to float over the top-right
    // corner and collide with the window's Close tooltip. The visible "Chat"
    // label carries the affordance instead.
    expect(find.byTooltip('Return to main chat'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('System start control is idle until an action runs', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(900, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      MaterialApp(home: SystemScreen(settings: Settings(), liveUpdates: false)),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('start-server')),
      240,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.text('Start server'), findsOneWidget);
    expect(find.text('Starting server...'), findsNothing);
    expect(find.byKey(const Key('runtime-busy')), findsNothing);
    expect(find.byKey(const Key('runtime-failure')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Permission mode chip is visible at the composer and switches', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    SharedPreferences.setMockInitialValues(<String, Object>{});

    var mode = 'manual';
    final posted = <String>[];
    final client = MockClient((request) async {
      if (request.url.path != '/v1/permission-mode') {
        return http.Response('{}', 503);
      }
      if (request.method == 'POST') {
        mode = (jsonDecode(request.body) as Map)['mode'].toString();
        posted.add(mode);
      }
      return http.Response(jsonEncode(_permissionModeBody(mode)), 200);
    });

    await http.runWithClient(() async {
      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();

      // Persistent, not buried in a menu: it is on screen next to the
      // composer before anything is typed.
      final chip = find.byKey(const Key('permission-mode-chip'));
      expect(chip, findsOneWidget);
      expect(
        find.descendant(of: chip, matching: find.text('manual')),
        findsOneWidget,
      );

      await tester.tap(chip);
      await tester.pumpAndSettle();

      // The picker lists every mode the server published, with the blurb
      // that says where each one's boundary is.
      expect(find.byKey(const Key('permission-mode-picker')), findsOneWidget);
      for (final name in ['plan', 'manual', 'acceptEdits', 'auto']) {
        expect(find.byKey(Key('permission-mode-option-$name')), findsOneWidget);
      }
      expect(find.text('reads only - no writes, no commands'), findsOneWidget);
      expect(
        find.textContaining('Elevation is a separate switch'),
        findsOneWidget,
      );

      await tester.tap(find.byKey(const Key('permission-mode-option-plan')));
      await tester.pumpAndSettle();

      // Selecting switches through the API and shows what the server
      // reported back, not what was optimistically requested.
      expect(posted, ['plan']);
      expect(find.byKey(const Key('permission-mode-picker')), findsNothing);
      expect(
        find.descendant(of: chip, matching: find.text('plan')),
        findsOneWidget,
      );
      expect(tester.takeException(), isNull);
    }, () => client);
  });

  testWidgets('Elevation renders as its own badge, never as the mode label', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    SharedPreferences.setMockInitialValues(<String, Object>{});

    final client = MockClient((request) async {
      if (request.url.path != '/v1/permission-mode') {
        return http.Response('{}', 503);
      }
      return http.Response(
        jsonEncode(
          _permissionModeBody(
            'auto',
            elevated: true,
            elevationReason: 'installing a driver',
          ),
        ),
        200,
      );
    });

    await http.runWithClient(() async {
      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();

      final chip = find.byKey(const Key('permission-mode-chip'));
      final badge = find.byKey(const Key('permission-elevated-badge'));
      expect(chip, findsOneWidget);
      expect(badge, findsOneWidget);

      // Two axes, two widgets: the badge is outside the mode chip, and the
      // chip's label is the mode alone -- no "auto +admin" hybrid.
      expect(find.descendant(of: chip, matching: badge), findsNothing);
      expect(
        find.descendant(of: badge, matching: find.text('ADMIN')),
        findsOneWidget,
      );
      expect(tester.getSemantics(chip).label, contains('auto'));
      expect(
        tester.getSemantics(badge).label,
        contains('Elevated privileges: on'),
      );
      final labels = tester
          .widgetList<Text>(
            find.descendant(of: chip, matching: find.byType(Text)),
          )
          .map((t) => t.data)
          .toList();
      expect(labels, ['auto']);
      expect(tester.takeException(), isNull);
    }, () => client);
  });

  testWidgets('Chip stays hidden on a server without the mode route', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    SharedPreferences.setMockInitialValues(<String, Object>{});

    // An older server has no such route at all: no indicator, not an error.
    final missing = MockClient((request) async => http.Response('', 404));
    await http.runWithClient(() async {
      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('permission-mode-chip')), findsNothing);
      expect(find.byKey(const Key('permission-elevated-badge')), findsNothing);
      // The composer still works; only the indicator is absent.
      expect(find.byType(TextField), findsOneWidget);
      expect(tester.takeException(), isNull);
    }, () => missing);
  });

  testWidgets(
      'A mode that stops being readable disappears instead of going '
      'stale', (tester) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    SharedPreferences.setMockInitialValues(<String, Object>{});

    var reachable = true;
    final flaky = MockClient((request) async {
      if (request.url.path != '/v1/permission-mode') {
        return http.Response('{}', 503);
      }
      if (!reachable) return http.Response('nope', 503);
      return http.Response(jsonEncode(_permissionModeBody('auto')), 200);
    });

    await http.runWithClient(() async {
      await tester.pumpWidget(const SonderRuntimeApp(manageLocalServer: false));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('permission-mode-chip')), findsOneWidget);

      // The next drift check fails. A stale mode shown as current is worse
      // than showing nothing, so the chip goes rather than freezes.
      reachable = false;
      await tester.pump(const Duration(seconds: 16));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('permission-mode-chip')), findsNothing);
      expect(find.text('auto'), findsNothing);
      expect(tester.takeException(), isNull);
    }, () => flaky);
  });
}

/// The `/v1/permission-mode` record for [mode], shaped like the server's.
Map<String, dynamic> _permissionModeBody(
  String mode, {
  bool elevated = false,
  String elevationReason = '',
}) {
  const blurbs = <String, String>{
    'plan': 'reads only - no writes, no commands',
    'manual': 'ask before anything that is not a read',
    'acceptEdits': 'file changes proceed; running programs still asks',
    'auto':
        'file changes and programs proceed; destructive still asks at the console',
  };
  const labels = <String, String>{
    'plan': 'plan',
    'manual': 'manual',
    'acceptEdits': 'accept edits',
    'auto': 'auto',
  };
  return {
    'mode': mode,
    'label': labels[mode] ?? mode,
    'blurb': blurbs[mode] ?? '',
    'elevated': elevated,
    'elevationReason': elevationReason,
    'modes': [
      for (final entry in blurbs.entries)
        {'name': entry.key, 'label': labels[entry.key], 'blurb': entry.value},
    ],
    'matrix': const {
      'safe': 'allow',
      'ask': 'ask',
      'mutation': 'ask',
      'execution': 'ask',
      'dangerous': 'ask',
    },
  };
}
