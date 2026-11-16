import 'package:flutter/material.dart';

/// The visual language shared by every surface: a familiar chat product on
/// warm-grey neutrals with one restrained accent.
///
/// The conversation is the object. Hierarchy comes from surface tone (a
/// sunken sidebar, a raised composer, a soft user bubble) rather than from
/// rules and boxes, and the accent, Iris, is drawn from the Sonder mark and
/// kept for the things a person acts on: send, links, focus and selection.
/// Keeping every token in one place makes a restyle safe: screens consume
/// [SonderTokens] and Material roles instead of inventing slightly different
/// panel colours and radii. Light and dark are both first-class and mirror
/// each other token for token.
abstract final class SonderTheme {
  /// Iris, the seed of the Material roles the tokens do not name.
  static const seed = Color(0xFF5B54C8);

  /// The UI face. Bundled under `fonts/` (OFL); every text style names it so
  /// no surface falls back to the platform default by accident.
  static const sans = 'IBM Plex Sans';

  /// The code, status and terminal face. Tabular figures are applied where
  /// numbers line up in columns.
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
      seedColor: seed,
      brightness: brightness,
    );
    // The seeded scheme supplies the roles the tokens do not name; the
    // tokens win everywhere a screen actually paints.
    final scheme = seeded.copyWith(
      primary: tokens.accent,
      onPrimary: tokens.onAccent,
      primaryContainer: tokens.accentDim,
      onPrimaryContainer: tokens.text,
      secondary: tokens.accent,
      onSecondary: tokens.onAccent,
      secondaryContainer: tokens.raised,
      onSecondaryContainer: tokens.text,
      surface: tokens.canvas,
      onSurface: tokens.text,
      onSurfaceVariant: tokens.text2,
      surfaceContainerLowest: tokens.sunken,
      surfaceContainerLow: tokens.panel,
      surfaceContainer: tokens.panel,
      surfaceContainerHigh: tokens.raised,
      surfaceContainerHighest: tokens.raised,
      surfaceTint: Colors.transparent,
      outline: tokens.muted,
      outlineVariant: tokens.hairline,
      error: tokens.danger,
      onError: tokens.onAccent,
      errorContainer: tokens.dangerDim,
      onErrorContainer: tokens.text,
      shadow: tokens.shadow,
      scrim: tokens.scrim,
    );
    final text = _textTheme(tokens);
    OutlineInputBorder field(Color color, {double width = 1}) =>
        OutlineInputBorder(
          borderRadius: BorderRadius.circular(SonderRadius.row),
          borderSide: BorderSide(color: color, width: width),
        );
    final control = RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(SonderRadius.row),
    );

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
      hoverColor: tokens.raised.withValues(alpha: 0.7),
      focusColor: tokens.accentDim,
      textSelectionTheme: TextSelectionThemeData(
        cursorColor: tokens.accentText,
        selectionColor: tokens.accentDim,
        selectionHandleColor: tokens.accent,
      ),
      segmentedButtonTheme: SegmentedButtonThemeData(
        style: SegmentedButton.styleFrom(
          foregroundColor: tokens.text2,
          selectedForegroundColor: tokens.text,
          selectedBackgroundColor: tokens.raised,
          side: BorderSide(color: tokens.hairlineStrong),
          textStyle: text.labelLarge,
          minimumSize: const Size(0, 36),
          shape: control,
        ),
      ),
      sliderTheme: SliderThemeData(
        activeTrackColor: tokens.accent,
        inactiveTrackColor: tokens.hairlineStrong,
        thumbColor: tokens.accent,
        overlayColor: tokens.accentDim,
        valueIndicatorColor: tokens.text,
        valueIndicatorTextStyle:
            text.labelLarge?.copyWith(color: tokens.canvas),
        trackHeight: 4,
      ),
      // Bars share the canvas: the sidebar's tone, not a rule, separates
      // the frame from the page.
      appBarTheme: AppBarTheme(
        backgroundColor: tokens.canvas,
        foregroundColor: tokens.text,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        toolbarHeight: 48,
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
          borderRadius: BorderRadius.circular(SonderRadius.card),
          side: BorderSide(color: tokens.hairline),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: tokens.panel,
        hintStyle: text.bodyMedium?.copyWith(color: tokens.muted),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.md,
          vertical: SonderSpace.md,
        ),
        // Field boundaries are control boundaries: hairlineStrong keeps them
        // at 3:1 or better against every surface (WCAG 1.4.11).
        border: field(tokens.hairlineStrong),
        enabledBorder: field(tokens.hairlineStrong),
        disabledBorder: field(tokens.hairline),
        focusedBorder: field(tokens.accentText, width: 1.5),
        errorBorder: field(tokens.danger),
        focusedErrorBorder: field(tokens.danger, width: 1.5),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: tokens.panel,
        selectedColor: tokens.raised,
        labelStyle: text.labelLarge?.copyWith(color: tokens.text),
        side: BorderSide(color: tokens.hairline),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.pill),
        ),
        padding: const EdgeInsets.symmetric(
            horizontal: SonderSpace.sm, vertical: SonderSpace.xs),
      ),
      listTileTheme: ListTileThemeData(
        shape: control,
        selectedTileColor: tokens.raised,
        selectedColor: tokens.text,
        iconColor: tokens.text2,
        textColor: tokens.text,
        contentPadding: const EdgeInsets.symmetric(horizontal: SonderSpace.md),
        minVerticalPadding: SonderSpace.sm,
      ),
      drawerTheme: DrawerThemeData(
        backgroundColor: tokens.sunken,
        surfaceTintColor: Colors.transparent,
        scrimColor: tokens.scrim,
        elevation: 0,
        shape: const RoundedRectangleBorder(),
      ),
      navigationDrawerTheme: NavigationDrawerThemeData(
        backgroundColor: tokens.sunken,
        surfaceTintColor: Colors.transparent,
        indicatorColor: tokens.raised,
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: tokens.sunken,
        indicatorColor: tokens.raised,
        selectedIconTheme: IconThemeData(color: tokens.text, size: 20),
        unselectedIconTheme: IconThemeData(color: tokens.text2, size: 20),
        selectedLabelTextStyle: text.labelMedium?.copyWith(color: tokens.text),
        unselectedLabelTextStyle:
            text.labelMedium?.copyWith(color: tokens.text2),
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: tokens.panel,
        surfaceTintColor: Colors.transparent,
        elevation: 12,
        shadowColor: tokens.shadow,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.sheet),
          side: BorderSide(color: tokens.hairline),
        ),
        titleTextStyle: text.titleLarge,
        contentTextStyle: text.bodyMedium,
      ),
      bottomSheetTheme: BottomSheetThemeData(
        backgroundColor: tokens.panel,
        surfaceTintColor: Colors.transparent,
        modalBarrierColor: tokens.scrim,
        dragHandleColor: tokens.hairlineStrong,
        shape: const RoundedRectangleBorder(
          borderRadius:
              BorderRadius.vertical(top: Radius.circular(SonderRadius.sheet)),
        ),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: tokens.panel,
        surfaceTintColor: Colors.transparent,
        elevation: 8,
        shadowColor: tokens.shadow,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.menu),
          side: BorderSide(color: tokens.hairline),
        ),
        textStyle: text.bodyMedium,
      ),
      // Toasts and tooltips invert the surface, so they read as a layer
      // above the page on either theme without a border.
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: tokens.text,
        contentTextStyle: text.bodyMedium?.copyWith(color: tokens.canvas),
        actionTextColor: tokens.canvas,
        closeIconColor: tokens.canvas,
        elevation: 6,
        shape: control,
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: tokens.text,
          borderRadius: BorderRadius.circular(SonderRadius.control),
        ),
        padding: const EdgeInsets.symmetric(
            horizontal: SonderSpace.sm, vertical: SonderSpace.xs),
        textStyle: text.bodySmall?.copyWith(color: tokens.canvas),
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
              : tokens.panel,
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
          padding: const EdgeInsets.symmetric(horizontal: SonderSpace.lg),
          textStyle: text.labelLarge,
          shape: control,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: tokens.text,
          minimumSize: const Size(0, 36),
          padding: const EdgeInsets.symmetric(horizontal: SonderSpace.lg),
          side: BorderSide(color: tokens.hairlineStrong),
          textStyle: text.labelLarge,
          shape: control,
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: tokens.text2,
          minimumSize: const Size(0, 36),
          textStyle: text.labelLarge,
          shape: control,
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(
          foregroundColor: tokens.text2,
          shape: control,
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
          borderRadius: BorderRadius.circular(SonderRadius.card),
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

    // One family, three weights. Reading text is 16/26; everything that is
    // chrome is smaller and quieter than the conversation it frames.
    return TextTheme(
      headlineMedium: style(28, 36, FontWeight.w500, spacing: -0.4),
      headlineSmall: style(22, 28, FontWeight.w600, spacing: -0.2),
      titleLarge: style(20, 26, FontWeight.w600, spacing: -0.2),
      titleMedium: style(16, 24, FontWeight.w500),
      titleSmall: style(14, 20, FontWeight.w500),
      bodyLarge: style(16, 26, FontWeight.w400),
      bodyMedium: style(14, 22, FontWeight.w400),
      bodySmall: style(12, 18, FontWeight.w400, color: tokens.text2),
      labelLarge: style(13, 18, FontWeight.w500),
      labelMedium: style(12, 16, FontWeight.w500, color: tokens.text2),
      labelSmall: style(12, 16, FontWeight.w500, color: tokens.muted),
    );
  }
}

