@Tags(['golden'])
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/approval_sheet.dart';
import 'package:sonder_runtime/ui/status_line.dart';
import 'package:sonder_runtime/ui/status_row.dart';
import 'package:sonder_runtime/ui/status_vocab.dart';
import 'package:sonder_runtime/workspace_ui.dart';

import 'golden_fonts.dart';

// Lane B primitives (APP-PLAN §4): notice × kind × theme, approval sheet,
// raise sheet, status rows narrow/wide, markdown code and the glyph strip
// (no tofu offline). Update with:
//   flutter test --update-goldens --tags golden test/goldens/primitives_golden_test.dart

const _request = ApprovalRequest(
  tool: 'write_file',
  callId: '3f9a12c0d4e5',
  arguments: [
    ('path', 'src/render/pso_cache.cpp'),
    ('content', '(1,204 chars)'),
  ],
  refusedAt: '12:39',
  mode: 'manual',
  reason: 'nobody asked',
);

Future<void> _board(WidgetTester tester, String name, ThemeData theme,
    Size size, Widget child) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  await loadGoldenFonts(tester);
  await tester.pumpWidget(MaterialApp(
    debugShowCheckedModeBanner: false,
    theme: theme,
    home: Scaffold(
      body: RepaintBoundary(
        key: const ValueKey('board'),
        child: SizedBox.expand(
          child: ColoredBox(
            color: theme.extension<SonderTokens>()!.canvas,
            child: Padding(padding: const EdgeInsets.all(16), child: child),
          ),
        ),
      ),
    ),
  ));
  await tester.pumpAndSettle();
  await expectLater(find.byKey(const ValueKey('board')),
      matchesGoldenFile('primitives/$name.png'));
}

Widget _gap(Widget child) =>
    Padding(padding: const EdgeInsets.only(bottom: 10), child: child);

