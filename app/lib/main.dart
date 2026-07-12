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
    const seed = Color(0xFF63D6C8); // trilobite signal teal

    ThemeData buildTheme(Brightness brightness) {
      final dark = brightness == Brightness.dark;
      final scheme = ColorScheme.fromSeed(
        seedColor: seed,
        brightness: brightness,
      );
      return ThemeData(
        useMaterial3: true,
        brightness: brightness,
        colorScheme: scheme,
        scaffoldBackgroundColor:
            dark ? const Color(0xFF0B1117) : const Color(0xFFF5F8F8),
        canvasColor: dark ? const Color(0xFF0B1117) : const Color(0xFFF5F8F8),
        appBarTheme: AppBarTheme(
          backgroundColor: dark ? const Color(0xFF0B1117) : scheme.surface,
          foregroundColor: scheme.onSurface,
          elevation: 0,
          scrolledUnderElevation: 0,
          titleTextStyle: TextStyle(
            color: scheme.onSurface,
            fontSize: 18,
            fontWeight: FontWeight.w700,
          ),
        ),
        cardTheme: CardThemeData(
          elevation: 0,
          margin: EdgeInsets.zero,
          color: dark ? const Color(0xFF121B23) : Colors.white,
          surfaceTintColor: Colors.transparent,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(18),
            side: BorderSide(
              color: dark ? const Color(0xFF24343D) : const Color(0xFFDDE7E7),
            ),
          ),
        ),
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: dark ? const Color(0xFF121B23) : Colors.white,
          contentPadding: const EdgeInsets.symmetric(
            horizontal: 16,
            vertical: 14,
          ),
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: BorderSide(color: scheme.outlineVariant),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: BorderSide(color: scheme.outlineVariant),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: BorderSide(color: scheme.primary, width: 1.5),
          ),
        ),
        chipTheme: ChipThemeData(
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(10),
            side: BorderSide(color: scheme.outlineVariant),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
        ),
        dividerTheme: DividerThemeData(
          color: scheme.outlineVariant.withValues(alpha: 0.65),
          space: 1,
        ),
      );
    }

    return MaterialApp(
      title: 'trilobite',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(Brightness.light),
      darkTheme: buildTheme(Brightness.dark),
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
