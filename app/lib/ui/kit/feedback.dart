import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../theme.dart';
import '../status_vocab.dart';
import 'motion.dart';

/// A short confirmation that something happened ("Copied", "Saved",
/// "Marked useful"). Floating, glyph-and-word led, gone in a couple of
/// seconds. For failures that need reading, use a notice instead.
void showSonderToast(
  BuildContext context,
  String message, {
  StatusKind kind = StatusKind.ok,
  String? actionLabel,
  VoidCallback? onAction,
  Duration duration = const Duration(milliseconds: 2200),
}) {
  final messenger = ScaffoldMessenger.maybeOf(context);
  if (messenger == null) return;
  final tokens = SonderTokens.of(context);
  final text = Theme.of(context).textTheme;
  final wide = MediaQuery.sizeOf(context).width >= 600;
  messenger
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(
      duration: duration,
      width: wide ? 380 : null,
      content: Semantics(
        liveRegion: true,
        label: '${kind.word}: $message',
        child: ExcludeSemantics(
          child: Row(children: [
            Text(kind.glyph,
                style: tokens.mono(13,
                    color: kind.color(tokens), weight: FontWeight.w600)),
            const SizedBox(width: SonderSpace.sm),
            Expanded(
              child: Text(message,
                  style: text.bodyMedium?.copyWith(color: tokens.text)),
            ),
          ]),
        ),
      ),
      action: actionLabel == null
          ? null
          : SnackBarAction(
              label: actionLabel,
              textColor: tokens.accentText,
              onPressed: onAction ?? () {},
            ),
    ));
}

/// Plain command or log output: mono, selectable, with Copy, collapsed past
/// [collapsedLines] with an explicit "Show all" toggle. This is where raw
/// slash-command replies go, so they never masquerade as designed UI.
class RawOutput extends StatefulWidget {
  final String text;
  final int collapsedLines;
  final String? label;

  const RawOutput(
    this.text, {
    super.key,
    this.collapsedLines = 14,
    this.label,
  });

  @override
  State<RawOutput> createState() => _RawOutputState();
}

class _RawOutputState extends State<RawOutput> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final lines = widget.text.split('\n');
    final long = lines.length > widget.collapsedLines;
    final shown = !_expanded && long
        ? lines.take(widget.collapsedLines).join('\n')
        : widget.text;
    return Container(
      decoration: BoxDecoration(
        color: tokens.canvas,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        border: Border.all(color: tokens.hairline),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.md, SonderSpace.xs, SonderSpace.xs, 0),
            child: Row(children: [
              Expanded(
                child: Text(widget.label ?? 'Output',
                    style: text.labelSmall),
              ),
              IconButton(
                tooltip: 'Copy output',
                iconSize: 16,
                icon: const Icon(Icons.copy_outlined),
                onPressed: () async {
                  await Clipboard.setData(ClipboardData(text: widget.text));
                  if (context.mounted) showSonderToast(context, 'Output copied');
                },
              ),
            ]),
          ),
          AnimatedSize(
            duration: SonderMotion.of(context, SonderMotion.medium),
            curve: SonderMotion.standard,
            alignment: Alignment.topCenter,
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.fromLTRB(
                  SonderSpace.md, 0, SonderSpace.md, SonderSpace.md),
              child: SelectableText(shown,
                  style: tokens.mono(12, color: tokens.text2, height: 18)),
            ),
          ),
          if (long)
            Align(
              alignment: Alignment.centerLeft,
              child: Padding(
                padding: const EdgeInsets.only(
                    left: SonderSpace.xs, bottom: SonderSpace.xs),
                child: TextButton(
                  onPressed: () => setState(() => _expanded = !_expanded),
                  child: Text(_expanded
                      ? 'Show less'
                      : 'Show all ${lines.length} lines'),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// A collapsed "raw" view for diagnostic text that has no designed
/// presentation yet: the summary stays visible, the dump opens on demand.
class RawDisclosure extends StatefulWidget {
  final String title;
  final String text;
  final bool initiallyOpen;

  const RawDisclosure({
    super.key,
    required this.title,
    required this.text,
    this.initiallyOpen = false,
  });

  @override
  State<RawDisclosure> createState() => _RawDisclosureState();
}

class _RawDisclosureState extends State<RawDisclosure> {
  late bool _open = widget.initiallyOpen;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        HoverSurface(
          onTap: () => setState(() => _open = !_open),
          semanticLabel: '${widget.title}, ${_open ? 'expanded' : 'collapsed'}',
          child: Padding(
            padding: const EdgeInsets.symmetric(
                horizontal: SonderSpace.lg, vertical: SonderSpace.md),
            child: Row(children: [
              AnimatedRotation(
                turns: _open ? 0.25 : 0,
                duration: SonderMotion.of(context, SonderMotion.fast),
                child: Icon(Icons.chevron_right, size: 18, color: tokens.text2),
              ),
              const SizedBox(width: SonderSpace.sm),
              Expanded(
                child: Text(widget.title,
                    style: text.bodyMedium
                        ?.copyWith(fontWeight: FontWeight.w500)),
              ),
            ]),
          ),
        ),
        SonderReveal(
          visible: _open,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.lg, 0, SonderSpace.lg, SonderSpace.md),
            child: RawOutput(widget.text, label: widget.title),
          ),
        ),
      ],
    );
  }
}

/// What an action produced, shown right under the control that ran it.
class ActionOutcome {
  final StatusKind kind;
  final String title;
  final String? detail;

  /// Raw text output (a slash-command reply), shown as [RawOutput].
  final String? output;

  const ActionOutcome(this.kind, this.title, {this.detail, this.output});

  const ActionOutcome.ok(String title, {String? detail, String? output})
      : this(StatusKind.ok, title, detail: detail, output: output);

  const ActionOutcome.failed(String title, {String? detail, String? output})
      : this(StatusKind.fail, title, detail: detail, output: output);
}

/// Renders an [ActionOutcome]: a glyph-and-word line, an optional detail and
/// output, and a dismiss button.
class OutcomeView extends StatelessWidget {
  final ActionOutcome outcome;
  final VoidCallback? onDismiss;

  const OutcomeView(this.outcome, {super.key, this.onDismiss});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final color = outcome.kind.color(tokens);
    return Semantics(
      liveRegion: true,
      container: true,
      label: '${outcome.kind.word}: ${outcome.title}',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            ExcludeSemantics(
              child: Text('${outcome.kind.glyph} ${outcome.kind.word}',
                  style: tokens.mono(12.5,
                      color: color, weight: FontWeight.w600)),
            ),
            const SizedBox(width: SonderSpace.md),
            Expanded(
              child: ExcludeSemantics(
                child: Text(outcome.title,
                    style: text.bodyMedium?.copyWith(color: tokens.text)),
              ),
            ),
            if (onDismiss != null)
              IconButton(
                tooltip: 'Dismiss',
                iconSize: 16,
                icon: const Icon(Icons.close),
                onPressed: onDismiss,
              ),
          ]),
          if (outcome.detail != null)
            Padding(
              padding: const EdgeInsets.only(top: SonderSpace.xs),
              child: SelectableText(outcome.detail!,
                  style: text.bodySmall?.copyWith(color: tokens.text2)),
            ),
          if (outcome.output != null && outcome.output!.trim().isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: SonderSpace.sm),
              child: RawOutput(outcome.output!),
            ),
        ],
      ),
    );
  }
}
