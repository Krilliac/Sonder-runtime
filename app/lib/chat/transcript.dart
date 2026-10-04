import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter/services.dart';

import '../api.dart' show WorkRun;
import '../models.dart';
import '../theme.dart';
import '../ui/kit.dart' show QuietAction, SonderReveal, showSonderToast;
import '../ui/status_line.dart';
import '../ui/status_vocab.dart' show statusGlyphs;
import '../workspace_ui.dart'
    show ConversationContent, StatusKind, WorkspaceNotice, conversationWidth;
import 'backend.dart';
import 'classify.dart';
import 'controller.dart';
import 'live_line.dart';
import 'refusal.dart';
import 'route_overflow_chip.dart';
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
  final Future<PendingCallLookup> Function(String callId)? onLookupApproval;
  final ApproveCallback? onApprove;
  final Future<WorkRun> Function(String id) fetchWorkRun;
  final Future<WorkRun> Function(String id) cancelWorkRun;
  final Future<List<WorkRun>> Function() listWorkRuns;
  final void Function(int entryId, WorkRun run) onWorkRunResolved;

  final ValueChanged<String>? onOpenAgentLane;
  final ValueChanged<String>? onSendCommand;

  const TranscriptActions({
    required this.onStop,
    required this.onFeedback,
    required this.onRetry,
    required this.onChangeMode,
    this.onLookupApproval,
    required this.onApprove,
    required this.fetchWorkRun,
    required this.cancelWorkRun,
    required this.listWorkRuns,
    required this.onWorkRunResolved,
    this.onOpenAgentLane,
    this.onSendCommand,
  });
}

/// The conversation: one row per entry, keyed by the entry's stable id so a
/// streamed or resolved reply keeps its row state.
///
/// A message that arrives while the transcript is open enters with a short
/// fade and rise, once; rows built again later (scrolling, a new delta, a
/// thread switch) never re-animate, and reduced motion shows them at once.
class Transcript extends StatefulWidget {
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
  State<Transcript> createState() => _TranscriptState();
}

class _TranscriptState extends State<Transcript> {
  int? _firstId;
  int _maxSeen = 0;

  /// Entries that should enter with motion when their row is first built.
  final Set<int> _entering = <int>{};

  @override
  void initState() {
    super.initState();
    _track(widget.entries, opening: true);
  }

  @override
  void didUpdateWidget(covariant Transcript oldWidget) {
    super.didUpdateWidget(oldWidget);
    _track(widget.entries);
  }

  void _track(List<ChatEntry> entries, {bool opening = false}) {
    if (entries.isEmpty) {
      _firstId = null;
      _maxSeen = 0;
      _entering.clear();
      return;
    }
    var maxId = 0;
    for (final e in entries) {
      if (e.id > maxId) maxId = e.id;
    }
    final first = entries.first.id;
    if (opening) {
      // A transcript that opens on a turn just sent (a new chat's first
      // message) lets that turn enter; one that opens on history does not.
      if (entries.last.message.pending) {
        _entering.add(entries.last.id);
        if (entries.length >= 2 &&
            entries[entries.length - 2].message.role == Role.user) {
          _entering.add(entries[entries.length - 2].id);
        }
      }
    } else if (first == _firstId) {
      for (final e in entries) {
        if (e.id > _maxSeen) _entering.add(e.id);
      }
    } else {
      // Another thread, or rows rebuilt from storage: nothing is new.
      _entering.clear();
    }
    _firstId = first;
    _maxSeen = maxId;
  }