/// The corner radii, by role. Softer than a dashboard's: small controls 8,
/// rows, fields and buttons 10, menus 12, cards 16, sheets, dialogs and the
/// user bubble 20, the composer 26, and pills.
abstract final class SonderRadius {
  static const control = 8.0;
  static const row = 10.0;
  static const menu = 12.0;
  static const card = 16.0;
  static const sheet = 20.0;
  static const bubble = 20.0;
  static const composer = 26.0;
  static const pill = 999.0;
}

/// The mark's own colours: the brand gradient and the navy tile under it.
/// They belong to the mark (`ui/sonder_mark.dart` is their only reader), not
/// to the interface, and they live here so this file stays the one place a
/// colour value is written.
abstract final class SonderMarkColors {
  static const gradient = [
    Color(0xFFB56CF7),
    Color(0xFF8A85FB),
    Color(0xFF4FA6FF),
  ];
  static const tileTop = Color(0xFF16173D);
  static const tileBottom = Color(0xFF0B0C22);
}

/// The spacing scale: a 4-point grid. Layout gaps and padding come from here
/// rather than one-off literals (7, 9, 11...), so rhythm stays consistent.
///
/// [tight] and [snug] are half steps for the inside of dense controls only (a
/// glyph beside its label in a chip, the inset of a compact row); layout gaps
/// use the 4-point steps. [hair] is a one-pixel optical nudge, never a gap.
abstract final class SonderSpace {
  static const hair = 1.0;
  static const xxs = 2.0;
  static const xs = 4.0;
  static const tight = 6.0;
  static const sm = 8.0;
  static const snug = 10.0;
  static const md = 12.0;
  static const lg = 16.0;
  static const xl = 20.0;
  static const xxl = 24.0;
  static const x3 = 32.0;
  static const x4 = 40.0;
  static const x5 = 48.0;
  static const x6 = 64.0;
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
/// Five surfaces carry the hierarchy by tone: [sunken] (the sidebar, code
/// and raw-output wells) sits below [canvas] (the page); [panel] (cards,
/// fields, menus, the composer) sits above it; [raised] is the hover and
/// selection fill on any of them; [bubble] is the user's turn. Semantic
/// tones carry meaning together with a label or a glyph, never alone;
/// [SonderTokens.auto] is the one mode colour that is not also a risk tone,
/// which is why it has its own name.
class SonderTokens extends ThemeExtension<SonderTokens> {
  final Color canvas;

