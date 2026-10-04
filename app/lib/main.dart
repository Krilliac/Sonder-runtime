import 'package:flutter/material.dart';

import 'local_manager.dart';
import 'settings.dart';
import 'shell/app_shell.dart';
import 'shell/preferences.dart';
import 'shell/splash.dart';
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
  bool _sidebarCollapsed = false;
  bool _startedLocalServer = false;
  bool _startingLocalServer = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _load();
  }

  Future<void> _load() async {
    final collapsed = ShellPreferences.sidebarCollapsed();
    final settings = await Settings.load();
    _sidebarCollapsed = await collapsed;
    if (!mounted) return;
    setState(() => _settings = settings);
    _autoStartServer(settings);
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
      themeMode: switch (settings?.themeMode) {
        'light' => ThemeMode.light,
        'system' => ThemeMode.system,
        _ => ThemeMode.dark,
      },
      home: _Boot(
        child: settings == null
            ? const ShellSplash(key: ValueKey('splash'))
            : AppShell(
                key: const ValueKey('shell'),
                settings: settings,
                onSettingsChanged: _update,
                initialSidebarCollapsed: _sidebarCollapsed,
              ),
      ),
    );
  }
}

/// Cross-fades from the splash to the app once settings are read.
class _Boot extends StatelessWidget {
  final Widget child;
  const _Boot({required this.child});

  @override
  Widget build(BuildContext context) => AnimatedSwitcher(
        duration: SonderMotion.of(context, SonderMotion.slow),
        switchInCurve: SonderMotion.enter,
        switchOutCurve: SonderMotion.exit,
        child: child,
      );
}