  @override
  Widget build(BuildContext context) {
    final entries = widget.entries;
    final header = widget.header;
    final offset = header == null ? 0 : 1;
    return ListView.builder(
      key: const Key('chat-transcript'),
      controller: widget.scroll,
      padding: const EdgeInsets.fromLTRB(
          SonderSpace.lg, SonderSpace.xxl, SonderSpace.lg, SonderSpace.lg),
      itemCount: entries.length + offset,
      findChildIndexCallback: (key) {
        if (key is! ValueKey<int>) return null;
        final i = entries.indexWhere((e) => e.id == key.value);
        return i < 0 ? null : i + offset;
      },
      itemBuilder: (_, i) {
        if (i < offset) return header!;
        final index = i - offset;
        final entry = entries[index];
        return Center(
          key: ValueKey<int>(entry.id),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: conversationWidth),
            child: _EnterOnce(
              animate: _entering.remove(entry.id),
              child: TranscriptTurn(
                entry: entry,
                live: widget.live,
                actions: widget.actions,
                isLast: index == entries.length - 1,
              ),
            ),
          ),
        );
      },
    );
  }
}

/// Fades and rises its child in when first built with [animate]; later
/// builds never replay it. Reduced motion shows the child at once.
class _EnterOnce extends StatelessWidget {
  final bool animate;
  final Widget child;
  const _EnterOnce({required this.animate, required this.child});

  @override
  Widget build(BuildContext context) {
    return TweenAnimationBuilder<double>(
      tween: Tween<double>(begin: animate ? 0 : 1, end: 1),
      duration: SonderMotion.of(context, SonderMotion.medium),
      curve: SonderMotion.enter,
      builder: (context, t, child) => Opacity(
        opacity: t,
        child: Transform.translate(
          offset: Offset(0, (1 - t) * SonderSpace.sm),
          child: child,
        ),
      ),
      child: child,
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
        duration: SonderMotion.medium, curve: SonderMotion.standard);
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

/// `ran 3 tools · 2.1s · 1 refused`: the one-line summary of a turn's tool
/// calls. The time is the sum of the calls that reported one.
String toolCallsSummary(List<ToolCallRow> calls) {
  final ran = calls.where((c) => c.ok).length;
  final refused = calls.length - ran;
  final timed = calls.where((c) => c.ok && c.elapsedMs != null);
  final parts = <String>[
    if (ran > 0) 'ran $ran tool${ran == 1 ? '' : 's'}',
    if (ran > 0 && timed.isNotEmpty)
      durationLabel(timed.fold<int>(0, (sum, c) => sum + c.elapsedMs!)),
    if (refused > 0) '$refused refused',
  ];
  return parts.join(' ${statusGlyphs['sep']} ');
}

/// The footer wraps in the reading column, so it is not cut to a width.
const _noLimit = 1 << 20;

/// The answer footer (P2-7): `done 61.2s · 2 model calls · 2.6k→143 tok`.
/// Null when there is nothing honest to say (an old message with no
/// receipt and no measured time).
String? footerFor(ChatEntry entry, ParsedAnswer? parsed) {
  final state = footerStateFor(entry, parsed);
  return state == null ? null : footerLine(state, _noLimit);
}

/// The metrics behind [footerFor], for a footer fitted to a width.
FooterState? footerStateFor(ChatEntry entry, ParsedAnswer? parsed) {
  final m = entry.message.responseMetadata;
  final stats = parsed?.stats ?? const <String, int>{};
  final elapsed = (m?.elapsedMs ?? 0) > 0 ? m!.elapsedMs : entry.elapsedMs;
  if (entry.message.error) {
    if (elapsed == null) return null;
    return FooterState(elapsedMs: elapsed, ok: false);
  }
  if (elapsed == null && m == null && stats.isEmpty) return null;
  int? pick(int? a, String key) => (a != null && a > 0) ? a : (stats[key] ?? a);
  return FooterState(
    elapsedMs: elapsed ?? 0,
    modelCalls: pick(m?.modelCalls, 'model_calls'),
    toolCalls: pick(m?.toolCalls, 'tool_calls'),
    tokensIn: pick(m?.promptTokens, 'tokens_in'),
    tokensOut: pick(m?.completionTokens, 'tokens_out'),
  );
}

/// The answer footer on one line: the REPL's footer fitted to the width it
/// gets, so a phone drops token counts instead of wrapping mid-line.
class _FooterMetrics extends StatelessWidget {
  final FooterState state;
  const _FooterMetrics({required this.state});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final style = tokens.mono(11, color: tokens.muted);
    return LayoutBuilder(builder: (context, constraints) {
      final cell = monoCellWidth(context, style);
      final cols = cell <= 0 || !constraints.hasBoundedWidth
          ? _noLimit
          : (constraints.maxWidth / cell).floor();
      return Text(footerLine(state, cols),
          key: const Key('answer-footer-text'),
          maxLines: 1,
          softWrap: false,
          overflow: TextOverflow.clip,
          style: style);
    });
  }
}

/// Feedback already given for an answer during this session, keyed by the
/// (immutable) message, so a row rebuilt after scrolling still shows it.
final Expando<Set<String>> _feedbackGiven = Expando<Set<String>>();

/// Whether this device has no hover: action rows then stay visible instead
/// of waiting for a pointer.
bool _touchFirst(BuildContext context) => switch (Theme.of(context).platform) {
      TargetPlatform.android ||
      TargetPlatform.iOS ||
      TargetPlatform.fuchsia =>
        true,
      _ => false,
    };

/// One turn: a gutter glyph (❯ you, ◈ Sonder, ⊘/✗ refusal or failure) and
/// the content in the reading column. Your messages sit on a faint surface
/// so a long conversation scans by turn; answers carry their details and
/// actions underneath, revealed by hover or focus on pointer devices (the
/// newest answer always shows them).
class TranscriptTurn extends StatefulWidget {
  final ChatEntry entry;
  final ValueListenable<LiveTurn?> live;
  final TranscriptActions actions;

