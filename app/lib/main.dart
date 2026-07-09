import 'dart:async';

import 'package:flutter/material.dart';

import 'chat_screen.dart';
import 'local_manager.dart';
import 'settings.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const TrilobiteApp());
}

class TrilobiteApp extends StatefulWidget {
  final bool manageLocalServer;

  const TrilobiteApp({super.key, this.manageLocalServer = true});

  @override
  State<TrilobiteApp> createState() => _TrilobiteAppState();
}

class _TrilobiteAppState extends State<TrilobiteApp> with WidgetsBindingObserver {
  Settings? _settings;
  bool _startedLocalServer = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    Settings.load().then((s) {
      setState(() => _settings = s);
      _autoStartServer(s);
    });
  }

  Future<void> _autoStartServer(Settings settings) async {
    if (!widget.manageLocalServer ||
        _startedLocalServer ||
        !LocalManager.canRunLocalTools) {
      return;
    }
    _startedLocalServer = true;
    await LocalManager.startServer(
      allowHosted: settings.allowHosted,
      contextSize: settings.contextSize,
    );
  }

  void _update(Settings s) {
    setState(() => _settings = s);
    _autoStartServer(s);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (widget.manageLocalServer && state == AppLifecycleState.detached) {
      unawaited(LocalManager.stopServers());
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    if (widget.manageLocalServer) {
      unawaited(LocalManager.stopServers());
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final settings = _settings;
    const seed = Color(0xFF4F8A8B); // trilobite teal

    return MaterialApp(
      title: 'trilobite',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: seed),
      ),
      darkTheme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: seed,
          brightness: Brightness.dark,
        ),
      ),
      themeMode:
          (settings?.darkMode ?? true) ? ThemeMode.dark : ThemeMode.light,
      home: settings == null
          ? const Scaffold(
              body: Center(child: CircularProgressIndicator()),
            )
          : ChatScreen(
              settings: settings,
              onSettingsChanged: _update,
            ),
    );
  }
}
