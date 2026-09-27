import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/ui/status_line.dart';

/// Exact-string tests for the style.py ports chat renders with (lane B's
/// lib/ui/status_line.dart, which replaced chat's private copy). Expected values were produced by style.py itself (colour off,
/// unicode glyphs) or copied from tests/repl/test_style.py.
void main() {
  // style.compact_count
  test('compactCount matches style.compact_count', () {
    expect(
      [64, 999, 1000, 8192, 2600, 143, 1250000, 1000000].map(compactCount),
      ['64', '999', '1k', '8.2k', '2.6k', '143', '1.2M', '1M'],
    );
  });

  // style.duration_label
  test('one-decimal labels round exact ties half to even like CPython', () {
    // Was chat's fixed1(); lane B keeps the rounding private, so check it
    // through the labels that use it.
    expect([1250, 1350, 2750, 8192].map(durationLabel),
        ['1.2s', '1.4s', '2.8s', '8.2s']);
    expect([1250, 1350, 2750, 8192].map(compactCount),
        ['1.2k', '1.4k', '2.8k', '8.2k']);
    // Decimal ties that are not exact binary fractions (the old fixed1
    // 0.15 / 0.05 cases): CPython's '%.1f' follows the stored double.
    expect([1050, 1150, 1450].map(durationLabel), ['1.1s', '1.1s', '1.4s']);
    expect([1050, 1150, 1450].map(compactCount), ['1.1k', '1.1k', '1.4k']);
  });

  test('durationLabel matches style.duration_label', () {
    expect(
      [231, 1200, 75700, 99999, 135000, 3600000].map(durationLabel),
      ['231ms', '1.2s', '75.7s', '100.0s', '2m 15s', '60m 00s'],
    );
  });

  // style.status_line — tests/repl/test_style.py::test_status_line_width_variants
  test('statusLine matches style.status_line at every width', () {
    const elevated = StatusState(
      mode: 'manual',
      tier: 'code',
      model: 'sonder:latest',
      ctxUsed: 64,
      ctxLimit: 8192,
      agents: 2,
      project: 'foo',
      elevated: true,
      elevatedReason: 'dev bypass',
    );
    expect(
      statusLine(elevated, 110),
      'code · sonder:latest · manual ELEVATED (dev bypass) · ctx 64/8.2k · '
      '2 agents · proj foo',
    );
    expect(statusLine(elevated, 70), isNot(contains('proj')));
    final at50 = statusLine(elevated, 50);
    expect(at50, isNot(contains('agents')));
    expect(at50, contains('…'));
    expect(statusLine(elevated, 30), 'code · manual ELEVATED');
    const plain = StatusState(
        mode: 'manual',
        tier: 'code',
        model: 'sonder:latest',
        ctxUsed: 64,
        ctxLimit: 8192);
    expect(statusLine(plain, 30), 'code · manual · ctx 64/8.2k');

    // Values generated with style.status_line.
    const st = StatusState(
        model: 'sonder:latest', ctxUsed: 2100, ctxLimit: 8192, agents: 2);
    expect(statusLine(st, 120),
        'code · sonder:latest · manual · ctx 2.1k/8.2k · 2 agents');
    expect(statusLine(st, 45), 'code · sonder:late… · manual · ctx 2.1k/8.2k');
    expect(statusLine(st, 32), 'code · manual · ctx 2.1k/8.2k');
    expect(statusLine(st, 20), 'code · manual');
    const long = StatusState(
        model: 'a-very-long-model-name:with-tag-q4',
        ctxUsed: 2100,
        ctxLimit: 8192,
        agents: 2,
        project: 'engine');
    expect(statusLine(long, 50),
        'code · a-very-long-mode… · manual · ctx 2.1k/8.2k');
  });

  test('the mode word survives at 320 px worth of cells', () {
    // 320 px / ~6.6 px per 11 px mono cell ≈ 48 cells; test far narrower.
    for (final width in [8, 12, 20, 32, 48]) {
      final fields = statusSegments(
          const StatusState(
              mode: 'acceptEdits',
              model: 'qwen2.5-coder:7b-instruct-q4_K_M',
              ctxUsed: 5100,
              ctxLimit: 32768,
              agents: 2),
          width);
      expect(fields.any((f) => f.field == StatusField.mode), isTrue,
          reason: 'width $width');
    }
  });

  test('zero counts are hidden', () {
    expect(statusLine(const StatusState(model: 'm', agents: 0), 120),
        'code · m · manual');
  });

  // style.footer — tests/repl/test_style.py::test_footer_forms, without the
  // REPL's two-cell indent (the reading column indents) and with rate '' (the
  // app's rating controls are buttons).
  test('footerLine matches style.footer', () {
    expect(
      footerLine(
          const FooterState(
              elapsedMs: 75700,
              modelCalls: 2,
              tokensIn: 2600,
              tokensOut: 43,
              action: 'rate: /pass /fail'),
          80),
      'done 75.7s · 2 model calls · 2.6k→43 tok · rate: /pass /fail',
    );
    expect(
      footerLine(
          const FooterState(elapsedMs: 231, ok: false, hint: 'start ollama'),
          80),
      'failed after 231ms · hint: start ollama',
    );
    expect(
        footerLine(
            const FooterState(elapsedMs: 1200, action: '/pass /fail'), 80),
        'done 1.2s · /pass /fail');
    // Generated with style.footer(rate='').
    expect(
      footerLine(
          const FooterState(
              elapsedMs: 61200, modelCalls: 2, tokensIn: 2600, tokensOut: 143),
          1 << 20),
      'done 61.2s · 2 model calls · 2.6k→143 tok',
    );
    expect(
      footerLine(
              const FooterState(
                  elapsedMs: 61200,
                  modelCalls: 2,
                  tokensIn: 2600,
                  tokensOut: 143,
                  toolCalls: 3),
              30,
              // style.footer's two-cell indent counts toward the width.
              indent: '  ')
          .trimLeft(),
      'done 61.2s · 2 model calls',
    );
    expect(
      footerLine(
          const FooterState(elapsedMs: 12000, ok: false, hint: 'start ollama'),
          1 << 20),
      'failed after 12.0s · hint: start ollama',
    );
  });

  // style.live_line, minus "Ctrl-C cancels" (Stop is a button in the app).
  test('liveLine drops model, tokens, then the slow hint', () {
    expect(
      liveLine(
          const LiveState(
              phase: 'routing', elapsedS: 12, model: 'sonder:latest'),
          80),
      '◈ working · routing · 12s · sonder:latest',
    );
    const slow = LiveState(
        phase: 'model call 1/2',
        elapsedS: 42,
        model: 'sonder:latest',
        tokensIn: 2600,
        slow: true);
    expect(
        liveLine(slow, 200),
        '◈ working · model call 1/2 · 42s · sonder:latest · 2.6k tok in · '
        'slow local model? try the fast route');
    expect(
        liveLine(slow, 90),
        '◈ working · model call 1/2 · 42s · 2.6k tok in · slow local model? '
        'try the fast route');
    expect(liveLine(slow, 80),
        '◈ working · model call 1/2 · 42s · slow local model? try the fast route');
    expect(liveLine(slow, 40), '◈ working · model call 1/2 · 42s');
    expect(
      liveLine(const LiveState(phase: 'routing', elapsedS: 75), 40),
      '◈ working · routing · 1m 15s',
    );
  });
}