  /// The newest row: its actions stay visible, and Retry applies to it.
  final bool isLast;

  const TranscriptTurn({
    super.key,
    required this.entry,
    required this.live,
    required this.actions,
    this.isLast = false,
  });

  @override
  State<TranscriptTurn> createState() => _TranscriptTurnState();
}

class _TranscriptTurnState extends State<TranscriptTurn> {
  bool _hover = false;
  bool _focusWithin = false;

  ChatEntry get entry => widget.entry;
  TranscriptActions get actions => widget.actions;

  bool _revealed(BuildContext context) =>
      widget.isLast || _hover || _focusWithin || _touchFirst(context);

  @override
  Widget build(BuildContext context) {
    TranscriptDebug.turnBuilds++;
    final tokens = SonderTokens.of(context);
    final message = entry.message;
    final isUser = message.role == Role.user;
    final parsed = isUser || message.pending ? null : ParsedAnswer.of(message);
    final kind = parsed?.kind ?? ReplyKind.answer;
    final speaker = isUser ? 'You' : 'Sonder Runtime';

    final Widget body;
    if (isUser) {
      body = _userRow(context, tokens);
    } else if (message.pending) {
      body = _pendingContent();
    } else {
      final glyph = switch (kind) {
        ReplyKind.error => '✗',
        ReplyKind.refused => '⊘',
        _ => '◈',
      };
      final glyphColor = kind == ReplyKind.error || kind == ReplyKind.refused
          ? tokens.danger
          : tokens.accent;
      final content = switch (kind) {
        ReplyKind.error => _errorContent(context, tokens),
        ReplyKind.refused => _refusalContent(context, parsed!),
        ReplyKind.workRun => WorkRunCard(
            run: parsed!.workRun!,
            acknowledgement: message.content,
            fetch: actions.fetchWorkRun,
            cancel: actions.cancelWorkRun,
            onResolved: (run) => actions.onWorkRunResolved(entry.id, run),
          ),
        ReplyKind.answer => _answerContent(context, tokens, parsed!),
      };
      body = Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _Gutter(glyph: glyph, color: glyphColor),
          const SizedBox(width: transcriptGutterGap),
          Expanded(child: content),
        ],
      );
    }

