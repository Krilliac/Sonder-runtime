import 'package:flutter/material.dart';

import 'chat_screen.dart';
import 'settings.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const TrilobiteApp());
}

class TrilobiteApp extends StatefulWidget {
  const TrilobiteApp({super.key});

  @override
  State<TrilobiteApp> createState() => _TrilobiteAppState();
}

class _TrilobiteAppState extends State<TrilobiteApp> {
  Settings? _settings;

  @override
  void initState() {
    super.initState();
    Settings.load().then((s) => setState(() => _settings = s));
  }

  void _update(Settings s) => setState(() => _settings = s);

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
