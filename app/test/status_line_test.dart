import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/ui/status_line.dart';

// Expected strings are captured from sonder_runtime/interfaces/repl/style.py
// with Caps(color="none", glyphs="unicode"), using the fixtures of
// tests/repl/test_style.py (STATUS, the live and footer states). Each group
// names the style.py function it mirrors.

const _plain = StatusState(
    mode: 'manual',
    tier: 'code',
    model: 'sonder:latest',
    ctxUsed: 64,
    ctxLimit: 8192);
const _busy = StatusState(
    mode: 'acceptEdits',
    tier: 'code',
    model: 'qwen2.5-coder:7b-instruct-q4_K_M',
    ctxUsed: 5100,
    ctxLimit: 32768,
    agents: 2,
    lanes: 1,
    project: 'alpha-site');
const _elevated = StatusState(
    mode: 'manual',
    tier: 'code',
    model: 'sonder:latest',
    ctxUsed: 64,
    ctxLimit: 8192,
    agents: 2,
    project: 'foo',
    elevated: true,
    elevatedReason: 'dev bypass');

void main() {
  group('status_line', () {
    const expected = <String, Map<int, String>>{
      'plain': {
        20: 'code · manual',
        30: 'code · manual · ctx 64/8.2k',
        40: 'code · sonder:l… · manual · ctx 64/8.2k',
        50: 'code · sonder:latest · manual · ctx 64/8.2k',
        110: 'code · sonder:latest · manual · ctx 64/8.2k',
      },
      'busy': {
        20: 'code · acceptEdits',
        30: 'code · acceptEdits',
        40: 'code · acceptEdits · ctx 5.1k/32.8k',
        50: 'code · qwen2.5-co… · acceptEdits · ctx 5.1k/32.8k',
        60: 'code · qwen2.5-coder:7b-ins… · acceptEdits · ctx 5.1k/32.8k',
        80: 'code · qwen2.5-coder:7b-instruct-q4_K_M · acceptEdits · ctx 5.1k/32.8k',
        110:
            'code · qwen2.5-coder:7b-instruct-q4_K_M · acceptEdits · ctx 5.1k/32.8k · 2 agents · 1 lane · proj alpha-site',
      },
      'elevated': {
        20: 'manual ELEVATED',
        30: 'code · manual ELEVATED',
        40: 'code · manual ELEVATED · ctx 64/8.2k',
        50: 'code · sonder:la… · manual ELEVATED · ctx 64/8.2k',
        60: 'code · sonder:latest · manual ELEVATED · ctx 64/8.2k',
        80: 'code · sonder:latest · manual ELEVATED (dev bypass) · ctx 64/8.2k · 2 agents',
        110:
            'code · sonder:latest · manual ELEVATED (dev bypass) · ctx 64/8.2k · 2 agents · proj foo',
      },
    };
    const states = {'plain': _plain, 'busy': _busy, 'elevated': _elevated};
    for (final entry in expected.entries) {
      for (final width in entry.value.entries) {
        test('${entry.key} at ${width.key}', () {
          final line = statusLine(states[entry.key]!, width.key);
          expect(line, width.value);
          expect(cellWidth(line), lessThanOrEqualTo(width.key - 1));
        });
      }
    }

    test('the mode word is never dropped, even at 20 cells', () {
      for (final mode in ['plan', 'manual', 'acceptEdits', 'auto']) {
        final line = statusLine(StatusState(mode: mode, model: 'm'), 20);
        expect(line, contains(mode));
      }
    });

    test('same fields before and after a turn', () {
      final before = statusLine(
          const StatusState(model: 'sonder:latest', ctxUsed: 0, ctxLimit: 8192),
          110);
      final after = statusLine(
          const StatusState(
              model: 'sonder:latest', ctxUsed: 64, ctxLimit: 8192),
          110);
      expect(before.replaceAll('ctx 0/', 'ctx 64/'), after);
    });

    test('app-only pending approvals ride with the agent counts', () {
      const st = StatusState(
          model: 'sonder:latest',
          ctxUsed: 2100,
          ctxLimit: 8200,
          pendingApprovals: 1);
      expect(statusLine(st, 110),
          'code · sonder:latest · manual · ctx 2.1k/8.2k · 1 pending approval');
      expect(statusLine(st, 50), isNot(contains('pending')));
    });

    test('segments tag the mode word for styling', () {
      final segments = statusSegments(_elevated, 110);
      expect(segments.map((s) => s.field), [
        StatusField.tier,
        StatusField.model,
        StatusField.elevated,
        StatusField.ctx,
        StatusField.agents,
        StatusField.project,
      ]);
    });
  });

  group('live_line', () {
    const live = LiveState(
        phase: 'model call 1/2',
        elapsedS: 42,
        model: 'sonder:latest',
        tokensIn: 2600,
        slow: true,
        slowHint: 'slow local model? /model fast',
        cancelHint: 'Ctrl-C cancels');
    const expected = {
      20: '◈ working · model …',
      30: '◈ working · model call 1/2 ·…',
      40: '◈ working · model call 1/2 · 42s',
      50: '◈ working · model call 1/2 · 42s · Ctrl-C cancels',
      60: '◈ working · model call 1/2 · 42s · Ctrl-C cancels',
      80: '◈ working · model call 1/2 · 42s · slow local model? /model fast',
      110:
          '◈ working · model call 1/2 · 42s · 2.6k tok in · slow local model? /model fast · Ctrl-C cancels',
    };
    for (final entry in expected.entries) {
      test('REPL form at ${entry.key}', () {
        expect(liveLine(live, entry.key), entry.value);
      });
    }

    test('minutes and the app defaults (Stop is a button, no hint text)', () {
      expect(
          liveLine(
              const LiveState(
                  phase: 'routing',
                  elapsedS: 75,
                  model: 'm',
                  cancelHint: 'Ctrl-C cancels'),
              110),
          '◈ working · routing · 1m 15s · m · Ctrl-C cancels');
      expect(
          liveLine(
              const LiveState(
                  phase: 'routing', elapsedS: 12, model: 'sonder:latest'),
              110),
          '◈ working · routing · 12s · sonder:latest');
      expect(
          liveLine(
              const LiveState(phase: 'routing', elapsedS: 23, slow: true), 110),
          '◈ working · routing · 23s · slow local model? try the fast route');
    });
  });

  group('footer', () {
    const ok = FooterState(
        elapsedMs: 75700,
        modelCalls: 2,
        tokensIn: 2600,
        tokensOut: 43,
        action: 'rate: /pass /fail');
    const fail = FooterState(
        elapsedMs: 231,
        ok: false,
        hint: 'the model endpoint refused the connection; start ollama');
    const okExpected = {
      20: '  done 75.7s · rat…',
      30: '  done 75.7s · rate: /pass /…',
      40: '  done 75.7s · rate: /pass /fail',
      50: '  done 75.7s · 2 model calls · rate: /pass /fail',
      80: '  done 75.7s · 2 model calls · 2.6k→43 tok · rate: /pass /fail',
    };
    const failExpected = {
      20: '  failed after 231…',
      30: '  failed after 231ms · hint:…',
      40: '  failed after 231ms · hint: the model…',
      60: '  failed after 231ms · hint: the model endpoint refused th…',
      110:
          '  failed after 231ms · hint: the model endpoint refused the connection; start ollama',
    };
    for (final entry in okExpected.entries) {
      test('ok at ${entry.key}', () {
        expect(footerLine(ok, entry.key, indent: '  '), entry.value);
      });
    }
    for (final entry in failExpected.entries) {
      test('fail at ${entry.key}', () {
        expect(footerLine(fail, entry.key, indent: '  '), entry.value);
      });
    }
    test('short and bare forms', () {
      expect(
          footerLine(
              const FooterState(elapsedMs: 1200, action: '/pass /fail'), 80,
              indent: '  '),
          '  done 1.2s · /pass /fail');
      expect(
          footerLine(
              const FooterState(
                  elapsedMs: 61200,
                  modelCalls: 1,
                  toolCalls: 3,
                  tokensIn: 0,
                  tokensOut: 0),
              80,
              indent: '  '),
          '  done 61.2s · 1 model call · 3 tools');
      // The app form: no indent, no rating text (chips carry it).
      expect(
          footerLine(
              const FooterState(
                  elapsedMs: 61200,
                  modelCalls: 2,
                  tokensIn: 2600,
                  tokensOut: 143),
              120),
          'done 61.2s · 2 model calls · 2.6k→143 tok');
    });
  });

  test('compact_count and duration_label', () {
    const counts = {
      0: '0',
      64: '64',
      999: '999',
      1000: '1k',
      8192: '8.2k',
      2600: '2.6k',
      1250000: '1.2M',
      1000000: '1M',
      999999: '1000k',
      1250: '1.2k',
      1750: '1.8k',
      1350: '1.4k',
    };
    for (final entry in counts.entries) {
      expect(compactCount(entry.key), entry.value, reason: '${entry.key}');
    }
    const durations = {
      0: '0ms',
      231: '231ms',
      999: '999ms',
      1000: '1.0s',
      75700: '75.7s',
      99999: '100.0s',
      100000: '1m 40s',
      135000: '2m 15s',
      3600000: '60m 00s',
      1250: '1.2s',
    };
    for (final entry in durations.entries) {
      expect(durationLabel(entry.key), entry.value, reason: '${entry.key}');
    }
  });

  test('truncate and cell width', () {
    expect(truncateCells('sonder:latest', 8), 'sonder:…');
    expect(truncateCells('abc', 3), 'abc');
    expect(truncateCells('abc', 1), '…');
    expect(truncateCells('abc', 0), '');
    expect(cellWidth('◈ working'), 9);
    expect(cellWidth('模型'), 4);
  });
}
