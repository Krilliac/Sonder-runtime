import 'package:flutter/material.dart';

/// The visual language shared by the chat, system, and settings surfaces.
///
/// Keeping the palette in one place makes a redesign safe: screens consume
/// Material roles instead of inventing slightly different panel colours and
/// border radii.  The local-first teal remains the product signal, while the
/// neutral surfaces keep long model responses easy to scan.
abstract final class SonderTheme {
  static const signal = Color(0xFF63D6C8);
  static const darkCanvas = Color(0xFF0B1117);
  static const lightCanvas = Color(0xFFF5F8F8);
  static const darkPanel = Color(0xFF121B23);
  static const darkBorder = Color(0xFF24343D);
  static const lightBorder = Color(0xFFDDE7E7);
  static const mono = 'Consolas';

  static ThemeData get dark => _build(Brightness.dark);
  static ThemeData get light => _build(Brightness.light);

  static ThemeData _build(Brightness brightness) {
    final dark = brightness == Brightness.dark;
    final scheme = ColorScheme.fromSeed(
      seedColor: signal,
      brightness: brightness,
    );
    final border = dark ? darkBorder : lightBorder;
    final panel = dark ? darkPanel : Colors.white;

    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      scaffoldBackgroundColor: dark ? darkCanvas : lightCanvas,
      canvasColor: dark ? darkCanvas : lightCanvas,
      visualDensity: VisualDensity.standard,
      appBarTheme: AppBarTheme(
        backgroundColor: dark ? darkCanvas : scheme.surface,
        foregroundColor: scheme.onSurface,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          color: scheme.onSurface,
          fontSize: 18,
          fontWeight: FontWeight.w700,
        ),
      ),
      cardTheme: CardThemeData(
        elevation: 0,
        margin: EdgeInsets.zero,
        color: panel,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(18),
          side: BorderSide(color: border),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: panel,
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
      listTileTheme: ListTileThemeData(
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        selectedTileColor: scheme.primaryContainer.withValues(alpha: 0.55),
        contentPadding: const EdgeInsets.symmetric(horizontal: 12),
      ),
      navigationDrawerTheme: NavigationDrawerThemeData(
        backgroundColor: panel,
        surfaceTintColor: Colors.transparent,
        indicatorColor: scheme.primaryContainer,
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      ),
      progressIndicatorTheme: ProgressIndicatorThemeData(
        color: scheme.primary,
        linearTrackColor: scheme.surfaceContainerHighest,
      ),
      dividerTheme: DividerThemeData(
        color: scheme.outlineVariant.withValues(alpha: 0.65),
        space: 1,
      ),
    );
  }
}
