import 'package:flutter/material.dart';

/// The visual language shared by the chat, system, and settings surfaces:
/// "quiet instrument".
///
/// The transcript is the object; chrome is hairlines, one accent and a glyph
/// gutter. Keeping every token in one place makes a restyle safe: screens
/// consume [SonderTokens] and Material roles instead of inventing slightly
/// different panel colours and radii. The local-first teal remains the
/// product signal; the neutral surfaces keep long model responses easy to
/// scan. Dark is the primary theme and light mirrors it token for token.
abstract final class SonderTheme {
  static const signal = Color(0xFF63D6C8);
  static const darkCanvas = Color(0xFF0B1117);
  static const lightCanvas = Color(0xFFF4F7F8);
  static const darkPanel = Color(0xFF0F171E);
  static const darkBorder = Color(0xFF1F2C36);
  static const lightBorder = Color(0xFFDCE5E8);

  /// The UI face. Bundled under `fonts/` (OFL); every text style names it so
  /// no surface falls back to the platform default by accident.
  static const sans = 'IBM Plex Sans';

  /// The transcript, code, status and terminal face. Tabular figures are
  /// applied where numbers line up in columns.
  static const mono = 'IBM Plex Mono';

  /// The bundled symbol subset (`assets/fonts/SonderSymbols.ttf`) holding the
  /// status-vocabulary glyphs Plex lacks: ❯ ◈ ⊘ ✗ ▸ ● and friends.
  static const symbols = 'SonderSymbols';

  /// The fallback chain every Sans and Mono style carries, so a glyph such
  /// as ◈ or ⊘ renders from a bundled face instead of tofu or a network font.
  static const fontFallback = <String>[symbols];

  static ThemeData get dark => _build(Brightness.dark);
  static ThemeData get light => _build(Brightness.light);

