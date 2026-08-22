import 'package:flutter/material.dart';

import 'chat_screen.dart';
import 'local_manager.dart';
import 'settings.dart';
import 'theme.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const SonderRuntimeApp());
}

class SonderRuntimeApp extends StatefulWidget {
  final bool manageLocalServer;

  const SonderRuntimeApp({super.key, this.manageLocalServer = true});

  @override
  State<SonderRuntimeApp> createState() => _SonderRuntimeAppState();
}

class _SonderRuntimeAppState extends State<SonderRuntimeApp>
    with WidgetsBindingObserver {
  Settings? _settings;
  bool _startedLocalServer = false;
  bool _startingLocalServer = false;

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
        _startingLocalServer ||
        settings.hasHostLauncher ||
        !LocalManager.canRunLocalTools) {
      return;
    }
    _startingLocalServer = true;
    try {
      final result = await LocalManager.startServer(
        allowHosted: settings.allowHosted,
        contextSize: settings.contextSize,
        persistOnAppClose: settings.keepServerRunning,
      );
      _startedLocalServer = result.ok;
    } finally {
      _startingLocalServer = false;
    }
  }

  void _update(Settings s) {
    final previous = _settings;
    if (_startedLocalServer &&
        !(previous?.hasHostLauncher ?? false) &&
        s.hasHostLauncher &&
        !(previous?.keepServerRunning ?? false)) {
      LocalManager.stopManagedServerNow();
      _startedLocalServer = false;
    }
    setState(() => _settings = s);
    _autoStartServer(s);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (widget.manageLocalServer &&
        _startedLocalServer &&
        state == AppLifecycleState.detached &&
        !(_settings?.hasHostLauncher ?? false) &&
        !(_settings?.keepServerRunning ?? false)) {
      LocalManager.stopManagedServerNow();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    if (widget.manageLocalServer &&
        _startedLocalServer &&
        !(_settings?.hasHostLauncher ?? false) &&
        !(_settings?.keepServerRunning ?? false)) {
      LocalManager.stopManagedServerNow();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final settings = _settings;

    return MaterialApp(
      title: 'Sonder Runtime',
      debugShowCheckedModeBanner: false,
      theme: SonderTheme.light,
      darkTheme: SonderTheme.dark,
      themeMode: (settings?.darkMode ?? true)
          ? ThemeMode.dark
          : ThemeMode.light,
      home: settings == null
          ? const Scaffold(body: Center(child: CircularProgressIndicator()))
          : ChatScreen(settings: settings, onSettingsChanged: _update),
    );
  }
}
