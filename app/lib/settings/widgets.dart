/// Small pieces the Settings pages share: a text setting with its label
/// above the field, the field itself, the "changed" dot, and the bar that
/// appears while there are unsaved changes.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme.dart';
import '../ui/kit.dart';
import '../ui/status_row.dart';
import '../ui/status_vocab.dart';

/// Below this width a field's action (Test connection) moves under it.
const _actionBesideFieldFrom = 520.0;

/// The accent dot that marks a value differing from its saved value. Same
/// mark as the kit's [SettingRow] `modified:` dot.
class ModifiedDot extends StatelessWidget {
  /// Null draws the bare dot (when nearby text already says it).
  final String? tooltip;
  final String semanticLabel;

  const ModifiedDot({
    super.key,
    this.tooltip = 'Changed',
    this.semanticLabel = 'changed',
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final dot = Container(
      width: 6,
      height: 6,
      decoration: BoxDecoration(color: tokens.accent, shape: BoxShape.circle),
    );
    if (tooltip == null) return ExcludeSemantics(child: dot);
    return Tooltip(
      message: tooltip,
      child: Semantics(label: semanticLabel, child: dot),
    );
  }
}

/// A text setting: its label and description above a full-width field, an
/// optional action beside the field, and an inline result revealed under
/// it. The label is drawn once; the field carries it for screen readers.
class SettingsFieldRow extends StatelessWidget {
  final String label;
  final String? description;
  final bool modified;
  final Widget field;

  /// A button that acts on this field, e.g. "Test connection".
  final Widget? action;

  /// What the action (or the field) produced, shown under the field.
  final Widget? below;

  const SettingsFieldRow({
    super.key,
    required this.label,
    required this.field,
    this.description,
    this.modified = false,
    this.action,
    this.below,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.lg, vertical: SonderSpace.md),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          ExcludeSemantics(
            child: Row(children: [
              Flexible(
                child: Text(label,
                    style: text.bodyMedium?.copyWith(
                        fontWeight: FontWeight.w500, color: tokens.text)),
              ),
              if (modified) ...[
                const SizedBox(width: SonderSpace.sm),
                const ModifiedDot(),
              ],
            ]),
          ),
          if (description != null)
            Padding(
              padding: const EdgeInsets.only(top: SonderSpace.xxs),
              child: Text(description!, style: text.bodySmall),
            ),
          const SizedBox(height: SonderSpace.sm),
          LayoutBuilder(builder: (context, constraints) {
            final action = this.action;
            if (action == null) return field;
            if (constraints.maxWidth < _actionBesideFieldFrom) {
              return Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  field,
                  const SizedBox(height: SonderSpace.sm),
                  action,
                ],
              );
            }
            return Row(children: [
              Expanded(child: field),
              const SizedBox(width: SonderSpace.md),
              action,
            ]);
          }),
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
  }
}

/// A [TextField] whose accessible name is [label] (drawn above it by
/// [SettingsFieldRow]), so the visible label is not repeated inside the
/// border. Technical values ([mono]) use the mono face.
class LabeledTextField extends StatelessWidget {
  /// Key of the [TextField] itself: tests and deep links address the field.
  final Key? fieldKey;
  final String label;
  final TextEditingController controller;
  final String? hint;
  final bool obscureText;
  final bool mono;
  final Widget? suffix;
  final String? errorText;
  final TextInputType? keyboardType;
  final List<TextInputFormatter>? inputFormatters;
  final ValueChanged<String>? onChanged;
  final ValueChanged<String>? onSubmitted;
  final FocusNode? focusNode;
  final TextAlign textAlign;
  final String? suffixText;

  const LabeledTextField({
    super.key,
    this.fieldKey,
    required this.label,
    required this.controller,
    this.hint,
    this.obscureText = false,
    this.mono = false,
    this.suffix,
    this.errorText,
    this.keyboardType,
    this.inputFormatters,
    this.onChanged,
    this.onSubmitted,
    this.focusNode,
    this.textAlign = TextAlign.start,
    this.suffixText,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final style = mono
        ? tokens.mono(13.5, color: tokens.text, height: 22)
        : text.bodyMedium;
    return Semantics(
      container: true,
      label: label,
      child: TextField(
        key: fieldKey,
        controller: controller,
        focusNode: focusNode,
        obscureText: obscureText,
        autocorrect: false,
        enableSuggestions: !obscureText,
        keyboardType: keyboardType,
        inputFormatters: inputFormatters,
        onChanged: onChanged,
        onSubmitted: onSubmitted,
        textAlign: textAlign,
        style: style,
        decoration: InputDecoration(
          hintText: hint,
          hintStyle: (mono ? tokens.mono(13.5, height: 22) : text.bodyMedium)
              ?.copyWith(color: tokens.muted),
          suffixIcon: suffix,
          suffixText: suffixText,
          suffixStyle: text.bodySmall?.copyWith(color: tokens.muted),
          errorText: errorText,
          errorMaxLines: 3,
        ),
      ),
    );
  }
}

/// Show / hide for a masked field. The tooltip says what it does
/// ("Show API key"), so the button is never an unlabelled eye.
class VisibilityToggle extends StatelessWidget {
  final bool obscured;
  final String what;
  final VoidCallback onPressed;

