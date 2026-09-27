import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import '../ui/status_line.dart';
import '../ui/status_vocab.dart';
import 'live_line.dart' show monoCellWidth;

/// Build the REPL status state from what chat knows.
StatusState statusStateFor({
  required SystemInfo? info,
  required PermissionMode? mode,
  required String model,
  required String tier,
  required String project,
}) {
  final ctx = info?.context;
  return StatusState(
    mode: mode?.mode ?? 'unknown',
    tier: tier,
    model: model == 'sonder' ? 'sonder' : model,
    ctxUsed: ctx?.estimatedTokens,
    ctxLimit: ctx == null
        ? null
        : (ctx.contextLimit > 0 ? ctx.contextLimit : ctx.nativeContextLimit),
    agents: info?.agents?.activeAgents ?? 0,
    project: project,
    elevated: mode?.elevated ?? false,
    elevatedReason: mode?.elevationReason ?? '',
  );
}

/// The one-row status line under the composer (P2-6):
/// `code · sonder:latest · manual · ctx 2.1k/8.2k · 2 agents`.
///
/// Mode comes first after the tier and is never dropped; zero counts are
/// hidden; fields leave in the REPL's order as the width shrinks. Per-turn
/// metrics live in the answer footer, not here. It listens to [info] itself
/// so a status poll rebuilds this row and nothing else.
class ChatStatusStrip extends StatelessWidget {
  final ValueListenable<SystemInfo?> info;
  final PermissionMode? mode;
  final String model;
  final String tier;
  final String project;

  const ChatStatusStrip({
    super.key,
    required this.info,
    required this.mode,
    required this.model,
    required this.tier,
    required this.project,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return ValueListenableBuilder<SystemInfo?>(
      valueListenable: info,
      builder: (context, value, _) {
        final state = statusStateFor(
            info: value,
            mode: mode,
            model: model,
            tier: tier,
            project: project);
        final base = tokens.mono(11, color: tokens.muted);
        return Container(
          key: const Key('chat-status-strip'),
          width: double.infinity,
          constraints: const BoxConstraints(minHeight: 28),
          decoration: BoxDecoration(
            color: tokens.panel,
            border: Border(top: BorderSide(color: tokens.hairline)),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 5),
          child: SafeArea(
            top: false,
            child: LayoutBuilder(builder: (context, constraints) {
              final cell = monoCellWidth(context, base);
              final cols =
                  cell <= 0 ? 80 : (constraints.maxWidth / cell).floor();
              final fields = statusSegments(state, cols);
              final line = statusLine(state, cols);
              return Semantics(
                label: 'Status: $line',
                child: ExcludeSemantics(
                  child: Text.rich(
                    TextSpan(children: _spans(fields, tokens, base)),
                    key: const Key('chat-status-line'),
                    maxLines: 1,
                    softWrap: false,
                    overflow: TextOverflow.clip,
                  ),
                ),
              );
            }),
          ),
        );
      },
    );
  }

  static List<InlineSpan> _spans(
      List<StatusSegment> fields, SonderTokens tokens, TextStyle base) {
    final spans = <InlineSpan>[];
    for (var i = 0; i < fields.length; i++) {
      final f = fields[i];
      if (i > 0) {
        spans.add(TextSpan(text: ' ${statusGlyphs['sep']} ', style: base));
      }
      if (f.field == StatusField.elevated) {
        // Lane B keeps "manual ELEVATED (reason)" as one segment, like the
        // REPL; the mode word keeps its role, the badge is reversed danger.
        final cut = f.text.indexOf(' ELEVATED');
        final word = cut < 0 ? f.text : f.text.substring(0, cut);
        spans.add(TextSpan(text: word, style: _mode(word, tokens, base)));
        if (cut >= 0) {
          spans.add(TextSpan(text: ' ', style: base));
          spans.add(TextSpan(
              text: f.text.substring(cut + 1),
              style: base.copyWith(
                  color: tokens.canvas,
                  backgroundColor:
                      modeStyle(word, elevated: true).color(tokens),
                  fontWeight: FontWeight.w600)));
        }
        continue;
      }
      spans.add(TextSpan(text: f.text, style: _style(f, tokens, base)));
    }
    return spans;
  }

  /// The mode word in its role (lane B's [modeStyle], style.py mode_roles).
  static TextStyle _mode(String word, SonderTokens tokens, TextStyle base) {
    final style = modeStyle(word);
    return base.copyWith(
        color: style.color(tokens),
        fontWeight: style.strong ? FontWeight.w700 : FontWeight.w500);
  }

  /// Only the tier (info) and the mode word (its mode role) carry colour.
  static TextStyle _style(
      StatusSegment f, SonderTokens tokens, TextStyle base) {
    switch (f.field) {
      case StatusField.tier:
        return base.copyWith(color: tokens.info);
      case StatusField.mode:
      case StatusField.elevated:
        return _mode(f.text, tokens, base);
      case StatusField.model:
      case StatusField.ctx:
      case StatusField.agents:
      case StatusField.project:
        return base;
    }
  }
}