    return MouseRegion(
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: Focus(
        canRequestFocus: false,
        skipTraversal: true,
        onFocusChange: (focused) => setState(() => _focusWithin = focused),
        child: Semantics(
          label: speaker,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: SonderSpace.sm),
            child: body,
          ),
        ),
      ),
    );
  }

  // -- You --------------------------------------------------------------

  Widget _userRow(BuildContext context, SonderTokens tokens) {
    final message = entry.message;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _Gutter(glyph: '❯', color: tokens.accent, top: SonderSpace.sm),
        const SizedBox(width: transcriptGutterGap),
        Flexible(
          child: Container(
            key: const Key('user-turn-surface'),
            padding: const EdgeInsets.symmetric(
                horizontal: SonderSpace.md, vertical: SonderSpace.sm),
            decoration: BoxDecoration(
              color: tokens.raised,
              borderRadius: BorderRadius.circular(SonderRadius.card),
            ),
            child: SelectableText(message.content,
                style: Theme.of(context).textTheme.bodyMedium),
          ),
        ),
        // On touch, long press selects the text; the button shows on
        // hover or keyboard focus.
        _Reveal(
          visible: _hover || _focusWithin,
          child: _CopyAction(
            key: const Key('user-turn-copy'),
            text: message.content,
            label: '',
            semanticLabel: 'Copy message',
            toast: 'Message copied',
            alignment: const Alignment(0, -0.3),
          ),
        ),
      ],
    );
  }

  // -- Live turn -----------------------------------------------------------

  Widget _pendingContent() {
    final message = entry.message;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (message.content.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(
                left: transcriptGutter + transcriptGutterGap,
                bottom: SonderSpace.sm),
            child: ConversationContent(
                key: const Key('streaming-text'),
                content: message.content,
                fullWidthCode: true),
          ),
        // Keyed so its stall clock survives the text appearing above it.
        LiveLineView(
          key: ValueKey<String>('live-${entry.id}'),
          live: widget.live,
          onStop: actions.onStop,
          outputLength: message.content.length,
        ),
      ],
    );
  }

  // -- Failures and refusals ----------------------------------------------

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
        WorkspaceNotice(
          framed: false,
          key: const Key('error-notice'),
          kind: StatusKind.fail,
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
            _CopyAction(
              key: const Key('error-copy'),
              text: message.content,
              label: 'Copy error',
              semanticLabel: 'Copy error',
              toast: 'Error copied',
            ),
          ],
        ),
        if (footer != null) ...[
          const SizedBox(height: SonderSpace.xs),
          Text(footer, style: tokens.mono(11, color: tokens.muted)),
        ],
        if (message.diagnostic.trim().isNotEmpty) ...[
          const SizedBox(height: SonderSpace.xs),
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
          onLookup: actions.onLookupApproval,
          onApprove: actions.onApprove,
          onChangeMode: actions.onChangeMode,
          onRetry: () => actions.onRetry(entry.id),
        ),
        if (m != null && m.diagnosticText.isNotEmpty) ...[
          const SizedBox(height: SonderSpace.xs),
          CollapsedDetail(
              icon: Icons.receipt_long_outlined,
              label: 'Response details',
              body: m.diagnosticText),
        ],
      ],
    );
  }

  // -- Answers ---------------------------------------------------------------

  Widget _answerContent(
      BuildContext context, SonderTokens tokens, ParsedAnswer parsed) {
    final message = entry.message;
    final m = message.responseMetadata;
    final footer = footerStateFor(entry, parsed);
    final showActions = message.content.isNotEmpty;
    final details = <_Detail>[
      if (message.reasoning.trim().isNotEmpty)
        _Detail(
          id: 'reasoning',
          icon: Icons.psychology_outlined,
          label: 'Model reasoning',
          body: message.reasoning.trim(),
        ),
      if (parsed.toolCalls.isNotEmpty)
        _Detail(
          id: 'tools',
          icon: Icons.build_outlined,
          label: toolCallsSummary(parsed.toolCalls),
          refused: parsed.toolCalls.where((c) => !c.ok).length,
          child: _ToolCallRows(calls: parsed.toolCalls),
        )
      else if (parsed.activityRaw.isNotEmpty)
        _Detail(
          id: 'activity',
          icon: Icons.monitor_heart_outlined,
          label: 'Activity evidence',
          body: parsed.activityRaw,
        ),
      if (m != null && m.diagnosticText.isNotEmpty)
        _Detail(
          id: 'details',
          icon: Icons.receipt_long_outlined,
          label: m.cache == 'hit'
              ? 'Response details - cached replay'
              : 'Response details',
          body: m.diagnosticText,
        ),
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (m?.overflow != null) ...[
          Align(
            alignment: Alignment.centerLeft,
            child: RouteOverflowChip(overflow: m!.overflow!),
          ),
          const SizedBox(height: SonderSpace.sm),
        ],
        if (m?.agentLane != null) ...[
          _AgentLaneAck(
            receipt: m!.agentLane!,
            onOpen: actions.onOpenAgentLane,
          ),
          const SizedBox(height: SonderSpace.md),
        ],
        if (m?.orchestration != null &&
            m!.orchestration!.choices.isNotEmpty) ...[
          _OrchestrationChoices(
            receipt: m.orchestration!,
            onSend: actions.onSendCommand,
          ),
          const SizedBox(height: SonderSpace.md),
        ],
        ConversationContent(content: parsed.answer, fullWidthCode: true),
        if (details.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: SonderSpace.xs),
            child: _DetailsBar(details: details),
          ),
        if (footer != null || showActions)
          Wrap(
            key: const Key('answer-footer'),
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              if (footer != null)
                Padding(
                  padding: const EdgeInsets.only(right: SonderSpace.sm),
                  child: _FooterMetrics(state: footer),
                ),
              if (showActions)
                _Reveal(
                  visible: _revealed(context),
                  child: _AnswerActions(
                    message: message,
                    onFeedback: actions.onFeedback,
                    onRetry:
                        widget.isLast ? () => actions.onRetry(entry.id) : null,
                  ),
                ),
            ],
          ),
      ],
    );
  }
}

