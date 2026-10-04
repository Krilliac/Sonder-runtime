import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../theme.dart';
import '../status_row.dart';
import '../status_vocab.dart';
import 'feedback.dart';
import 'motion.dart';

/// A titled card of related rows: the unit every settings and runtime page
/// is built from. Rows are separated by hairlines; the card itself is the
/// panel surface with a hairline border, so pages read as a few calm blocks
/// instead of one long form.
class SettingsSection extends StatelessWidget {
  final String? title;
  final String? description;

  /// Header action, e.g. a Refresh icon button or a "Manage" link.
  final Widget? trailing;
  final List<Widget> children;

  /// Insert hairlines between [children] (rows). Off for free-form content.
  final bool dividers;
  final EdgeInsetsGeometry contentPadding;

  const SettingsSection({
    super.key,
    this.title,
    this.description,
    this.trailing,
    required this.children,
    this.dividers = true,
    this.contentPadding = EdgeInsets.zero,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final body = <Widget>[];
    for (var i = 0; i < children.length; i++) {
      if (i > 0 && dividers) {
        body.add(Divider(
            height: 1,
            thickness: 1,
            indent: SonderSpace.lg,
            endIndent: SonderSpace.lg,
            color: tokens.hairline));
      }
      body.add(children[i]);
    }
    final hasHeader = title != null || trailing != null;
    return Padding(
      padding: const EdgeInsets.only(bottom: SonderSpace.lg),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (hasHeader)
            Padding(
              padding: const EdgeInsets.fromLTRB(
                  SonderSpace.xs, 0, 0, SonderSpace.sm),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.center,
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        if (title != null)
                          Semantics(
                            header: true,
                            child: Text(title!,
                                style: text.titleSmall
                                    ?.copyWith(fontWeight: FontWeight.w600)),
                          ),
                        if (description != null)
                          Padding(
                            padding: const EdgeInsets.only(top: 2),
                            child: Text(description!, style: text.bodySmall),
                          ),
                      ],
                    ),
                  ),
                  if (trailing != null) trailing!,
                ],
              ),
            ),
          if (body.isNotEmpty)
            Container(
              padding: contentPadding,
              decoration: BoxDecoration(
                color: tokens.panel,
                borderRadius: BorderRadius.circular(SonderRadius.card),
                border: Border.all(color: tokens.hairline),
              ),
              clipBehavior: Clip.antiAlias,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: body,
              ),
            ),
        ],
      ),
    );
  }
}

/// One setting: label and description on the left, its control on the
/// right. Below [stackBelow] the control moves under the text. [below] is
/// optional content revealed under the row (an inline result or error), so
/// feedback appears next to the control that caused it.
class SettingRow extends StatelessWidget {
  final String label;
  final String? description;
  final Widget? leading;
  final Widget? trailing;
  final Widget? below;
  final VoidCallback? onTap;

  /// Marks a value that differs from the default or from the saved value.
  final bool modified;
  final bool enabled;
  final double stackBelow;

  const SettingRow({
    super.key,
    required this.label,
    this.description,
    this.leading,
    this.trailing,
    this.below,
    this.onTap,
    this.modified = false,
    this.enabled = true,
    this.stackBelow = 520,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final labelStyle = text.bodyMedium?.copyWith(
      fontWeight: FontWeight.w500,
      color: enabled ? tokens.text : tokens.muted,
    );
    final labelBlock = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(mainAxisSize: MainAxisSize.min, children: [
          Flexible(child: Text(label, style: labelStyle)),
          if (modified) ...[
            const SizedBox(width: SonderSpace.sm),
            Tooltip(
              message: 'Changed',
              child: Semantics(
                label: 'changed',
                child: Container(
                  width: 6,
                  height: 6,
                  decoration: BoxDecoration(
                      color: tokens.accent, shape: BoxShape.circle),
                ),
              ),
            ),
          ],
        ]),
        if (description != null)
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Text(description!, style: text.bodySmall),
          ),
      ],
    );
    final content = LayoutBuilder(builder: (context, constraints) {
      final stacked = trailing != null && constraints.maxWidth < stackBelow;
      final leadingWidget = leading == null
          ? null
          : Padding(
              padding: const EdgeInsets.only(right: SonderSpace.md, top: 1),
              child: IconTheme.merge(
                data: IconThemeData(size: 20, color: tokens.text2),
                child: leading!,
              ),
            );
      if (stacked) {
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              if (leadingWidget != null) leadingWidget,
              Expanded(child: labelBlock),
            ]),
            const SizedBox(height: SonderSpace.sm),
            Padding(
              padding: EdgeInsets.only(left: leading == null ? 0 : 32),
              child: trailing!,
            ),
          ],
        );
      }
      return Row(children: [
        if (leadingWidget != null) leadingWidget,
        Expanded(child: labelBlock),
        if (trailing != null) ...[
          const SizedBox(width: SonderSpace.lg),
          trailing!,
        ],
      ]);
    });
    final row = Container(
      // A whole-row tap target is never shorter than 48 (tap-target tests).
      constraints: const BoxConstraints(minHeight: 48),
      alignment: Alignment.centerLeft,
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.lg, vertical: SonderSpace.md),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          content,
          SonderReveal(
            visible: below != null,
            child: Padding(
              padding: const EdgeInsets.only(top: SonderSpace.md),
              child: below ?? const SizedBox.shrink(),
            ),
          ),
        ],
      ),
    );
    if (onTap == null || !enabled) return row;
    return HoverSurface(
      onTap: onTap,
      borderRadius: BorderRadius.zero,
      child: row,
    );
  }
}

