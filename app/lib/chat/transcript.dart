import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter/services.dart';

import '../api.dart' show WorkRun;
import '../models.dart';
import '../theme.dart';
import '../workspace_ui.dart' show conversationWidth;
import 'backend.dart';
import 'classify.dart';
import 'controller.dart';
import 'lines.dart';
import 'live_line.dart';
import 'markdown.dart';
import 'notice.dart';
import 'refusal.dart';
import 'work_run_card.dart';

/// Counters for performance tests (P2-17). Debug-only bookkeeping.
class TranscriptDebug {
  static int turnBuilds = 0;
  static int parses = 0;
  static void reset() {
    turnBuilds = 0;
    parses = 0;
  }
}

/// Speak [message] to screen readers (P2-12): "Sonder replied",
/// "Request failed: …".
void announceToScreenReader(BuildContext context, String message) {
  final view = View.maybeOf(context);
  if (view == null) return;
  SemanticsService.sendAnnouncement(view, message, TextDirection.ltr);
}

/// Everything a transcript row can do, supplied by the shell.
class TranscriptActions {
  final VoidCallback onStop;
  final ValueChanged<String> onFeedback;
  final ValueChanged<int> onRetry;
  final VoidCallback onChangeMode;
  final Future<ApprovalOutcome> Function(String callId, Duration ttl)?
      onApprove;
  final Future<WorkRun> Function(String id) fetchWorkRun;
  final Future<WorkRun> Function(String id) cancelWorkRun;
  final Future<List<WorkRun>> Function() listWorkRuns;
  final void Function(int entryId, WorkRun run) onWorkRunResolved;

  const TranscriptActions({
    required this.onStop,
    required this.onFeedback,
    required this.onRetry,
    required this.onChangeMode,
    required this.onApprove,
    required this.fetchWorkRun,
    required this.cancelWorkRun,
    required this.listWorkRuns,
    required this.onWorkRunResolved,
  });
}

/// The conversation: one row per entry, keyed by the entry's stable id so a
/// streamed or resolved reply keeps its row state.
class Transcript extends StatelessWidget {
  final List<ChatEntry> entries;
  final ScrollController scroll;
  final ValueListenable<LiveTurn?> live;
  final TranscriptActions actions;
  final Widget? header;

  const Transcript({
    super.key,
    required this.entries,
    required this.scroll,
    required this.live,
    required this.actions,
    this.header,
  });

  @override
  Widget build(BuildContext context) {
    final offset = header == null ? 0 : 1;
    return ListView.builder(
      key: const Key('chat-transcript'),
      controller: scroll,
      padding: const EdgeInsets.fromLTRB(16, 24, 16, 16),
      itemCount: entries.length + offset,
      findChildIndexCallback: (key) {
        if (key is! ValueKey<int>) return null;
        final i = entries.indexWhere((e) => e.id == key.value);
        return i < 0 ? null : i + offset;
      },
      itemBuilder: (_, i) {
        if (i < offset) return header!;
        final entry = entries[i - offset];
        return Center(
          key: ValueKey<int>(entry.id),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: conversationWidth),
            child: TranscriptTurn(entry: entry, live: live, actions: actions),
          ),
        );
      },
    );
  }
}