/// The turn's glyph, centred in the gutter on the first line of text.
class _Gutter extends StatelessWidget {
  final String glyph;
  final Color color;
  final double top;
  const _Gutter({required this.glyph, required this.color, this.top = 0});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return SizedBox(
      width: transcriptGutter,
      child: Padding(
        padding: EdgeInsets.only(top: top),
        child: ExcludeSemantics(
          child: Text(glyph,
              textAlign: TextAlign.center,
              style: tokens.mono(14,
                  color: color, weight: FontWeight.w600, height: 22)),
        ),
      ),
    );
  }
}

/// Shows [child] (an action row) with a short fade; while hidden it takes
/// no pointer events, but keyboard focus still reaches it (and reveals it)
/// and screen readers still find it.
class _Reveal extends StatelessWidget {
  final bool visible;
  final Widget child;
  const _Reveal({required this.visible, required this.child});

  @override
  Widget build(BuildContext context) => AnimatedOpacity(
        opacity: visible ? 1 : 0,
        duration: SonderMotion.of(context, SonderMotion.fast),
        curve: SonderMotion.standard,
        // Hidden from the pointer, never from screen readers.
        alwaysIncludeSemantics: true,
        child: IgnorePointer(ignoring: !visible, child: child),
      );
}

/// Copy with a visible confirmation: the icon turns into a check for a
/// moment, and a toast says what was copied.
class _CopyAction extends StatefulWidget {
  final String text;
  final String label;
  final String semanticLabel;
  final String toast;
  final VoidCallback? onCopied;
  final AlignmentGeometry alignment;

  const _CopyAction({
    super.key,
    required this.text,
    required this.label,
    required this.semanticLabel,
    required this.toast,
    this.onCopied,
    this.alignment = Alignment.center,
  });

