/// The row vocabulary of the Runtime pages, built on the component kit:
/// list rows that lead with a status mark, one-line notes, capability rows,
/// small stat strips and status-value rows that a screen reader hears as one
/// sentence. Every status shows its glyph and word; colour never carries it
/// alone.
library;

import 'package:flutter/material.dart';

import '../theme.dart';
import '../ui/kit.dart';
import '../ui/status_row.dart';
import 'status_word.dart';

/// One row of a Runtime list (a work run, an agent, an approval, a job): a
/// status mark, a title, an optional second line and actions at the end.
///
/// Wide rows keep the marks in one column so a list scans like the REPL's
/// column 11. Below 480 px the mark sits on its own line and the actions
/// wrap under the text, so nothing is squeezed into a sliver.
class RuntimeRow extends StatelessWidget {
  final StatusKind? kind;

  /// A synonym for the kind's word, or the server's own status word.
  final String? word;

  /// Drawn instead of a status mark (a time, an icon).
  final Widget? leading;
  final Widget title;
  final Widget? subtitle;
  final List<Widget> actions;

  /// Content revealed under the row: an action's outcome, details.
  final Widget? below;
  final double markWidth;

  /// What a screen reader hears for the text part (the actions stay
  /// separately reachable).
  final String? semanticLabel;

  /// Tighter rows for long read-only lists (no actions, no hairlines).
  final bool dense;

  const RuntimeRow({
    super.key,
    required this.title,
    this.kind,
    this.word,
    this.leading,
    this.subtitle,
    this.actions = const [],
    this.below,
    this.markWidth = 112,
    this.semanticLabel,
    this.dense = false,
  });

  static const wideBreakpoint = 480.0;

  @override
  Widget build(BuildContext context) {
    return Container(
      constraints: BoxConstraints(minHeight: dense ? 0 : 48),
      padding: EdgeInsets.symmetric(
          horizontal: SonderSpace.lg,
          vertical: dense ? SonderSpace.sm : SonderSpace.md),
      alignment: Alignment.centerLeft,
      child: LayoutBuilder(builder: (context, constraints) {
        final narrow = constraints.maxWidth < wideBreakpoint;
        final mark = leading ??
            (kind == null
                ? null
                : StatusMark(kind!,
                    word: word ?? kind!.runtimeWord, size: 12.5));
        Widget text(Widget child) => semanticLabel == null
            ? child
            : Semantics(
                container: true,
                label: semanticLabel,
                excludeSemantics: true,
                child: child);
        if (narrow) {
          // One action rides on the mark's line; more wrap under the text.
          final inline = mark != null && actions.length == 1;
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (inline)
                // Mark left, action right; with large text the action moves
                // under the mark instead of overflowing.
                SizedBox(
                  width: double.infinity,
                  child: Wrap(
                    alignment: WrapAlignment.spaceBetween,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    spacing: SonderSpace.sm,
                    children: [
                      // The labelled text block below already speaks it.
                      semanticLabel == null
                          ? mark
                          : ExcludeSemantics(child: mark),
                      actions.single,
                    ],
                  ),
                ),
              text(Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (mark != null && !inline) ...[
                    mark,
                    const SizedBox(height: SonderSpace.xs),
                  ],
                  title,
                  if (subtitle != null) ...[
                    const SizedBox(height: SonderSpace.xxs),
                    subtitle!,
                  ],
                ],
              )),
              if (actions.isNotEmpty && !inline) ...[
                const SizedBox(height: SonderSpace.sm),
                Wrap(
                  spacing: SonderSpace.sm,
                  runSpacing: SonderSpace.xs,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: actions,
                ),
              ],
              if (below != null)
                Padding(
                  padding: const EdgeInsets.only(top: SonderSpace.md),
                  child: below!,
                ),
            ],
          );
        }
        final indent = mark == null ? 0.0 : markWidth;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(children: [
              Expanded(
                child: text(Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      // A status mark shares the title's baseline; a custom
                      // leading (an icon, a time) centres on its line.
                      crossAxisAlignment: leading == null
                          ? CrossAxisAlignment.baseline
                          : CrossAxisAlignment.center,
                      textBaseline: TextBaseline.alphabetic,
                      children: [
                        if (mark != null)
                          SizedBox(width: markWidth, child: mark),
                        Expanded(child: title),
                      ],
                    ),
                    if (subtitle != null)
                      Padding(
                        padding:
                            EdgeInsets.only(left: indent, top: SonderSpace.xxs),
                        child: subtitle!,
                      ),
                  ],
                )),
              ),
              if (actions.isNotEmpty) ...[
                const SizedBox(width: SonderSpace.md),
                Wrap(
                  spacing: SonderSpace.xs,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: actions,
                ),
              ],
            ]),
            if (below != null)
              Padding(
                padding: EdgeInsets.only(left: indent, top: SonderSpace.md),
                child: below!,
              ),
          ],
        );
      }),
    );
  }
}

/// A one-line `<glyph> <word>  text` note inside a Runtime card: an empty,
/// loading, not-allowed or failed state, with an optional action.
class RuntimePanelNote extends StatelessWidget {
  final StatusKind status;
  final String? word;
  final String text;
  final Widget? action;

  const RuntimePanelNote({
    super.key,
    required this.status,
    required this.text,
    this.word,
    this.action,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return RuntimeRow(
      kind: status,
      word: word,
      title: Text(text,
          style: Theme.of(context)
              .textTheme
              .bodyMedium
              ?.copyWith(color: tokens.text2)),
      actions: [if (action != null) action!],
    );
  }
}

/// A primary line in Runtime rows: sans for names, mono for ids and paths.
class RuntimeRowTitle extends StatelessWidget {
  final String text;
  final bool mono;
  final int maxLines;