/// Scroll to the end only when the reader is already near it, unless
/// [force] (the reader just sent something). A reader scrolled up to an
/// older answer is not yanked away by a streaming reply (P2-17).
void scrollTranscriptToEnd(ScrollController scroll, {bool force = false}) {
  WidgetsBinding.instance.addPostFrameCallback((_) {
    if (!scroll.hasClients) return;
    final position = scroll.position;
    final near = position.maxScrollExtent - position.pixels < 160;
    if (!force && !near) return;
    scroll.animateTo(position.maxScrollExtent,
        duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
  });
}

/// The parsed form of an assistant message, computed once per message
/// instance (messages are immutable; a changed reply is a new instance).
class ParsedAnswer {
  static const _activityMarker = '=== ACTIVITY (observable work) ===';
  static const _endReportMarker = '=== END REPORT ===';
  static final Expando<ParsedAnswer> _cache = Expando<ParsedAnswer>();

  final String answer;
  final String activityRaw;
  final List<ToolCallRow> toolCalls;
  final Map<String, int> stats;
  final ReplyKind kind;
  final RefusalInfo? refusal;
  final WorkRunRef? workRun;

  const ParsedAnswer._({
    required this.answer,
    required this.activityRaw,
    required this.toolCalls,
    required this.stats,
    required this.kind,
    this.refusal,
    this.workRun,
  });

  static ParsedAnswer of(ChatMessage message) =>
      _cache[message] ??= _parse(message);

  static ParsedAnswer _parse(ChatMessage message) {
    TranscriptDebug.parses++;
    final content = message.content;
    final markerIndex = content.indexOf(_activityMarker);
    final beforeActivity =
        (markerIndex < 0 ? content : content.substring(0, markerIndex))
            .trimRight();
    final endReportIndex = beforeActivity.indexOf(_endReportMarker);
    final answer = (endReportIndex < 0
            ? beforeActivity
            : beforeActivity.substring(0, endReportIndex))
        .trimRight();
    final activityRaw =
        markerIndex < 0 ? '' : content.substring(markerIndex).trim();
    final parsed = _parseActivityBlock(activityRaw);
    final kind = classifyReply(message);
    return ParsedAnswer._(
      answer: answer,
      activityRaw: activityRaw,
      toolCalls: parsed.$1,
      stats: parsed.$2,
      kind: kind,
      refusal: kind == ReplyKind.refused ? refusalOf(message) : null,
      workRun: kind == ReplyKind.workRun ? workRunOf(message) : null,
    );
  }

  static (List<ToolCallRow>, Map<String, int>) _parseActivityBlock(String raw) {
    if (raw.isEmpty) return (const [], const {});
    final actions = <ToolCallRow>[];
    final timed = <ToolCallRow>[];
    final stats = <String, int>{};
    for (final line in raw.split('\n')) {
      final t = line.trim();
      if (t.startsWith('model calls:')) {
        final parts = t.split(RegExp(r'\s+'));
        if (parts.length >= 9) {
          stats['model_calls'] = int.tryParse(parts[2]) ?? 0;
          stats['tool_calls'] = int.tryParse(parts[5]) ?? 0;
          final tokenParts = parts[8];
          if (tokenParts.contains('/')) {
            final tp = tokenParts.split('/');
            stats['tokens_in'] = int.tryParse(tp[0]) ?? 0;
            stats['tokens_out'] = int.tryParse(tp[1]) ?? 0;
          }
        }
      } else if (t.startsWith('files:')) {
        final parts = t.split(RegExp(r'\s+'));
        if (parts.length >= 4) {
          stats['file_creates'] =
              int.tryParse(parts[1].replaceAll('+', '')) ?? 0;
          stats['file_edits'] = int.tryParse(parts[2].replaceAll('~', '')) ?? 0;
          stats['file_deletes'] =
              int.tryParse(parts[3].replaceAll('-', '')) ?? 0;
        }
      } else if (t.startsWith('• ') || t.startsWith('× ')) {
        actions.add(ToolCallRow(t.substring(2).trim(), ok: t.startsWith('•')));
      } else if (t.startsWith('+') && t.contains('ms ')) {
        final parts = t.split(RegExp(r'\s+'));
        if (parts.length >= 3 && parts[1] == 'tool_call') {
          timed.add(ToolCallRow(parts.sublist(2).join(' '),
              elapsedMs: int.tryParse(
                  parts[0].replaceAll('+', '').replaceAll('ms', ''))));
        }
      }
    }
    if (actions.isEmpty) return (timed, stats);
    return (
      [
        for (var i = 0; i < actions.length; i++)
          ToolCallRow(actions[i].title,
              ok: actions[i].ok,
              elapsedMs: i < timed.length ? timed[i].elapsedMs : null),
      ],
      stats
    );
  }
}

class ToolCallRow {
  final String title;
  final bool ok;
  final int? elapsedMs;
  const ToolCallRow(this.title, {this.ok = true, this.elapsedMs});
}

/// The answer footer (P2-7): `done 61.2s · 2 model calls · 2.6k→143 tok`.
/// Null when there is nothing honest to say (an old message with no
/// receipt and no measured time).
String? footerFor(ChatEntry entry, ParsedAnswer? parsed) {
  final m = entry.message.responseMetadata;
  final stats = parsed?.stats ?? const <String, int>{};
  final elapsed = (m?.elapsedMs ?? 0) > 0 ? m!.elapsedMs : entry.elapsedMs;
  if (entry.message.error) {
    if (elapsed == null) return null;
    return footerLine(FooterState(elapsedMs: elapsed, ok: false));
  }
  if (elapsed == null && m == null && stats.isEmpty) return null;
  int? pick(int? a, String key) => (a != null && a > 0) ? a : (stats[key] ?? a);
  return footerLine(FooterState(
    elapsedMs: elapsed ?? 0,
    modelCalls: pick(m?.modelCalls, 'model_calls'),
    toolCalls: pick(m?.toolCalls, 'tool_calls'),
    tokensIn: pick(m?.promptTokens, 'tokens_in'),
    tokensOut: pick(m?.completionTokens, 'tokens_out'),
  ));
}

/// One turn: a gutter glyph (❯ you, ◈ Sonder, ⊘/✗ refusal or failure) and
/// the content in the reading column.
class TranscriptTurn extends StatelessWidget {
  final ChatEntry entry;
  final ValueListenable<LiveTurn?> live;
  final TranscriptActions actions;

  const TranscriptTurn({
    super.key,
    required this.entry,
    required this.live,
    required this.actions,
  });

  @override
  Widget build(BuildContext context) {
    TranscriptDebug.turnBuilds++;
    final tokens = SonderTokens.of(context);
    final message = entry.message;
    final isUser = message.role == Role.user;
    final parsed = isUser || message.pending ? null : ParsedAnswer.of(message);
    final kind = parsed?.kind ?? ReplyKind.answer;
    final glyph = isUser
        ? '❯'
        : switch (kind) {
            ReplyKind.error => '✗',
            ReplyKind.refused => '⊘',
            _ => '◈',
          };
    final glyphColor =
        !isUser && (kind == ReplyKind.error || kind == ReplyKind.refused)
            ? tokens.danger
            : tokens.accent;
    final speaker = isUser ? 'You' : 'Sonder Runtime';

    Widget content;
    if (isUser) {
      content = SelectableText(message.content,
          style: Theme.of(context).textTheme.bodyMedium);
    } else if (message.pending) {
      content = Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (message.content.isNotEmpty) ...[
            ChatMarkdown(
                key: const Key('streaming-text'), content: message.content),
            const SizedBox(height: 10),
          ],
          LiveLineView(live: live, onStop: actions.onStop),
        ],
      );
    } else {
      content = switch (kind) {
        ReplyKind.error => _errorContent(context, tokens),
        ReplyKind.refused => _refusalContent(context, parsed!),
        ReplyKind.workRun => WorkRunCard(
            run: parsed!.workRun!,
            fetch: actions.fetchWorkRun,
            cancel: actions.cancelWorkRun,
            onResolved: (run) => actions.onWorkRunResolved(entry.id, run),
          ),
        ReplyKind.answer => _answerContent(context, tokens, parsed!),
      };
    }

    return Semantics(
      label: speaker,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 24,
              child: Padding(
                padding: const EdgeInsets.only(top: 2),
                child: ExcludeSemantics(
                  child: Text(glyph,
                      textAlign: TextAlign.center,
                      style: tokens.mono(14,
                          color: glyphColor, weight: FontWeight.w600)),
                ),
              ),
            ),
            const SizedBox(width: 14),
            Expanded(child: content),
          ],
        ),
      ),
    );
  }

  Widget _errorContent(BuildContext context, SonderTokens tokens) {
    final message = entry.message;
    final lines = message.content.trim().split('\n');
    final title = lines.first.trim();
    final detail = lines.skip(1).join('\n').trim();
    final footer = footerFor(entry, null);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        // Plain text, never Markdown: error text carries server URLs that
        // must not become tappable links.
        ChatNotice(
          key: const Key('error-notice'),
          kind: ChatStatusKind.fail,
          liveRegion: true,
          title: title,
          detail: detail,
          actions: [
            if (isWorkCapacityMessage(message))
              FilledButton.tonal(
                key: const Key('error-running-work'),
                onPressed: () => showRunningWork(
                  context,
                  list: actions.listWorkRuns,
                  cancel: actions.cancelWorkRun,
                ),
                child: const Text('Show running work'),
              ),
            if (entry.retryable)
              OutlinedButton(
                key: const Key('error-retry'),
                onPressed: () => actions.onRetry(entry.id),
                child: const Text('Retry'),
              ),
            _FeedbackAction(
              icon: Icons.copy_all_outlined,
              label: 'Copy error',
              text: 'copy',
              onTap: () =>
                  Clipboard.setData(ClipboardData(text: message.content)),
            ),
          ],
        ),
        if (footer != null) ...[
          const SizedBox(height: 6),
          Text(footer, style: tokens.mono(11, color: tokens.muted)),
        ],
        if (message.diagnostic.trim().isNotEmpty) ...[
          const SizedBox(height: 6),
          CollapsedDetail(
              icon: Icons.error_outline,
              label: 'Error details',
              body: message.diagnostic.trim()),
        ],
      ],
    );
  }

  Widget _refusalContent(BuildContext context, ParsedAnswer parsed) {
    final m = entry.message.responseMetadata;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        RefusalNotice(
          refusal: parsed.refusal!,
          onApprove: actions.onApprove,
          onChangeMode: actions.onChangeMode,
          onRetry: () => actions.onRetry(entry.id),
        ),
        if (m != null && m.diagnosticText.isNotEmpty) ...[
          const SizedBox(height: 6),
          CollapsedDetail(
              icon: Icons.receipt_long_outlined,
              label: 'Response details',
              body: m.diagnosticText),
        ],
      ],
    );
  }

  Widget _answerContent(
      BuildContext context, SonderTokens tokens, ParsedAnswer parsed) {
    final message = entry.message;
    final m = message.responseMetadata;
    final footer = footerFor(entry, parsed);
    final showActions = message.content.isNotEmpty;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        ChatMarkdown(content: parsed.answer),
        if (message.reasoning.trim().isNotEmpty) ...[
          const SizedBox(height: 8),
          CollapsedDetail(
              icon: Icons.psychology_outlined,
              label: 'Model reasoning',
              body: message.reasoning.trim()),
        ],
        if (parsed.toolCalls.isNotEmpty) ...[
          const SizedBox(height: 8),
          _ToolCallsDetail(calls: parsed.toolCalls),
        ] else if (parsed.activityRaw.isNotEmpty) ...[
          const SizedBox(height: 8),
          CollapsedDetail(
              icon: Icons.monitor_heart_outlined,
              label: 'Activity evidence',
              body: parsed.activityRaw),
        ],
        if (m != null && m.diagnosticText.isNotEmpty) ...[
          const SizedBox(height: 8),
          CollapsedDetail(
            icon: Icons.receipt_long_outlined,
            label: m.cache == 'hit'
                ? 'Response details - cached replay'
                : 'Response details',
            body: m.diagnosticText,
          ),
        ],
        if (footer != null || showActions)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Wrap(
              key: const Key('answer-footer'),
              spacing: 6,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                if (footer != null)
                  Padding(
                    padding: const EdgeInsets.only(right: 6),
                    child: Text(footer,
                        key: const Key('answer-footer-text'),
                        style: tokens.mono(11, color: tokens.muted)),
                  ),
                if (showActions) ...[
                  _FeedbackAction(
                    icon: Icons.copy_all_outlined,
                    label: 'Copy response',
                    text: 'copy',
                    onTap: () {
                      Clipboard.setData(ClipboardData(text: message.content));
                      actions.onFeedback('/copied');
                    },
                  ),
                  // Quality feedback trains the learning loop on the last
                  // answer. Refusals and errors are not answers and never
                  // get these.
                  _FeedbackAction(
                    icon: Icons.check_circle_outline,
                    label: 'Mark response useful',
                    text: 'useful',
                    onTap: () => actions.onFeedback('/accept'),
                  ),
                  _FeedbackAction(
                    icon: Icons.edit_outlined,
                    label: 'Mark response edited',
                    text: 'edited',
                    onTap: () => actions.onFeedback('/edited'),
                  ),
                ],
              ],
            ),
          ),
      ],
    );
  }
}