  static ThemeData _build(Brightness brightness) {
    final dark = brightness == Brightness.dark;
    final tokens = dark ? SonderTokens.dark : SonderTokens.light;
    final seeded = ColorScheme.fromSeed(
      seedColor: signal,
      brightness: brightness,
    );
    // The seeded scheme supplies the roles the tokens do not name; the
    // tokens win everywhere a screen actually paints.
    final scheme = seeded.copyWith(
      primary: tokens.accent,
      onPrimary: tokens.onAccent,
      primaryContainer: tokens.accentDim,
      onPrimaryContainer: tokens.text,
      surface: tokens.canvas,
      onSurface: tokens.text,
      onSurfaceVariant: tokens.text2,
      surfaceContainerLowest: tokens.canvas,
      surfaceContainerLow: tokens.panel,
      surfaceContainer: tokens.panel,
      surfaceContainerHigh: tokens.raised,
      surfaceContainerHighest: tokens.raised,
      outline: tokens.muted,
      outlineVariant: tokens.hairline,
      error: tokens.danger,
      onError: tokens.onAccent,
      errorContainer: tokens.dangerDim,
      onErrorContainer: tokens.text,
      shadow: Colors.transparent,
    );
    final text = _textTheme(tokens);

    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      fontFamily: sans,
      fontFamilyFallback: fontFallback,
      textTheme: text,
      scaffoldBackgroundColor: tokens.canvas,
      canvasColor: tokens.canvas,
      splashFactory: InkSparkle.splashFactory,
      visualDensity: VisualDensity.standard,
      extensions: <ThemeExtension<dynamic>>[tokens],
      pageTransitionsTheme: const PageTransitionsTheme(builders: {
        TargetPlatform.android: SonderPageTransitionsBuilder(),
        TargetPlatform.iOS: SonderPageTransitionsBuilder(),
        TargetPlatform.macOS: SonderPageTransitionsBuilder(),
        TargetPlatform.windows: SonderPageTransitionsBuilder(),
        TargetPlatform.linux: SonderPageTransitionsBuilder(),
        TargetPlatform.fuchsia: SonderPageTransitionsBuilder(),
      }),
      hoverColor: tokens.raised.withValues(alpha: dark ? 0.7 : 0.9),
      focusColor: tokens.accentDim,
      segmentedButtonTheme: SegmentedButtonThemeData(
        style: SegmentedButton.styleFrom(
          foregroundColor: tokens.text2,
          selectedForegroundColor: tokens.text,
          selectedBackgroundColor: tokens.accentDim,
          side: BorderSide(color: tokens.hairlineStrong),
          textStyle: text.labelLarge,
          minimumSize: const Size(0, 36),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(SonderRadius.row),
          ),
        ),
      ),
      sliderTheme: SliderThemeData(
        activeTrackColor: tokens.accent,
        inactiveTrackColor: tokens.hairlineStrong,
        thumbColor: tokens.accent,
        overlayColor: tokens.accentDim,
        valueIndicatorColor: tokens.raised,
        valueIndicatorTextStyle: text.labelLarge?.copyWith(color: tokens.text),
        trackHeight: 4,
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: tokens.canvas,
        foregroundColor: tokens.text,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        toolbarHeight: 48,
        shape: Border(bottom: BorderSide(color: tokens.hairline)),
        titleTextStyle: text.titleMedium,
        iconTheme: IconThemeData(color: tokens.text2, size: 20),
        actionsIconTheme: IconThemeData(color: tokens.text2, size: 20),
      ),
      cardTheme: CardThemeData(
        elevation: 0,
        margin: EdgeInsets.zero,
        color: tokens.panel,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.sheet),
          side: BorderSide(color: tokens.hairline),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: tokens.panel,
        hintStyle: text.bodyMedium?.copyWith(color: tokens.muted),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 12,
          vertical: 10,
        ),
        // Field boundaries are control boundaries: hairlineStrong keeps them
        // at 3:1 or better against both the canvas and the fill (WCAG 1.4.11).
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
          borderSide: BorderSide(color: tokens.hairlineStrong),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
          borderSide: BorderSide(color: tokens.hairlineStrong),
        ),
        disabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
          borderSide: BorderSide(color: tokens.hairline),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
          borderSide: BorderSide(color: tokens.accentText, width: 1.5),
        ),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: Colors.transparent,
        selectedColor: tokens.accentDim,
        labelStyle: text.labelLarge?.copyWith(color: tokens.text),
        side: BorderSide(color: tokens.hairlineStrong),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.pill),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      ),
      listTileTheme: ListTileThemeData(
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
        ),
        selectedTileColor: tokens.raised,
        selectedColor: tokens.text,
        iconColor: tokens.text2,
        textColor: tokens.text,
        contentPadding: const EdgeInsets.symmetric(horizontal: 12),
        minVerticalPadding: 8,
      ),
      navigationDrawerTheme: NavigationDrawerThemeData(
        backgroundColor: tokens.panel,
        surfaceTintColor: Colors.transparent,
        indicatorColor: tokens.raised,
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: tokens.panel,
        indicatorColor: tokens.raised,
        selectedIconTheme: IconThemeData(color: tokens.accentText, size: 20),
        unselectedIconTheme: IconThemeData(color: tokens.text2, size: 20),
        selectedLabelTextStyle: text.labelMedium?.copyWith(color: tokens.text),
        unselectedLabelTextStyle:
            text.labelMedium?.copyWith(color: tokens.text2),
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: tokens.panel,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.sheet),
          side: BorderSide(color: tokens.hairline),
        ),
        titleTextStyle: text.titleMedium,
        contentTextStyle: text.bodyMedium,
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: tokens.panel,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
          side: BorderSide(color: tokens.hairline),
        ),
        textStyle: text.bodyMedium,
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: tokens.raised,
        contentTextStyle: text.bodyMedium?.copyWith(color: tokens.text),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
          side: BorderSide(color: tokens.hairlineStrong),
        ),
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: tokens.raised,
          borderRadius: BorderRadius.circular(SonderRadius.control),
          border: Border.all(color: tokens.hairlineStrong),
        ),
        textStyle: text.bodySmall?.copyWith(color: tokens.text),
        waitDuration: const Duration(milliseconds: 400),
      ),
      progressIndicatorTheme: ProgressIndicatorThemeData(
        color: tokens.accent,
        linearTrackColor: tokens.hairline,
        linearMinHeight: 4,
      ),
      dividerTheme: DividerThemeData(color: tokens.hairline, space: 1),
      switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith(
          (states) => states.contains(WidgetState.selected)
              ? tokens.onAccent
              : tokens.text2,
        ),
        trackColor: WidgetStateProperty.resolveWith(
          (states) => states.contains(WidgetState.selected)
              ? tokens.accent
              : tokens.hairlineStrong,
        ),
        trackOutlineColor: const WidgetStatePropertyAll(Colors.transparent),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: tokens.accent,
          foregroundColor: tokens.onAccent,
          minimumSize: const Size(0, 36),
          padding: const EdgeInsets.symmetric(horizontal: 14),
          textStyle: text.labelLarge,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(SonderRadius.row),
          ),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: tokens.text,
          minimumSize: const Size(0, 36),
          padding: const EdgeInsets.symmetric(horizontal: 14),
          side: BorderSide(color: tokens.hairlineStrong),
          textStyle: text.labelLarge,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(SonderRadius.row),
          ),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: tokens.text2,
          minimumSize: const Size(0, 36),
          textStyle: text.labelLarge,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(SonderRadius.row),
          ),
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(
          foregroundColor: tokens.text2,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(SonderRadius.row),
          ),
        ),
      ),
      floatingActionButtonTheme: FloatingActionButtonThemeData(
        backgroundColor: tokens.accent,
        foregroundColor: tokens.onAccent,
        elevation: 0,
        focusElevation: 0,
        hoverElevation: 0,
        highlightElevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
        ),
      ),
      expansionTileTheme: ExpansionTileThemeData(
        iconColor: tokens.text2,
        collapsedIconColor: tokens.text2,
        textColor: tokens.text,
        collapsedTextColor: tokens.text2,
      ),
    );
  }

  static TextTheme _textTheme(SonderTokens tokens) {
    TextStyle style(double size, double height, FontWeight weight,
        {Color? color, double spacing = 0}) {
      return TextStyle(
        fontFamily: sans,
        fontFamilyFallback: fontFallback,
        fontSize: size,
        height: height / size,
        fontWeight: weight,
        letterSpacing: spacing,
        color: color ?? tokens.text,
      );
    }

    return TextTheme(
      headlineSmall: style(22, 28, FontWeight.w600, spacing: -0.2),
      titleLarge: style(20, 26, FontWeight.w600, spacing: -0.2),
      titleMedium: style(16, 24, FontWeight.w500),
      titleSmall: style(14, 20, FontWeight.w500),
      bodyLarge: style(16, 24, FontWeight.w400),
      bodyMedium: style(14, 22, FontWeight.w400),
      bodySmall: style(12, 18, FontWeight.w400, color: tokens.text2),
      labelLarge: style(13, 18, FontWeight.w500),
      labelMedium: style(12, 16, FontWeight.w500, color: tokens.text2),
      labelSmall:
          style(11, 16, FontWeight.w600, color: tokens.muted, spacing: 0.88),
    );
  }
}