void main() {
  for (final (themeName, theme) in [
    ('dark', SonderTheme.dark),
    ('light', SonderTheme.light),
  ]) {
    testWidgets('notice kinds $themeName', skip: goldenSkip != null,
        (tester) async {
      await _board(
          tester,
          'notices_$themeName',
          theme,
          const Size(760, 900),
          Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            _gap(const WorkspaceNotice(
                kind: StatusKind.ok, word: 'done', title: 'Settings saved')),
            _gap(const WorkspaceNotice(
                kind: StatusKind.fail,
                title: "Can't reach mypc",
                detail: 'Connection refused at http://192.168.1.20:11435.')),
            _gap(WorkspaceNotice(
                kind: StatusKind.refused,
                title: '/write src/render/pso_cache.cpp',
                detail: 'File changes need a person to confirm, and manual '
                    'mode asks first.',
                actions: [
                  FilledButton(
                      onPressed: () {},
                      child: const Text('Approve this call once')),
                  OutlinedButton(
                      onPressed: () {}, child: const Text('Change mode…')),
                ])),
            _gap(WorkspaceNotice(
                kind: StatusKind.warn,
                word: 'refused',
                title: "mypc.local isn't allowed",
                hint: 'use the PC\'s IP, or add it to allowed_hosts on the PC',
                actions: [
                  OutlinedButton(onPressed: () {}, child: const Text('Retry')),
                  TextButton(onPressed: () {}, child: const Text('Settings')),
                ])),
            _gap(const WorkspaceNotice(
                kind: StatusKind.skipped,
                word: 'off',
                title: 'Autopilot is off')),
            _gap(const WorkspaceNotice(
                kind: StatusKind.unknown,
                title: 'The cancel request ended before the server replied')),
            _gap(const WorkspaceNotice(
                kind: StatusKind.note, title: 'Nothing to cancel')),
            _gap(const WorkspaceNotice(
                kind: StatusKind.running,
                title: 'work run wr-7c1e · 4m 12s of 30m budget')),
            _gap(ApprovalReceipt(
                tool: 'write_file',
                callId: '3f9a12c0',
                nonce: 'n_c41a',
                ttl: const Duration(minutes: 15),
                onRetry: () {},
                onRevoke: () {})),
          ]));
    });

    testWidgets('approval sheet $themeName', skip: goldenSkip != null,
        (tester) async {
      await _board(
          tester,
          'approval_sheet_$themeName',
          theme,
          const Size(390, 520),
          Material(
            color: theme.extension<SonderTokens>()!.panel,
            child: ApprovalSheet(
                request: _request, onApprove: (_) {}, onCancel: () {}),
          ));
    });

    testWidgets('approval sheet console fallback $themeName',
        skip: goldenSkip != null, (tester) async {
      await _board(
          tester,
          'approval_sheet_console_$themeName',
          theme,
          const Size(390, 560),
          Material(
            color: theme.extension<SonderTokens>()!.panel,
            child: ApprovalSheet(
                request: _request,
                consoleFallback: true,
                onApprove: (_) {},
                onCancel: () {}),
          ));
    });

    testWidgets('raise sheets $themeName', skip: goldenSkip != null,
        (tester) async {
      final panel = theme.extension<SonderTokens>()!.panel;
      await _board(
          tester,
          'raise_sheet_$themeName',
          theme,
          const Size(390, 600),
          Column(children: [
            Material(
                color: panel,
                child: RaiseModeSheet(
                    from: 'manual',
                    to: 'auto',
                    host: 'mypc',
                    onConfirm: () {},
                    onCancel: () {})),
            const SizedBox(height: 16),
            Material(
                color: panel,
                child: RaiseModeSheet(
                    from: 'manual',
                    to: 'acceptEdits',
                    host: 'mypc',
                    onConfirm: () {},
                    onCancel: () {})),
          ]));
    });

    for (final (layout, size) in [
      ('wide', const Size(760, 330)),
      ('narrow', const Size(390, 520)),
    ]) {
      testWidgets('status rows $layout $themeName', skip: goldenSkip != null,
          (tester) async {
        await _board(
            tester,
            'status_rows_${layout}_$themeName',
            theme,
            size,
            Column(children: [
              const StatusRow(
                  kind: StatusKind.ok,
                  label: 'Server',
                  value: 'mypc · 127.0.0.1:11435 · v2026.09.25 · up 3h 12m'),
              const StatusRow(
                  kind: StatusKind.ok,
                  label: 'Models',
                  value: 'sonder:latest (code) · pool 2 of 2 workers'),
              StatusRow(
                  kind: StatusKind.warn,
                  label: 'Approvals',
                  value: '1 call waiting',
                  trailing: OutlinedButton(
                      onPressed: () {}, child: const Text('Review'))),
              const StatusRow(
                  kind: StatusKind.running,
                  label: 'Work runs',
                  value: '1 running · wr-7c1e 4m'),
              const StatusRow(
                  kind: StatusKind.skipped,
                  word: 'off',
                  label: 'Autopilot',
                  value: 'off'),
              const StatusRow(
                  kind: StatusKind.fail,
                  label: 'Server',
                  value: "Can't reach mypc · as of 12:40",
                  stale: true),
            ]));
      });
    }

    testWidgets('markdown code $themeName', skip: goldenSkip != null,
        (tester) async {
      await _board(
          tester,
          'markdown_code_$themeName',
          theme,
          const Size(760, 420),
          const ConversationContent(
              fullWidthCode: true,
              content: 'The stall comes from the driver compiling the PSO '
                  'lazily on first bind; call `CreateGraphicsPipelineState` '
                  'early. See [the notes](https://example.invalid/pso).\n\n'
                  '```cpp\n'
                  'ID3D12PipelineState* pso = nullptr;\n'
                  'HRESULT hr = device->CreateGraphicsPipelineState(&desc, IID_PPV_ARGS(&pso)); // a deliberately long line that scrolls\n'
                  'cache.Store(desc.Hash(), pso);\n'
                  '```\n\n'
                  '> Warm the cache at load time, not on first draw.\n'));
    });

    testWidgets('glyphs and status lines $themeName', skip: goldenSkip != null,
        (tester) async {
      final tokens = theme.extension<SonderTokens>()!;
      final glyphs = [
        for (final kind in StatusKind.values) kind.glyph,
        '❯',
        '▸',
        '▾',
        '→',
        '…',
        '─',
        '●',
        '○',
        '⋯',
        '—',
        '↑',
        '↓',
      ].join(' ');
      await _board(
          tester,
          'glyphs_$themeName',
          theme,
          const Size(760, 330),
          Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(glyphs, style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(glyphs, style: tokens.mono(16)),
            const SizedBox(height: 12),
            Wrap(spacing: 16, runSpacing: 8, children: [
              for (final kind in StatusKind.values) StatusMark(kind),
            ]),
            const SizedBox(height: 12),
            Text(
                statusLine(
                    const StatusState(
                        model: 'sonder:latest',
                        ctxUsed: 2100,
                        ctxLimit: 8200,
                        pendingApprovals: 1),
                    100),
                style: tokens.mono(13, color: tokens.muted)),
            Text(
                liveLine(
                    const LiveState(
                        phase: 'reading files',
                        elapsedS: 23,
                        model: 'sonder:latest'),
                    100),
                style: tokens.mono(13, color: tokens.accentText)),
            Text(
                footerLine(
                    const FooterState(
                        elapsedMs: 61200,
                        modelCalls: 2,
                        tokensIn: 2600,
                        tokensOut: 143),
                    100),
                style: tokens.mono(13, color: tokens.muted)),
            const SizedBox(height: 8),
            Wrap(spacing: 16, children: [
              for (final mode in permissionModes)
                Text(mode,
                    style: tokens.mono(13,
                        color: modeStyle(mode).color(tokens),
                        weight: modeStyle(mode).strong
                            ? FontWeight.w600
                            : FontWeight.w400)),
            ]),
          ]));
    });
  }
}
