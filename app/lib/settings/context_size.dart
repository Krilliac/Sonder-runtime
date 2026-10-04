/// Context size as Settings edits it: a requested conversation window, in
/// tokens, with slider stops for the common sizes and an exact entry.
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../theme.dart';

/// The smallest window worth requesting: the runtime floors every native
/// window at 512 tokens (`platform/context_policy.py`, `MIN_CONTEXT`).
const contextSizeMin = 512;

/// The largest window the runtime accepts
/// (`application/lifecycle.py`, `MAX_CONTEXT_TOKENS`).
const contextSizeMax = 1000000;

/// The default request when the field is left empty.
const contextSizeDefault = '8192';

/// Slider stops, 4k to 128k.
const contextSizePresets = [4096, 8192, 16384, 32768, 65536, 131072];

/// `lifecycle.normalize_context_size`'s grammar: up to seven digits, an
/// optional three-digit fraction and a `k` (1,000) or `m` (1,000,000).
final _contextGrammar = RegExp(r'^(\d{1,7})(?:\.(\d{1,3}))?([km]?)$');

/// The tokens a context size asks for, read the way the runtime reads it
/// ("8192", "32k", "1.5m"), or null when it is not a whole number of tokens.
int? parseContextTokens(String text) {
  final match = _contextGrammar.firstMatch(text.trim().toLowerCase());
  if (match == null) return null;
  final fraction = match.group(2) ?? '';
  final multiplier = switch (match.group(3)) {
    'k' => 1000,
    'm' => 1000000,
    _ => 1,
  };
  final scale = math.pow(10, fraction.length).toInt();
  final scaled = (int.parse(match.group(1)!) * scale +
          (fraction.isEmpty ? 0 : int.parse(fraction))) *
      multiplier;
  if (scaled % scale != 0) return null;
  return scaled ~/ scale;
}

/// [tokens] within what Settings lets you request.
int clampContextTokens(int tokens) =>
    tokens.clamp(contextSizeMin, contextSizeMax);

/// Why [text] cannot be saved as a context size, or null. Out-of-range
/// sizes are clamped rather than refused; empty means the default.
String? contextSizeError(String text) {
  if (text.trim().isEmpty) return null;
  return parseContextTokens(text) == null
      ? 'Use a whole number of tokens, like 8192 or 32k.'
      : null;
}

/// What Settings stores: whole tokens within range, the default when the
/// field is empty, and the text unchanged when it does not parse (Save
/// refuses it first).
String canonicalContextSize(String text) {
  final trimmed = text.trim();
  if (trimmed.isEmpty) return contextSizeDefault;
  final tokens = parseContextTokens(trimmed);
  return tokens == null ? trimmed : '${clampContextTokens(tokens)}';
}

/// "4k" for a power-of-two multiple of 1,024, else the grouped count.
String contextSizeLabel(int tokens) {
  if (tokens >= 1024 && tokens % 1024 == 0) return '${tokens ~/ 1024}k';
  final digits = '$tokens';
  final out = StringBuffer();
  for (var i = 0; i < digits.length; i++) {
    if (i > 0 && (digits.length - i) % 3 == 0) out.write(',');
    out.write(digits[i]);
  }
  return out.toString();
}

/// The slider half of the context-size control: six stops from 4k to 128k
/// with their labels under the track. A size between stops sits between
/// them; one outside the range rests at the nearest end, and the exact
/// entry beside it always shows the real number.
class ContextSizeSlider extends StatelessWidget {
  /// The current request, or null while the entry does not parse.
  final int? tokens;
  final ValueChanged<int> onChanged;

  const ContextSizeSlider({
    super.key,
    required this.tokens,
    required this.onChanged,
  });

  /// Track inset: the overlay radius this slider is themed with.
  static const _inset = 20.0;

  static double _position(int tokens) =>
      (math.log(tokens) / math.ln2 - 12).clamp(0.0, 5.0).toDouble();

  @override
  Widget build(BuildContext context) {
    final colors = SonderTokens.of(context);
    final current = tokens;
    final value = current == null ? 1.0 : _position(current);
    // The stop the request sits exactly on, or -1.
    final exact = current == null ? -1 : contextSizePresets.indexOf(current);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        SliderTheme(
          data: SliderTheme.of(context).copyWith(
            overlayShape: const RoundSliderOverlayShape(overlayRadius: _inset),
            showValueIndicator: ShowValueIndicator.never,
          ),
          child: Semantics(
            container: true,
            label: 'Context size preset',
            child: Slider(
              key: const Key('settings-context-slider'),
              value: value,
              max: 5,
              divisions: 5,
              semanticFormatterCallback: (v) =>
                  '${contextSizeLabel(contextSizePresets[v.round()])} tokens',
              onChanged: (v) => onChanged(contextSizePresets[v.round()]),
            ),
          ),
        ),
        ExcludeSemantics(
          child: LayoutBuilder(builder: (context, constraints) {
            final track = constraints.maxWidth - 2 * _inset;
            return SizedBox(
              height: SonderSpace.lg,
              child: Stack(clipBehavior: Clip.none, children: [
                for (var i = 0; i < contextSizePresets.length; i++)
                  Positioned(
                    left: _inset + track * i / 5 - SonderSpace.xxl,
                    width: SonderSpace.xxl * 2,
                    child: Text(
                      contextSizeLabel(contextSizePresets[i]),
                      textAlign: TextAlign.center,
                      style: colors.mono(11,
                          color: exact == i ? colors.accentText : colors.muted,
                          weight:
                              exact == i ? FontWeight.w600 : FontWeight.w400),
                    ),
                  ),
              ]),
            );
          }),
        ),
      ],
    );
  }
}