/// The corner radii, by role: controls 4, rows and code 8, sheets and cards
/// 12, pills.
abstract final class SonderRadius {
  static const control = 4.0;
  static const row = 8.0;
  static const sheet = 12.0;
  static const card = 12.0;
  static const pill = 999.0;
}

/// The spacing scale: a 4-point grid. Layout gaps and padding come from here
/// rather than one-off literals (7, 9, 11...), so rhythm stays consistent.
abstract final class SonderSpace {
  static const xxs = 2.0;
  static const xs = 4.0;
  static const sm = 8.0;
  static const md = 12.0;
  static const lg = 16.0;
  static const xl = 20.0;
  static const xxl = 24.0;
  static const x3 = 32.0;
  static const x4 = 40.0;
}

/// Motion: short, purposeful, and never decorative. Entrances decelerate,
/// exits accelerate, state changes use the standard curve (Material 3 easing).
///
/// Durations pass through [SonderMotion.of], which returns zero when the
/// platform asks for reduced motion. Only decoration is removed: progress,
/// timers and live status keep updating either way.
abstract final class SonderMotion {
  /// Hover, press, small state flips.
  static const fast = Duration(milliseconds: 150);

  /// Panels, category switches, expanding sections.
  static const medium = Duration(milliseconds: 220);

  /// Page transitions and larger layout moves.
  static const slow = Duration(milliseconds: 320);

  /// Material 3 emphasized decelerate: things arriving.
  static const enter = Cubic(0.05, 0.7, 0.1, 1.0);

  /// Material 3 emphasized accelerate: things leaving.
  static const exit = Cubic(0.3, 0.0, 0.8, 0.15);

  /// Material 3 standard: things changing in place.
  static const standard = Cubic(0.2, 0.0, 0.0, 1.0);

  /// [duration], or zero when the platform requests reduced motion.
  static Duration of(BuildContext context, Duration duration) =>
      MediaQuery.maybeDisableAnimationsOf(context) == true
          ? Duration.zero
          : duration;
}