  /// One tone below the canvas: the sidebar, the drawer and wells.
  final Color sunken;
  final Color panel;
  final Color raised;

  /// The user's message bubble.
  final Color bubble;
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

  /// The soft shadow under floating surfaces (composer, menus, dialogs).
  final Color shadow;

  /// The veil behind a drawer, sheet or dialog.
  final Color scrim;

  const SonderTokens({
    required this.canvas,
    required this.sunken,
    required this.panel,
    required this.raised,
    required this.bubble,
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
    required this.shadow,
    required this.scrim,
  });

  static const dark = SonderTokens(
    canvas: Color(0xFF1B1A18),
    sunken: Color(0xFF151412),
    panel: Color(0xFF242321),
    raised: Color(0xFF32312D),
    bubble: Color(0xFF2C2B28),
    hairline: Color(0xFF34332F),
    hairlineStrong: Color(0xFF807C74),
    text: Color(0xFFECEAE4),
    text2: Color(0xFFB8B4AB),
    muted: Color(0xFF9E9A92),
    accent: Color(0xFFABA4F7),
    accentText: Color(0xFFB6B0FA),
    onAccent: Color(0xFF17143A),
    accentDim: Color(0x2EABA4F7),
    ok: Color(0xFF7FCF96),
    info: Color(0xFF74C2E6),
    warn: Color(0xFFEDBF66),
    danger: Color(0xFFF27F7B),
    dangerDim: Color(0x2EF27F7B),
    auto: Color(0xFFE59AD4),
    mutation: Color(0xFFEFA070),
    execution: Color(0xFFCDA88F),
    shadow: Color(0x73000000),
    scrim: Color(0x99000000),
  );