  @override
  State<_CopyAction> createState() => _CopyActionState();
}

class _CopyActionState extends State<_CopyAction> {
  bool _done = false;
  Timer? _reset;

  @override
  void dispose() {
    _reset?.cancel();
    super.dispose();
  }

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.text));
    widget.onCopied?.call();
    if (!mounted) return;
    setState(() => _done = true);
    showSonderToast(context, widget.toast);
    _reset?.cancel();
    _reset = Timer(const Duration(milliseconds: 1600), () {
      if (mounted) setState(() => _done = false);
    });
  }

  @override
  Widget build(BuildContext context) => QuietAction(
        icon: _done ? Icons.check : Icons.copy_outlined,
        label: widget.label.isEmpty ? '' : (_done ? 'Copied' : widget.label),
        tooltip: widget.label.isEmpty ? widget.semanticLabel : null,
        semanticLabel: _done ? widget.toast : widget.semanticLabel,
        selected: _done,
        alignment: widget.alignment,
        onPressed: _copy,
      );
}

/// Copy, Useful, Edited and (on the newest answer) Retry. Useful and Edited
/// are one-shot: once given they stay marked for the session.
class _AnswerActions extends StatefulWidget {
  final ChatMessage message;
  final ValueChanged<String> onFeedback;
  final VoidCallback? onRetry;

  const _AnswerActions({
    required this.message,
    required this.onFeedback,
    this.onRetry,
  });

  @override
  State<_AnswerActions> createState() => _AnswerActionsState();
}

class _AnswerActionsState extends State<_AnswerActions> {
  Set<String> get _given => _feedbackGiven[widget.message] ?? const <String>{};

  void _mark(String key, String command, String toast) {
    _feedbackGiven[widget.message] = {..._given, key};
    widget.onFeedback(command);
    setState(() {});
    showSonderToast(context, toast);
  }

  @override
  Widget build(BuildContext context) {
    final given = _given;
    final useful = given.contains('useful');
    final edited = given.contains('edited');
    // Wraps rather than overflowing at large text sizes.
    return Wrap(
      children: [
        _CopyAction(
          key: const Key('answer-copy'),
          text: widget.message.content,
          label: 'Copy',
          semanticLabel: 'Copy response',
          toast: 'Response copied',
          onCopied: () => widget.onFeedback('/copied'),
        ),
        // Quality feedback trains the learning loop on the last answer.
        // Refusals and errors are not answers and never get these.
        QuietAction(
          key: const Key('answer-useful'),
          icon: useful ? Icons.check_circle : Icons.check_circle_outline,
          label: 'Useful',
          semanticLabel: useful ? 'Marked useful' : 'Mark response useful',
          selected: useful,
          onPressed:
              useful ? null : () => _mark('useful', '/accept', 'Marked useful'),
        ),
        QuietAction(
          key: const Key('answer-edited'),
          icon: edited ? Icons.edit : Icons.edit_outlined,
          label: 'Edited',
          semanticLabel: edited ? 'Marked as edited' : 'Mark response edited',
          tooltip: edited ? null : 'You changed this answer before using it',
          selected: edited,
          onPressed: edited
              ? null
              : () => _mark('edited', '/edited', 'Marked as edited'),
        ),
        if (widget.onRetry != null)
          QuietAction(
            key: const Key('answer-retry'),
            icon: Icons.refresh,
            label: 'Retry',
            semanticLabel: 'Retry: ask again, replacing this answer',
            tooltip: 'Ask again, replacing this answer',
            onPressed: widget.onRetry,
          ),
      ],
    );
  }
}

/// One expandable detail under an answer.
class _Detail {
  final String id;
  final IconData icon;
  final String label;
  final String body;
  final Widget? child;

  /// Refused tool calls in the summary, drawn in the refusal tone.
  final int refused;