/// Route transition shared by every pushed page: a short fade-through with a
/// slight scale, so moving between surfaces feels continuous instead of a
/// hard cut (Settings used to have none) or a full-screen slide.
class SonderPageTransitionsBuilder extends PageTransitionsBuilder {
  const SonderPageTransitionsBuilder();

  @override
  Duration get transitionDuration => SonderMotion.slow;

  @override
  Duration get reverseTransitionDuration => SonderMotion.medium;

  @override
  Widget buildTransitions<T>(
    PageRoute<T> route,
    BuildContext context,
    Animation<double> animation,
    Animation<double> secondaryAnimation,
    Widget child,
  ) {
    if (MediaQuery.maybeDisableAnimationsOf(context) == true) return child;
    final enter = CurvedAnimation(
      parent: animation,
      curve: SonderMotion.enter,
      reverseCurve: SonderMotion.exit,
    );
    // The page underneath fades back a little while the new one arrives.
    final under = CurvedAnimation(
      parent: secondaryAnimation,
      curve: SonderMotion.standard,
    );
    return FadeTransition(
      opacity: Tween<double>(begin: 1, end: 0.6).animate(under),
      child: FadeTransition(
        opacity: enter,
        child: ScaleTransition(
          scale: Tween<double>(begin: 0.985, end: 1).animate(enter),
          child: child,
        ),
      ),
    );
  }
}

/// The colour tokens of one theme. Read them with `SonderTokens.of(context)`.
///
/// Semantic tones carry meaning together with a label or a glyph, never
/// alone; [SonderTokens.auto] is the one mode colour that is not also a
/// risk tone, which is why it has its own name.
class SonderTokens extends ThemeExtension<SonderTokens> {
  final Color canvas;
  final Color panel;
  final Color raised;
  final Color hairline;
  final Color hairlineStrong;
  final Color text;
  final Color text2;
  final Color muted;
  final Color accent;

  /// The accent as text or a thin glyph: 4.5:1 on every surface. [accent]
  /// stays the fill, focus and icon colour (3:1 is enough there).
  final Color accentText;
  final Color onAccent;
  final Color accentDim;
  final Color ok;
  final Color info;
  final Color warn;
  final Color danger;
  final Color dangerDim;
  final Color auto;
  final Color mutation;
  final Color execution;

  const SonderTokens({
    required this.canvas,
    required this.panel,
    required this.raised,
    required this.hairline,
    required this.hairlineStrong,
    required this.text,
    required this.text2,
    required this.muted,
    required this.accent,
    required this.accentText,
    required this.onAccent,
    required this.accentDim,
    required this.ok,
    required this.info,
    required this.warn,
    required this.danger,
    required this.dangerDim,
    required this.auto,
    required this.mutation,
    required this.execution,
  });

  static const dark = SonderTokens(
    canvas: Color(0xFF0B1117),
    panel: Color(0xFF0F171E),
    raised: Color(0xFF141F28),
    hairline: Color(0xFF1F2C36),
    hairlineStrong: Color(0xFF5A6C7E),
    text: Color(0xFFE7EDF2),
    text2: Color(0xFFA5B2BD),
    muted: Color(0xFF7F8E9A),
    accent: Color(0xFF63D6C8),
    accentText: Color(0xFF63D6C8),
    onAccent: Color(0xFF062A27),
    accentDim: Color(0x2463D6C8),
    ok: Color(0xFF79D394),
    info: Color(0xFF7FB8F0),
    warn: Color(0xFFF0C36A),
    danger: Color(0xFFF27B7B),
    dangerDim: Color(0x24F27B7B),
    auto: Color(0xFFD89CF6),
    mutation: Color(0xFFF0A070),
    execution: Color(0xFFC9A58E),
  );

  static const light = SonderTokens(
    canvas: Color(0xFFF4F7F8),
    panel: Color(0xFFFFFFFF),
    raised: Color(0xFFEEF3F4),
    hairline: Color(0xFFDCE5E8),
    hairlineStrong: Color(0xFF7A8D98),
    text: Color(0xFF0F1A21),
    text2: Color(0xFF42525C),
    muted: Color(0xFF5F6F7A),
    accent: Color(0xFF1FA597),
    accentText: Color(0xFF0A7B73),
    onAccent: Color(0xFF062A27),
    accentDim: Color(0x1F1FA597),
    ok: Color(0xFF1F7A45),
    info: Color(0xFF2F6FB3),
    warn: Color(0xFF946000),
    danger: Color(0xFFC23636),
    dangerDim: Color(0x1FC23636),
    auto: Color(0xFF7A4BB5),
    mutation: Color(0xFFA4501F),
    execution: Color(0xFF7A5A46),
  );

