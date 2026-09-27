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
      final surfaces = {
        'canvas': tokens.canvas,
        'panel': tokens.panel,
        'raised': tokens.raised,
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
        // Field outlines and chip/button sides use hairlineStrong; focus
        // rings and the selected rail icon use accentText (the light fill
        // accent #1FA597 is 2.8:1 on the canvas, fine for labelled fills
        // only).
        for (final boundary in {
          'hairlineStrong': tokens.hairlineStrong,
          'accentText': tokens.accentText,
        }.entries) {
          for (final bg in surfaces.entries) {
            expect(SonderContrast.ratio(boundary.value, bg.value),
                greaterThanOrEqualTo(SonderContrast.control),
                reason: '${boundary.key} on ${bg.key}');
          }
        }
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
}