  const _Detail({
    required this.id,
    required this.icon,
    required this.label,
    this.body = '',
    this.child,
    this.refused = 0,
  });
}

/// The details of an answer (reasoning, tool calls, the receipt) as one
/// row of quiet disclosures; each opens its panel underneath.
class _DetailsBar extends StatefulWidget {
  final List<_Detail> details;
  const _DetailsBar({required this.details});

  @override
  State<_DetailsBar> createState() => _DetailsBarState();
}

class _DetailsBarState extends State<_DetailsBar> {
  final Set<String> _open = <String>{};

  /// Below this width several details share one disclosure, so a phone
  /// gets one 48 dp row instead of one per detail.
  static const _foldBelow = 480.0;

  @override
  Widget build(BuildContext context) {
    final details = widget.details;
    if (details.length < 2) return _inline(details);
    return LayoutBuilder(
      builder: (context, constraints) => constraints.maxWidth < _foldBelow
          ? _folded(details)
          : _inline(details),
    );
  }

  Widget _folded(List<_Detail> details) {
    final tools = details.where((d) => d.id == 'tools').firstOrNull;
    final open = _open.contains('all');
    final summary = _Detail(
      id: 'all',
      icon: tools?.icon ?? Icons.receipt_long_outlined,
      label: tools?.label ?? 'Details',
      refused: tools?.refused ?? 0,
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Align(
          alignment: Alignment.centerLeft,
          child: _DisclosureChip(
            key: const Key('detail-all'),
            detail: summary,
            open: open,
            onToggle: () => setState(() {
              if (!_open.remove('all')) _open.add('all');
            }),
          ),
        ),
        SonderReveal(
          visible: open,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final d in details)
                Padding(
                  padding: const EdgeInsets.only(bottom: SonderSpace.sm),
                  child: _DetailPanel(detail: d, titled: true),
                ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _inline(List<_Detail> details) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Wrap(
          children: [
            for (final d in details)
              _DisclosureChip(
                key: Key('detail-${d.id}'),
                detail: d,
                open: _open.contains(d.id),
                onToggle: () => setState(() {
                  if (!_open.remove(d.id)) _open.add(d.id);
                }),
              ),
          ],
        ),
        for (final d in details)
          SonderReveal(
            visible: _open.contains(d.id),
            child: Padding(
              padding: const EdgeInsets.only(bottom: SonderSpace.sm),
              child: _DetailPanel(detail: d),
            ),
          ),
      ],
    );
  }
}

class _DisclosureChip extends StatelessWidget {
  final _Detail detail;
  final bool open;
  final VoidCallback onToggle;

  const _DisclosureChip({
    super.key,
    required this.detail,
    required this.open,
    required this.onToggle,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final refused = detail.refused;
    InlineSpan? rich;
    if (refused > 0) {
      final tail = '$refused refused';
      final head = detail.label.endsWith(tail)
          ? detail.label.substring(0, detail.label.length - tail.length)
          : detail.label;
      rich = TextSpan(children: [
        TextSpan(text: head),
        TextSpan(
            text: '${StatusKind.refused.glyph} $tail',
            style: TextStyle(color: tokens.danger)),
      ]);
    }
    return QuietAction(
      icon: detail.icon,
      label: detail.label,
      richLabel: rich,
      semanticLabel: '${detail.label}, ${open ? 'expanded' : 'collapsed'}',
      onPressed: onToggle,
      trailing: AnimatedRotation(
        turns: open ? 0.25 : 0,
        duration: SonderMotion.of(context, SonderMotion.fast),
        curve: SonderMotion.standard,
        child: const Icon(Icons.chevron_right),
      ),
    );
  }
}

class _DetailPanel extends StatelessWidget {
  final _Detail detail;