  /// The tokens of the ambient theme; the dark set when a caller sits
  /// outside a [Theme] (tests that pump a bare widget).
  static SonderTokens of(BuildContext context) {
    return Theme.of(context).extension<SonderTokens>() ??
        (Theme.of(context).brightness == Brightness.dark ? dark : light);
  }

  /// A monospace style for figures, paths and code, with tabular numerals.
  TextStyle mono(
    double size, {
    Color? color,
    FontWeight weight = FontWeight.w400,
    double? height,
  }) {
    return TextStyle(
      fontFamily: SonderTheme.mono,
      fontFamilyFallback: SonderTheme.fontFallback,
      fontSize: size,
      height: (height ?? size + 6) / size,
      fontWeight: weight,
      color: color ?? text,
      fontFeatures: const [FontFeature.tabularFigures()],
    );
  }

  @override
  SonderTokens copyWith({
    Color? canvas,
    Color? panel,
    Color? raised,
    Color? hairline,
    Color? hairlineStrong,
    Color? text,
    Color? text2,
    Color? muted,
    Color? accent,
    Color? accentText,
    Color? onAccent,
    Color? accentDim,
    Color? ok,
    Color? info,
    Color? warn,
    Color? danger,
    Color? dangerDim,
    Color? auto,
    Color? mutation,
    Color? execution,
  }) {
    return SonderTokens(
      canvas: canvas ?? this.canvas,
      panel: panel ?? this.panel,
      raised: raised ?? this.raised,
      hairline: hairline ?? this.hairline,
      hairlineStrong: hairlineStrong ?? this.hairlineStrong,
      text: text ?? this.text,
      text2: text2 ?? this.text2,
      muted: muted ?? this.muted,
      accent: accent ?? this.accent,
      accentText: accentText ?? this.accentText,
      onAccent: onAccent ?? this.onAccent,
      accentDim: accentDim ?? this.accentDim,
      ok: ok ?? this.ok,
      info: info ?? this.info,
      warn: warn ?? this.warn,
      danger: danger ?? this.danger,
      dangerDim: dangerDim ?? this.dangerDim,
      auto: auto ?? this.auto,
      mutation: mutation ?? this.mutation,
      execution: execution ?? this.execution,
    );
  }

  @override
  SonderTokens lerp(ThemeExtension<SonderTokens>? other, double t) {
    if (other is! SonderTokens) return this;
    Color mix(Color a, Color b) => Color.lerp(a, b, t) ?? a;
    return SonderTokens(
      canvas: mix(canvas, other.canvas),
      panel: mix(panel, other.panel),
      raised: mix(raised, other.raised),
      hairline: mix(hairline, other.hairline),
      hairlineStrong: mix(hairlineStrong, other.hairlineStrong),
      text: mix(text, other.text),
      text2: mix(text2, other.text2),
      muted: mix(muted, other.muted),
      accent: mix(accent, other.accent),
      accentText: mix(accentText, other.accentText),
      onAccent: mix(onAccent, other.onAccent),
      accentDim: mix(accentDim, other.accentDim),
      ok: mix(ok, other.ok),
      info: mix(info, other.info),
      warn: mix(warn, other.warn),
      danger: mix(danger, other.danger),
      dangerDim: mix(dangerDim, other.dangerDim),
      auto: mix(auto, other.auto),
      mutation: mix(mutation, other.mutation),
      execution: mix(execution, other.execution),
    );
  }
}

/// WCAG 2.x contrast arithmetic, shared by the contrast tests and any
/// widget that must pick a legible foreground at run time.
abstract final class SonderContrast {
  /// Minimum ratio for body text and status words.
  static const text = 4.5;

  /// Minimum ratio for control boundaries, focus rings and meaningful icons.
  static const control = 3.0;

  /// The contrast ratio of two opaque colours, 1.0 to 21.0.
  static double ratio(Color a, Color b) {
    final la = a.computeLuminance();
    final lb = b.computeLuminance();
    final hi = la > lb ? la : lb;
    final lo = la > lb ? lb : la;
    return (hi + 0.05) / (lo + 0.05);
  }
}