/// A [SettingRow] whose control is a switch; tapping the row toggles it.
class SwitchRow extends StatelessWidget {
  final String label;
  final String? description;
  final Widget? leading;
  final bool value;
  final ValueChanged<bool>? onChanged;
  final bool modified;

  /// Key for the [Switch] itself (tests flip the switch by key).
  final Key? switchKey;

  const SwitchRow({
    super.key,
    required this.label,
    required this.value,
    required this.onChanged,
    this.description,
    this.leading,
    this.modified = false,
    this.switchKey,
  });

  @override
  Widget build(BuildContext context) {
    return MergeSemantics(
      child: SettingRow(
        label: label,
        description: description,
        leading: leading,
        modified: modified,
        enabled: onChanged != null,
        onTap: onChanged == null ? null : () => onChanged!(!value),
        trailing: Switch(key: switchKey, value: value, onChanged: onChanged),
      ),
    );
  }
}

/// A read-only fact: label, value and (optionally) a status mark and a copy
/// button. Technical values (paths, ids, digests, URLs) are mono.
class ValueRow extends StatelessWidget {
  final String label;
  final String value;
  final StatusKind? kind;
  final String? word;
  final bool mono;
  final bool copyable;
  final Widget? trailing;
  final String? description;

  const ValueRow({
    super.key,
    required this.label,
    required this.value,
    this.kind,
    this.word,
    this.mono = false,
    this.copyable = false,
    this.trailing,
    this.description,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final valueStyle = mono
        ? tokens.mono(12.5, color: tokens.text2)
        : text.bodyMedium?.copyWith(color: tokens.text2);
    // A fixed rhythm: rows with and without a copy button are the same
    // height (the button keeps its 48 dp target inside the row).
    return Container(
      constraints: const BoxConstraints(minHeight: 56),
      alignment: Alignment.centerLeft,
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.lg, vertical: SonderSpace.xs),
      child: LayoutBuilder(builder: (context, constraints) {
        final narrow = constraints.maxWidth < 480;
        final labelBlock = Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(label,
                style: text.bodyMedium?.copyWith(fontWeight: FontWeight.w500)),
            if (description != null)
              Padding(
                padding: const EdgeInsets.only(top: 2),
                child: Text(description!, style: text.bodySmall),
              ),
          ],
        );
        final valueBlock = Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (kind != null) ...[
              StatusMark(kind!, word: word, size: 12),
              const SizedBox(width: SonderSpace.sm),
            ],
            Flexible(
              child: SelectableText(value,
                  style: valueStyle,
                  textAlign: narrow ? TextAlign.start : TextAlign.end),
            ),
            if (copyable)
              IconButton(
                tooltip: 'Copy $label',
                iconSize: 16,
                icon: const Icon(Icons.copy_outlined),
                onPressed: () async {
                  await Clipboard.setData(ClipboardData(text: value));
                  if (context.mounted) {
                    showSonderToast(context, '$label copied');
                  }
                },
              ),
            if (trailing != null) ...[
              const SizedBox(width: SonderSpace.xs),
              trailing!,
            ],
          ],
        );
        if (narrow) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              labelBlock,
              const SizedBox(height: SonderSpace.xs),
              valueBlock,
            ],
          );
        }
        return Row(children: [
          Expanded(flex: 2, child: labelBlock),
          const SizedBox(width: SonderSpace.lg),
          Flexible(
            flex: 3,
            child: Align(alignment: Alignment.centerRight, child: valueBlock),
          ),
        ]);
      }),
    );
  }
}