  /// Names the detail above its content (when several share a panel).
  final bool titled;
  const _DetailPanel({required this.detail, this.titled = false});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final body = detail.child ??
        SelectableText(detail.body,
            style: tokens.mono(11.5, color: tokens.text2, height: 18));
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.md, vertical: SonderSpace.md),
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        border: Border.all(color: tokens.hairline),
      ),
      child: !titled
          ? body
          : Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(detail.id == 'tools' ? 'Tool calls' : detail.label,
                    style: Theme.of(context).textTheme.labelMedium),
                const SizedBox(height: SonderSpace.sm),
                body,
              ],
            ),
    );
  }
}

/// A single collapsed, monospaced detail block (an error's details, a
/// refusal's receipt): one quiet disclosure and its panel.
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
  Widget build(BuildContext context) => _DetailsBar(details: [
        _Detail(id: label, icon: icon, label: label, body: body, child: child),
      ]);
}

class _ToolCallRows extends StatelessWidget {
  final List<ToolCallRow> calls;
  const _ToolCallRows({required this.calls});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final call in calls)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: SonderSpace.xxs),
            child: Row(children: [
              SizedBox(
                width: 80,
                child: Text(
                    call.ok
                        ? '${statusGlyphs['tool']} ran'
                        : '${StatusKind.refused.glyph} refused',
                    style: tokens.mono(11.5,
                        color: call.ok ? tokens.muted : tokens.danger)),
              ),
              Expanded(
                child: Text(call.title,
                    overflow: TextOverflow.ellipsis,
                    style: tokens.mono(12, color: tokens.text2)),
              ),
              if (call.elapsedMs != null)
                Padding(
                  padding: const EdgeInsets.only(left: SonderSpace.sm),
                  child: Text(durationLabel(call.elapsedMs!),
                      style: tokens.mono(11, color: tokens.muted)),
                ),
            ]),
          ),
      ],
    );
  }
}

class _AgentLaneAck extends StatelessWidget {
  final AgentLaneReceipt receipt;
  final ValueChanged<String>? onOpen;
  const _AgentLaneAck({required this.receipt, this.onOpen});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Container(
      padding: const EdgeInsets.fromLTRB(
          SonderSpace.md, SonderSpace.sm, SonderSpace.sm, SonderSpace.sm),
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: const BorderRadius.horizontal(
            right: Radius.circular(SonderRadius.row)),
        border: Border(left: BorderSide(color: tokens.accent, width: 2)),
      ),
      child: Wrap(
        spacing: SonderSpace.md,
        runSpacing: SonderSpace.xs,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          Text.rich(
            TextSpan(children: [
              const TextSpan(text: 'Agent lane '),
              TextSpan(
                  text: receipt.laneId,
                  style: tokens.mono(12.5, color: tokens.text)),
              const TextSpan(text: ' started'),
            ]),
            style: text.bodyMedium?.copyWith(color: tokens.text2),
          ),
          if (receipt.folder.isNotEmpty)
            Text(
              receipt.folder,
              style: tokens.mono(11.5, color: tokens.muted),
              overflow: TextOverflow.ellipsis,
            ),
          if (onOpen != null)
            TextButton.icon(
              key: const Key('open-agent-lane'),
              onPressed: () => onOpen!(receipt.laneId),
              icon: const Icon(Icons.open_in_new, size: 16),
              label: const Text('Open in Agents'),
            ),
        ],
      ),
    );
  }
}

class _OrchestrationChoices extends StatelessWidget {
  final OrchestrationReceipt receipt;
  final ValueChanged<String>? onSend;
  const _OrchestrationChoices({required this.receipt, this.onSend});

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('How should I run this?',
              style: Theme.of(context).textTheme.bodyMedium),
          const SizedBox(height: SonderSpace.sm),
          Wrap(
            spacing: SonderSpace.sm,
            runSpacing: SonderSpace.sm,
            children: [
              for (final choice in receipt.choices)
                ActionChip(
                  key: ValueKey<String>('orchestration-${choice.command}'),
                  label: Text(choice.label),
                  onPressed:
                      onSend == null ? null : () => onSend!(choice.command),
                ),
            ],
          ),
        ],
      );
}