  static const light = SonderTokens(
    canvas: Color(0xFFF7F6F3),
    sunken: Color(0xFFF0EEEA),
    panel: Color(0xFFFFFFFF),
    raised: Color(0xFFE5E2DC),
    bubble: Color(0xFFECE9E4),
    hairline: Color(0xFFE0DDD6),
    hairlineStrong: Color(0xFF827E77),
    text: Color(0xFF1F1E1C),
    text2: Color(0xFF514E49),
    muted: Color(0xFF66635D),
    accent: Color(0xFF5B54C8),
    accentText: Color(0xFF4D45B5),
    onAccent: Color(0xFFFFFFFF),
    accentDim: Color(0x1F5B54C8),
    ok: Color(0xFF196B3C),
    info: Color(0xFF1B6584),
    warn: Color(0xFF835500),
    danger: Color(0xFFB52D2D),
    dangerDim: Color(0x1FB52D2D),
    auto: Color(0xFF913585),
    mutation: Color(0xFF9A4514),
    execution: Color(0xFF71523D),
    shadow: Color(0x1F1F1E1C),
    scrim: Color(0x521F1E1C),
  );

  /// The tokens of the ambient theme; the dark set when a caller sits
  /// outside a [Theme] (tests that pump a bare widget).
  static SonderTokens of(BuildContext context) {
    return Theme.of(context).extension<SonderTokens>() ??
        (Theme.of(context).brightness == Brightness.dark ? dark : light);
  }

  /// The lift under a floating surface: one wide, soft shadow, pulled in so
  /// it reads as depth rather than as an outline.
  List<BoxShadow> get lift => [
        BoxShadow(
          color: shadow,
          blurRadius: SonderSpace.x3,
          spreadRadius: -SonderSpace.md,
          offset: const Offset(0, SonderSpace.md),
        ),
      ];

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
    Color? sunken,
    Color? panel,
    Color? raised,
    Color? bubble,
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
    Color? shadow,
    Color? scrim,
  }) {
    return SonderTokens(
      canvas: canvas ?? this.canvas,
      sunken: sunken ?? this.sunken,
      panel: panel ?? this.panel,
      raised: raised ?? this.raised,
      bubble: bubble ?? this.bubble,
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
      shadow: shadow ?? this.shadow,
      scrim: scrim ?? this.scrim,
    );
  }

  @override
  SonderTokens lerp(ThemeExtension<SonderTokens>? other, double t) {
    if (other is! SonderTokens) return this;
    Color mix(Color a, Color b) => Color.lerp(a, b, t) ?? a;
    return SonderTokens(
      canvas: mix(canvas, other.canvas),
      sunken: mix(sunken, other.sunken),
      panel: mix(panel, other.panel),
      raised: mix(raised, other.raised),
      bubble: mix(bubble, other.bubble),
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
      shadow: mix(shadow, other.shadow),
      scrim: mix(scrim, other.scrim),
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
