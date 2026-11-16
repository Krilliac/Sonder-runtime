import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/sheet.dart';
import 'package:sonder_runtime/ui/status_vocab.dart';

/// P2-4: every text/background pair reaches 4.5:1 and every control
/// boundary 3:1, in both themes (WCAG 2.x 1.4.3 and 1.4.11).
void main() {
  test('contrast arithmetic matches WCAG reference values', () {
    expect(
        SonderContrast.ratio(Colors.black, Colors.white), closeTo(21.0, 0.01));
    expect(SonderContrast.ratio(Colors.white, Colors.white), 1.0);
    // #767676 on white is the well-known 4.54:1 grey.
    expect(SonderContrast.ratio(const Color(0xFF767676), Colors.white),
        closeTo(4.54, 0.01));
  });

  for (final (name, tokens) in [
    ('dark', SonderTokens.dark),
    ('light', SonderTokens.light),
  ]) {
    group('$name theme', () {
      // Every tone something can be written on: the page, the sidebar and
      // wells, cards and the composer, hover and selection, the user bubble.
      final surfaces = {
        'canvas': tokens.canvas,
        'sunken': tokens.sunken,
        'panel': tokens.panel,
        'raised': tokens.raised,
        'bubble': tokens.bubble,
      };
      final texts = {
        'text': tokens.text,
        'text2': tokens.text2,
        'muted': tokens.muted,
        'accentText': tokens.accentText,
        'ok': tokens.ok,
        'info': tokens.info,
        'warn': tokens.warn,
        'danger': tokens.danger,
        'auto': tokens.auto,
        'mutation': tokens.mutation,
        'execution': tokens.execution,
      };

      test('text and status words reach 4.5:1 on every surface', () {
        final failures = <String>[];
        for (final fg in texts.entries) {
          for (final bg in surfaces.entries) {
            final ratio = SonderContrast.ratio(fg.value, bg.value);
            if (ratio < SonderContrast.text) {
              failures
                  .add('${fg.key} on ${bg.key}: ${ratio.toStringAsFixed(2)}');
            }
          }
        }
        expect(failures, isEmpty);
      });

      test('every status kind and mode word is legible', () {
        for (final kind in StatusKind.values) {
          for (final bg in surfaces.values) {
            expect(SonderContrast.ratio(kind.color(tokens), bg),
                greaterThanOrEqualTo(SonderContrast.text),
                reason: kind.name);
          }
        }
        for (final mode in permissionModes) {
          expect(
              SonderContrast.ratio(modeStyle(mode).color(tokens), tokens.panel),
              greaterThanOrEqualTo(SonderContrast.text),
              reason: mode);
        }
      });

      test('control boundaries reach 3:1', () {
        // Field outlines and button sides use hairlineStrong; focus rings
        // use accentText; the send button and the switch are known by
        // their accent fill.
        for (final boundary in {
          'hairlineStrong': tokens.hairlineStrong,
          'accentText': tokens.accentText,
          'accent': tokens.accent,
        }.entries) {
          for (final bg in surfaces.entries) {
            expect(SonderContrast.ratio(boundary.value, bg.value),
                greaterThanOrEqualTo(SonderContrast.control),
                reason: '${boundary.key} on ${bg.key}');
          }
        }
      });

      test('surface tones step apart, so tone can carry the hierarchy', () {
        // The sidebar and cards against the page; hover and selection
        // against whatever they sit on; the user bubble against the page.
        double step(Color a, Color b) => SonderContrast.ratio(a, b);
        expect(step(tokens.canvas, tokens.sunken), greaterThanOrEqualTo(1.05));
        expect(step(tokens.canvas, tokens.panel), greaterThanOrEqualTo(1.05));
        expect(step(tokens.canvas, tokens.bubble), greaterThanOrEqualTo(1.1));
        for (final base in [tokens.canvas, tokens.sunken, tokens.panel]) {
          expect(step(tokens.raised, base), greaterThanOrEqualTo(1.1));
        }
      });

      test('toasts and tooltips invert the surface legibly', () {
        expect(SonderContrast.ratio(tokens.canvas, tokens.text),
            greaterThanOrEqualTo(SonderContrast.text));
      });

      test('labels on filled buttons reach 4.5:1', () {
        expect(SonderContrast.ratio(tokens.onAccent, tokens.accent),
            greaterThanOrEqualTo(SonderContrast.text));
        for (final role in [StatusRole.warning, StatusRole.danger]) {
          final (fill, onFill) = ToneButton.colors(tokens, role);
          expect(SonderContrast.ratio(onFill, fill),
              greaterThanOrEqualTo(SonderContrast.text),
              reason: role.name);
        }
      });
    });
  }

  test('the accent is Iris, not the retired teal, in both themes', () {
    // Hue is the claim: blue-violet, between 235 and 265 degrees.
    for (final tokens in [SonderTokens.dark, SonderTokens.light]) {
      for (final accent in [tokens.accent, tokens.accentText]) {
        expect(HSLColor.fromColor(accent).hue, inInclusiveRange(235, 265));
      }
    }
    expect(SonderTheme.light.colorScheme.primary, SonderTokens.light.accent);
    expect(SonderTheme.dark.colorScheme.primary, SonderTokens.dark.accent);
  });

  test('the neutrals are warm grey: red over blue, and barely', () {
    for (final tokens in [SonderTokens.dark, SonderTokens.light]) {
      for (final surface in [
        tokens.canvas,
        tokens.sunken,
        tokens.raised,
        tokens.bubble,
      ]) {
        final warmth = (surface.r - surface.b) * 255;
        expect(warmth, inInclusiveRange(1, 12), reason: '$surface');
      }
    }
  });

  test('the theme wires field borders and the symbol fallback', () {
    for (final theme in [SonderTheme.dark, SonderTheme.light]) {
      final tokens = theme.extension<SonderTokens>()!;
      final enabled =
          theme.inputDecorationTheme.enabledBorder as OutlineInputBorder;
      expect(enabled.borderSide.color, tokens.hairlineStrong);
      final focused =
          theme.inputDecorationTheme.focusedBorder as OutlineInputBorder;
      expect(focused.borderSide.color, tokens.accentText);
      expect(theme.textTheme.bodyMedium?.fontFamilyFallback,
          contains(SonderTheme.symbols));
      expect(tokens.mono(13).fontFamilyFallback, contains(SonderTheme.symbols));
    }
  });

  test('every motion token runs between 150 and 250 ms', () {
    final steps = [SonderMotion.fast, SonderMotion.medium, SonderMotion.slow]
        .map((duration) => duration.inMilliseconds)
        .toList();
    for (final step in steps) {
      expect(step, inInclusiveRange(150, 250));
    }
    // Shortest first, and each step is a different length.
    expect(steps, orderedEquals([...steps]..sort()));
    expect(steps.toSet(), hasLength(steps.length));
    // A page arrives on the longest step and leaves on a shorter one.
    const pages = SonderPageTransitionsBuilder();
    expect(pages.transitionDuration, SonderMotion.slow);
    expect(pages.reverseTransitionDuration, SonderMotion.medium);
  });
}
