import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat_screen.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/theme.dart';

/// A turn the test drives by hand.
class FakeTurn implements ChatTurn {
  final TurnRequest request;
  final StreamController<TurnEvent> controller = StreamController<TurnEvent>();
  int cancels = 0;

  FakeTurn(this.request);

  @override
  Stream<TurnEvent> get events => controller.stream;

  @override
  void cancel() {
    cancels++;
    if (!controller.isClosed) controller.close();
  }

  void phase(String p) => controller.add(TurnPhase(p));
  void delta(String text) => controller.add(TurnDelta(text));

  void done(String text, {ChatResponseMetadata? metadata}) {
    controller.add(TurnDone(ChatReply(text: text, metadata: metadata)));
    controller.close();
  }

  void fail(Object error) {
    controller.addError(error);
    controller.close();
  }
}

/// A scriptable [ChatBackend] that records every call.
class FakeChatBackend implements ChatBackend {
  @override
  String serverUrl;

  FakeChatBackend({this.serverUrl = 'http://127.0.0.1:11435'});

  final List<FakeTurn> turns = [];
  final List<String> feedback = [];
  final List<String> modePosts = [];
  final List<String> workRunGets = [];
  final List<String> workRunCancels = [];
  final List<(String, Duration)> approvals = [];
  int statusCalls = 0;
  int activeStatusCalls = 0;
  int maxConcurrentStatus = 0;

  /// Replies to the next turn immediately when set.
  String? autoReply;
  ChatResponseMetadata? autoMetadata;

  Object? statusError;
  Duration statusDelay = Duration.zero;
  SystemInfo statusInfo = SystemInfo.fromJson(const {});
  PermissionMode? mode;
  Object? modeReadError;
  Object? modeWriteError;
  List<String> models = const ['sonder'];

  WorkRunInfo Function(String id)? workRun;
  WorkRunInfo Function(String id)? cancelWorkRunResult;
  ApprovalOutcome approvalOutcome =
      const ApprovalOutcome(ApprovalStatus.approved, nonce: 'n_c41a');

  FakeTurn get lastTurn => turns.last;

  @override
  ChatTurn startTurn(TurnRequest request) {
    final turn = FakeTurn(request);
    turns.add(turn);
    final reply = autoReply;
    if (reply != null) {
      scheduleMicrotask(() => turn.done(reply, metadata: autoMetadata));
    }
    return turn;
  }

  @override
  Future<void> recordFeedback(String command, TurnRequest context) async {
    feedback.add(command);
  }

  @override
  Future<SystemInfo> systemInfo() async {
    statusCalls++;
    activeStatusCalls++;
    if (activeStatusCalls > maxConcurrentStatus) {
      maxConcurrentStatus = activeStatusCalls;
    }
    try {
      if (statusDelay > Duration.zero) await Future.delayed(statusDelay);
      final err = statusError;
      if (err != null) throw err;
      return statusInfo;
    } finally {
      activeStatusCalls--;
    }
  }

  @override
  Future<List<String>> listModels() async => models;

  @override
  Future<CommandCatalog> fetchCommands() async =>
      throw SonderException('offline');

  @override
  Future<PermissionMode?> fetchPermissionMode() async {
    final err = modeReadError;
    if (err != null) throw err;
    return mode;
  }

  @override
  Future<PermissionMode> setPermissionMode(String next) async {
    modePosts.add(next);
    final err = modeWriteError;
    if (err != null) throw err;
    mode = permissionModeFor(next);
    return mode!;
  }

  @override
  Future<WorkRunInfo> getWorkRun(String id) async {
    workRunGets.add(id);
    return (workRun ?? (i) => WorkRunInfo(id: i, status: 'running'))(id);
  }

  @override
  Future<WorkRunInfo> cancelWorkRun(String id) async {
    workRunCancels.add(id);
    return (cancelWorkRunResult ??
        (i) => WorkRunInfo(id: i, status: 'running', cancelRequested: true))(id);
  }

  @override
  Future<List<WorkRunInfo>> listWorkRuns() async => const [];

  @override
  Future<ApprovalOutcome> approveCall(String callId,
      {Duration ttl = const Duration(minutes: 15)}) async {
    approvals.add((callId, ttl));
    return approvalOutcome;
  }

  @override
  void dispose() {}
}

/// A server-shaped permission mode record.
PermissionMode permissionModeFor(String mode, {bool elevated = false}) {
  const blurbs = <String, String>{
    'plan': 'reads only - no writes, no commands',
    'manual': 'ask before anything that is not a read',
    'acceptEdits': 'file changes proceed; running programs still asks',
    'auto': 'file changes and programs proceed',
  };
  return PermissionMode(
    mode: mode,
    label: mode,
    blurb: blurbs[mode] ?? '',
    elevated: elevated,
    modes: [
      for (final e in blurbs.entries)
        PermissionModeOption(name: e.key, label: e.key, blurb: e.value),
    ],
  );
}

/// Pump a [ChatScreen] on [backend] at [size], dark theme by default.
Future<Settings> pumpChat(
  dynamic tester,
  FakeChatBackend backend, {
  Size size = const Size(1200, 900),
  ThemeMode themeMode = ThemeMode.dark,
  Map<String, Object> prefs = const <String, Object>{},
}) async {
  SharedPreferences.setMockInitialValues(prefs);
  ChatStore.backend = PrefsChatStoreBackend();
  ChatStore.resetCache();
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1.0;
  final settings = await Settings.load();
  await tester.pumpWidget(MaterialApp(
    theme: SonderTheme.light,
    darkTheme: SonderTheme.dark,
    themeMode: themeMode,
    home: ChatScreen(
      settings: settings,
      onSettingsChanged: (_) {},
      backendFactory: (_) => backend,
    ),
  ));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 50));
  return settings;
}

/// Unmount the screen so its timers stop before the test ends.
Future<void> unmountChat(dynamic tester) async {
  await tester.pumpWidget(const SizedBox());
  tester.view.resetPhysicalSize();
  tester.view.resetDevicePixelRatio();
}

/// Everything the chat store holds, as one string, for leak checks.
Future<String> storedChatText() async {
  final prefs = await SharedPreferences.getInstance();
  return prefs
      .getKeys()
      .map((k) => '$k=${prefs.get(k)}')
      .join('\n');
}

/// Load the bundled IBM Plex faces and Material Icons so text measures and
/// renders as in the app (tests otherwise use the square test font).
Future<void> loadAppFonts() async {
  Future<void> load(String family, List<String> paths) async {
    final loader = FontLoader(family);
    for (final path in paths) {
      final bytes = File(path).readAsBytesSync();
      loader.addFont(Future.value(ByteData.sublistView(bytes)));
    }
    await loader.load();
  }

  await load('IBM Plex Sans', [
    'fonts/IBMPlexSans-Regular.ttf',
    'fonts/IBMPlexSans-Medium.ttf',
    'fonts/IBMPlexSans-SemiBold.ttf',
  ]);
  await load('IBM Plex Mono', [
    'fonts/IBMPlexMono-Regular.ttf',
    'fonts/IBMPlexMono-Medium.ttf',
    'fonts/IBMPlexMono-SemiBold.ttf',
  ]);
  final flutterRoot = Platform.environment['FLUTTER_ROOT'] ?? '/opt/flutter';
  final icons = File(
      '$flutterRoot/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf');
  if (icons.existsSync()) await load('MaterialIcons', [icons.path]);
}