  const VisibilityToggle({
    super.key,
    required this.obscured,
    required this.what,
    required this.onPressed,
  });

  @override
  Widget build(BuildContext context) => IconButton(
        tooltip: obscured ? 'Show $what' : 'Hide $what',
        icon: Icon(obscured
            ? Icons.visibility_outlined
            : Icons.visibility_off_outlined),
        onPressed: onPressed,
      );
}

/// A note under a control, led by the status glyph and word like every
/// other inline result (`! warn  The API key is not sent…`). Notes ([kind]
/// note) are quieter than warnings.
class FieldNote extends StatelessWidget {
  final StatusKind kind;
  final String text;

  const FieldNote(this.kind, this.text, {super.key});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final theme = Theme.of(context).textTheme;
    final quiet = kind == StatusKind.note;
    return Semantics(
      container: true,
      liveRegion: true,
      label: '${kind.word}: $text',
      child: ExcludeSemantics(
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Padding(
            padding: EdgeInsets.only(top: quiet ? 0 : 2),
            child: StatusMark(kind, size: quiet ? 12 : 12.5),
          ),
          const SizedBox(width: SonderSpace.md),
          Expanded(
            child: Text(text,
                style: quiet
                    ? theme.bodySmall
                    : theme.bodyMedium?.copyWith(color: tokens.text)),
          ),
        ]),
      ),
    );
  }
}

/// A read-only fact: label (and description) on the left, its value on the
/// right, stacked on narrow widths. The value is plain text, so on a phone
/// it never becomes a tiny long-press target; technical values ([mono]) get
/// a Copy button instead ([copyable]).
class FactRow extends StatelessWidget {
  final String label;
  final String value;
  final String? description;
  final StatusKind? kind;
  final bool mono;
  final bool copyable;
  final Widget? trailing;

  const FactRow({
    super.key,
    required this.label,
    required this.value,
    this.description,
    this.kind,
    this.mono = false,
    this.copyable = false,
    this.trailing,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final valueStyle = mono
        ? tokens.mono(12.5, color: tokens.text2)
        : text.bodyMedium?.copyWith(color: tokens.text2);
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
                padding: const EdgeInsets.only(top: SonderSpace.xxs),
                child: Text(description!, style: text.bodySmall),
              ),
          ],
        );
        final valueBlock = Row(mainAxisSize: MainAxisSize.min, children: [
          if (kind != null) ...[
            StatusMark(kind!, size: 12),
            const SizedBox(width: SonderSpace.sm),
          ],
          Flexible(
            child: Text(value,
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
                if (context.mounted) showSonderToast(context, '$label copied');
              },
            ),
          if (trailing != null) ...[
            const SizedBox(width: SonderSpace.xs),
            trailing!,
          ],
        ]);
        if (narrow) {
          return Padding(
            padding: const EdgeInsets.symmetric(vertical: SonderSpace.sm),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                labelBlock,
                const SizedBox(height: SonderSpace.xs),
                valueBlock,
              ],
            ),
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

/// A quiet pointer to where related controls live, under a section:
/// "Open Observatory is on the Runtime page.  Open Runtime".
class RelatedLink extends StatelessWidget {
  final String text;
  final String action;
  final VoidCallback onPressed;

  const RelatedLink({
    super.key,
    required this.text,
    required this.action,
    required this.onPressed,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: SonderSpace.xs),
      child: Wrap(
        crossAxisAlignment: WrapCrossAlignment.center,
        spacing: SonderSpace.xs,
        children: [
          Text(text, style: Theme.of(context).textTheme.bodySmall),
          TextButton(onPressed: onPressed, child: Text(action)),
        ],
      ),
    );
  }
}