  const RuntimeRowTitle(this.text,
      {super.key, this.mono = false, this.maxLines = 2});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final style = mono
        ? tokens.mono(13, weight: FontWeight.w500)
        : Theme.of(context)
            .textTheme
            .bodyMedium
            ?.copyWith(fontWeight: FontWeight.w500);
    return Text(text,
        maxLines: maxLines, overflow: TextOverflow.ellipsis, style: style);
  }
}

/// A secondary line in Runtime rows.
class RuntimeRowDetail extends StatelessWidget {
  final String text;
  final bool mono;
  final int maxLines;

  const RuntimeRowDetail(this.text,
      {super.key, this.mono = false, this.maxLines = 2});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final style = mono
        ? tokens.mono(12, color: tokens.text2)
        : Theme.of(context).textTheme.bodySmall;
    return Text(text,
        maxLines: maxLines, overflow: TextOverflow.ellipsis, style: style);
  }
}

/// One figure in a [RuntimeStatStrip].
class RuntimeStat {
  final String label;
  final String value;
  final Key? key;
  const RuntimeStat(this.label, this.value, {this.key});
}

/// A strip of small labelled figures at the top of a card ("Worker slots
/// 8", "Free memory 12.3 GiB"): a quieter cousin of the overview tiles.
class RuntimeStatStrip extends StatelessWidget {
  final List<RuntimeStat> stats;

  const RuntimeStatStrip(this.stats, {super.key});

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.lg, vertical: SonderSpace.md),
      child: Wrap(
        spacing: SonderSpace.x3,
        runSpacing: SonderSpace.md,
        children: [
          for (final stat in stats)
            Semantics(
              key: stat.key,
              container: true,
              label: '${stat.label}: ${stat.value}',
              excludeSemantics: true,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(stat.label, style: text.labelMedium),
                  const SizedBox(height: SonderSpace.xxs),
                  Text(stat.value,
                      style: text.titleMedium
                          ?.copyWith(fontWeight: FontWeight.w600)),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

/// A capability the runtime reports: its name, the runtime's reason, and
/// `✓ ok` when available or `– off` when not (off by design, never red).
class CapabilityRow extends StatelessWidget {
  final String label;
  final bool available;
  final String reason;

  const CapabilityRow({
    super.key,
    required this.label,
    required this.available,
    this.reason = '',
  });

  @override
  Widget build(BuildContext context) {
    final kind = available ? StatusKind.ok : StatusKind.skipped;
    return MergeSemantics(
      child: SettingRow(
        label: label,
        description: reason.isEmpty ? null : reason,
        trailing: StatusMark(kind, word: kind.runtimeWord, size: 12.5),
      ),
    );
  }
}

/// A [ValueRow] with a status that a screen reader hears as one sentence,
/// "ok, Sonder Inference: ready" (the row's text is not separately
/// focusable; use a plain [ValueRow] when the value must be copyable).
class StatusValueRow extends StatelessWidget {
  final String label;
  final String value;
  final StatusKind kind;
  final String? word;
  final bool mono;
  final String? description;
  final Widget? trailing;

  const StatusValueRow({
    super.key,
    required this.label,
    required this.value,
    required this.kind,
    this.word,
    this.mono = false,
    this.description,
    this.trailing,
  });

  @override
  Widget build(BuildContext context) {
    final row = ValueRow(
      label: label,
      value: value,
      kind: kind,
      word: word ?? kind.runtimeWord,
      mono: mono,
      description: description,
    );
    final spoken = Semantics(
      container: true,
      label: '${word ?? kind.runtimeWord}, $label: $value',
      excludeSemantics: true,
      child: row,
    );
    if (trailing == null) return spoken;
    return Row(children: [
      Expanded(child: spoken),
      Padding(
        padding: const EdgeInsets.only(right: SonderSpace.sm),
        child: trailing!,
      ),
    ]);
  }
}

/// The body of a card that has nothing to list yet: a muted sentence with
/// an optional action, at row height.
class RuntimeEmptyRow extends StatelessWidget {
  final String text;
  final IconData icon;
  final Widget? action;

  const RuntimeEmptyRow(this.text,
      {super.key, this.icon = Icons.inbox_outlined, this.action});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Container(
      constraints: const BoxConstraints(minHeight: 56),
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.lg, vertical: SonderSpace.md),
      child: Row(children: [
        Icon(icon, size: 18, color: tokens.muted),
        const SizedBox(width: SonderSpace.md),
        Expanded(
          child: Text(text,
              style: Theme.of(context)
                  .textTheme
                  .bodyMedium
                  ?.copyWith(color: tokens.text2)),
        ),
        if (action != null) action!,
      ]),
    );
  }
}

/// [rows] with the card's hairline between each, for lists that live inside
/// one child of a [SettingsSection] (a disclosure body).
List<Widget> dividedRows(BuildContext context, List<Widget> rows) {
  final tokens = SonderTokens.of(context);
  return [
    for (var i = 0; i < rows.length; i++) ...[
      if (i > 0)
        Divider(
            height: 1,
            thickness: 1,
            indent: SonderSpace.lg,
            endIndent: SonderSpace.lg,
            color: tokens.hairline),
      rows[i],
    ],
  ];
}

/// A card's padded free-form block (forms, meters, prose) so it lines up
/// with the rows around it.
class RuntimeCardBody extends StatelessWidget {
  final Widget child;
  final EdgeInsetsGeometry padding;

  const RuntimeCardBody({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(SonderSpace.lg),
  });

  @override
  Widget build(BuildContext context) => Padding(padding: padding, child: child);
}