/// A feedback control with a full-size touch target and an explicit label.
class _FeedbackAction extends StatelessWidget {
  final IconData icon;
  final String label;
  final String text;
  final VoidCallback onTap;

  const _FeedbackAction({
    required this.icon,
    required this.label,
    required this.text,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Semantics(
      button: true,
      label: label,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 48, minWidth: 48),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 6),
            child: ExcludeSemantics(
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(icon, size: 14, color: tokens.muted),
                  const SizedBox(width: 5),
                  Text(text, style: tokens.mono(11, color: tokens.muted)),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _ToolCallsDetail extends StatelessWidget {
  final List<ToolCallRow> calls;
  const _ToolCallsDetail({required this.calls});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final n = calls.length;
    return CollapsedDetail(
      icon: Icons.build_outlined,
      label: '$n tool call${n == 1 ? '' : 's'}',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final call in calls)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 2),
              child: Row(children: [
                SizedBox(
                  width: 56,
                  child: Text(call.ok ? '▸ ran' : '⊘ refused',
                      style: tokens.mono(11,
                          color: call.ok ? tokens.muted : tokens.danger)),
                ),
                Expanded(
                  child: Text(call.title,
                      overflow: TextOverflow.ellipsis,
                      style: tokens.mono(12, color: tokens.text2)),
                ),
                if (call.elapsedMs != null)
                  Text(durationLabel(call.elapsedMs!),
                      style: tokens.mono(11, color: tokens.muted)),
              ]),
            ),
        ],
      ),
    );
  }
}

/// A collapsed, monospaced detail block under an answer.
class CollapsedDetail extends StatelessWidget {
  final IconData icon;
  final String label;
  final String body;
  final Widget? child;

  const CollapsedDetail({
    super.key,
    required this.icon,
    required this.label,
    this.body = '',
    this.child,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Material(
      color: Colors.transparent,
      child: Theme(
        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
        child: ExpansionTile(
          dense: true,
          visualDensity: VisualDensity.compact,
          tilePadding: EdgeInsets.zero,
          childrenPadding: EdgeInsets.zero,
          leading: Icon(icon, size: 16, color: tokens.muted),
          title: Text(label, style: tokens.mono(11, color: tokens.text2)),
          children: [
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: tokens.panel,
                borderRadius: BorderRadius.circular(SonderRadius.row),
                border: Border.all(color: tokens.hairline),
              ),
              child: child ??
                  SelectableText(body,
                      style: tokens.mono(11, color: tokens.text2)),
            ),
          ],
        ),
      ),
    );
  }
}