/// The sticky bar that rises from the bottom while Settings holds unsaved
/// changes: what is pending (or why Save refused), Discard and Save. It is
/// absent, not disabled, when there is nothing to save.
class UnsavedChangesBar extends StatelessWidget {
  final bool visible;

  /// Pages with changes, e.g. ["Connection", "General"].
  final List<String> where;

  /// Why the last Save was refused; replaces the summary until an edit.
  final String? error;
  final Future<void> Function() onSave;
  final VoidCallback onDiscard;

  /// The reading width of the pages, so the bar's edges line up with them.
  final double contentMaxWidth;

  const UnsavedChangesBar({
    super.key,
    required this.visible,
    required this.where,
    required this.onSave,
    required this.onDiscard,
    required this.contentMaxWidth,
    this.error,
  });

  @override
  Widget build(BuildContext context) {
    return AnimatedSwitcher(
      duration: SonderMotion.of(context, SonderMotion.medium),
      reverseDuration: SonderMotion.of(context, SonderMotion.fast),
      switchInCurve: SonderMotion.enter,
      switchOutCurve: SonderMotion.exit,
      // Rises from the bottom edge: the top of the bar is revealed last.
      transitionBuilder: (child, animation) => SizeTransition(
        sizeFactor: animation,
        alignment: Alignment.bottomCenter,
        child: FadeTransition(opacity: animation, child: child),
      ),
      child: visible
          ? KeyedSubtree(
              key: const ValueKey('unsaved-bar'), child: _bar(context))
          : const SizedBox(
              key: ValueKey('no-unsaved-bar'), width: double.infinity),
    );
  }

  Widget _bar(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final error = this.error;
    final summary = error != null
        ? Row(
            key: const ValueKey('unsaved-error'),
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(StatusKind.warn.glyph,
                  style: tokens.mono(13,
                      color: tokens.warn, weight: FontWeight.w600)),
              const SizedBox(width: SonderSpace.sm),
              Expanded(
                child: Text(error,
                    style: text.bodyMedium?.copyWith(color: tokens.text)),
              ),
            ],
          )
        : Row(
            key: const ValueKey('unsaved-summary'),
            children: [
              const ModifiedDot(tooltip: null),
              const SizedBox(width: SonderSpace.sm),
              Flexible(
                child: Text.rich(
                  TextSpan(children: [
                    TextSpan(
                        text: 'Unsaved changes',
                        style: text.bodyMedium?.copyWith(
                            color: tokens.text, fontWeight: FontWeight.w500)),
                    if (where.isNotEmpty)
                      TextSpan(
                          text: ' · ${where.join(', ')}',
                          style:
                              text.bodyMedium?.copyWith(color: tokens.text2)),
                  ]),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ],
          );
    final buttons = Row(mainAxisSize: MainAxisSize.min, children: [
      TextButton(
        key: const Key('settings-discard'),
        onPressed: onDiscard,
        child: const Text('Discard'),
      ),
      const SizedBox(width: SonderSpace.sm),
      AsyncActionButton(
        buttonKey: const Key('settings-save'),
        label: 'Save',
        busyLabel: 'Saving…',
        doneLabel: null,
        style: ActionButtonStyle.filled,
        onPressed: onSave,
      ),
    ]);
    return Container(
      decoration: BoxDecoration(
        color: tokens.panel,
        border: Border(top: BorderSide(color: tokens.hairline)),
      ),
      child: SafeArea(
        top: false,
        child: LayoutBuilder(builder: (context, constraints) {
          final narrow = constraints.maxWidth < 560;
          final side = narrow ? SonderSpace.lg : SonderSpace.x3;
          final content = Semantics(
            container: true,
            liveRegion: true,
            child: narrow
                ? Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      SonderSwitcher(child: summary),
                      const SizedBox(height: SonderSpace.sm),
                      Align(alignment: Alignment.centerRight, child: buttons),
                    ],
                  )
                : Row(children: [
                    Expanded(child: SonderSwitcher(child: summary)),
                    const SizedBox(width: SonderSpace.lg),
                    buttons,
                  ]),
          );
          return Align(
            alignment: Alignment.topCenter,
            child: ConstrainedBox(
              constraints:
                  BoxConstraints(maxWidth: contentMaxWidth + 2 * side),
              child: Padding(
                padding: EdgeInsets.symmetric(
                    horizontal: side, vertical: SonderSpace.md),
                child: content,
              ),
            ),
          );
        }),
      ),
    );
  }
}
