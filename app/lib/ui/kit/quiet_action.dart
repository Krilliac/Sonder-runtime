import 'package:flutter/material.dart';

import '../../theme.dart';

/// A compact action for toolbars that sit inside content (under an answer,
/// in a code block header, beside a message): a small icon and label on a
/// 28 dp pill, inside a full 48 dp tap target.
///
/// Quiet by default (muted, no fill); hover, press and keyboard focus draw
/// a raised surface and a focus ring on the pill only, so a row of them
/// stays light. [selected] tints the icon and label in [selectedColor]
/// (ok by default) and is announced, for one-shot actions such as
/// "Marked useful".
class QuietAction extends StatefulWidget {
  final IconData icon;

  /// The visible label; empty draws the icon alone (give a [tooltip]).
  final String label;

  /// Shown on hover and long press, and the spoken name when there is no
  /// label.
  final String? tooltip;

  /// The spoken name; defaults to [tooltip], then [label].
  final String? semanticLabel;
  final VoidCallback? onPressed;
  final bool selected;
  final Color? selectedColor;

  /// Where the pill sits inside the 48 dp target (centre by default), so a
  /// row can line the pill up with neighbouring text.
  final AlignmentGeometry alignment;

  /// Draws the label with styled runs (a count in a status colour). Its
  /// plain text should match [label], which stays the spoken name.
  final InlineSpan? richLabel;

  /// After the label, e.g. a disclosure chevron.
  final Widget? trailing;

  /// The icon's colour when not selected; defaults to the label's.
  final Color? iconColor;

  const QuietAction({
    super.key,
    required this.icon,
    this.label = '',
    this.tooltip,
    this.semanticLabel,
    required this.onPressed,
    this.selected = false,
    this.selectedColor,
    this.alignment = Alignment.center,
    this.richLabel,
    this.trailing,
    this.iconColor,
  });

  @override
  State<QuietAction> createState() => _QuietActionState();
}

class _QuietActionState extends State<QuietAction> {
  bool _hover = false;
  bool _focus = false;
  bool _pressed = false;

  bool get _enabled => widget.onPressed != null;

  void _activate() {
    final onPressed = widget.onPressed;
    if (onPressed != null) onPressed();
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final selectedColor = widget.selectedColor ?? tokens.ok;
    final foreground = widget.selected
        ? selectedColor
        : !_enabled
            ? tokens.muted
            : (_hover || _focus)
                ? tokens.text
                : tokens.text2;
    final background = !_enabled
        ? Colors.transparent
        : _pressed
            ? tokens.raised
            : _hover
                ? tokens.raised.withValues(alpha: 0.7)
                : Colors.transparent;
    final hasLabel = widget.label.isNotEmpty;
    final labelStyle = text.labelMedium?.copyWith(color: foreground);
    final label = widget.richLabel != null
        ? Text.rich(widget.richLabel!,
            maxLines: 1, overflow: TextOverflow.ellipsis, style: labelStyle)
        : Text(widget.label,
            maxLines: 1, overflow: TextOverflow.ellipsis, style: labelStyle);
    // The pill grows with the text scale; its label ellipsizes when the
    // action has a width to fit (in a Row it is sized by its text).
    final pill = LayoutBuilder(
      builder: (context, constraints) => AnimatedContainer(
        duration: SonderMotion.of(context, SonderMotion.fast),
        curve: SonderMotion.standard,
        constraints: BoxConstraints(minHeight: 28, minWidth: hasLabel ? 0 : 28),
        padding: hasLabel
            ? const EdgeInsets.symmetric(horizontal: SonderSpace.sm)
            : EdgeInsets.zero,
        decoration: BoxDecoration(
          color: background,
          borderRadius: BorderRadius.circular(SonderRadius.row),
          border: Border.all(
            color: _focus ? tokens.accentText : Colors.transparent,
            width: 1.5,
          ),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(widget.icon,
                size: 15,
                color: widget.selected
                    ? foreground
                    : (widget.iconColor ?? foreground)),
            if (hasLabel) ...[
              const SizedBox(width: SonderSpace.xs + SonderSpace.xxs),
              if (constraints.hasBoundedWidth)
                Flexible(child: label)
              else
                label,
            ],
            if (widget.trailing != null) ...[
              const SizedBox(width: SonderSpace.xs),
              IconTheme.merge(
                data: IconThemeData(color: foreground, size: 16),
                child: widget.trailing!,
              ),
            ],
          ],
        ),
      ),
    );
    final name = widget.semanticLabel ?? widget.tooltip ?? widget.label;
    Widget result = Semantics(
      button: true,
      enabled: _enabled,
      selected: widget.selected,
      label: name,
      onTap: _enabled ? _activate : null,
      excludeSemantics: true,
      child: FocusableActionDetector(
        enabled: _enabled,
        mouseCursor:
            _enabled ? SystemMouseCursors.click : SystemMouseCursors.basic,
        onShowHoverHighlight: (value) => setState(() => _hover = value),
        onShowFocusHighlight: (value) => setState(() => _focus = value),
        actions: <Type, Action<Intent>>{
          ActivateIntent: CallbackAction<ActivateIntent>(onInvoke: (_) {
            _activate();
            return null;
          }),
        },
        child: GestureDetector(
          behavior: HitTestBehavior.opaque,
          onTap: _enabled ? _activate : null,
          onTapDown: _enabled ? (_) => setState(() => _pressed = true) : null,
          onTapUp: _enabled ? (_) => setState(() => _pressed = false) : null,
          onTapCancel: _enabled ? () => setState(() => _pressed = false) : null,
          child: ConstrainedBox(
            constraints: const BoxConstraints(minWidth: 48, minHeight: 48),
            child: Align(
              alignment: widget.alignment,
              widthFactor: 1,
              heightFactor: 1,
              child: pill,
            ),
          ),
        ),
      ),
    );
    final tooltip = widget.tooltip;
    if (tooltip != null && tooltip.isNotEmpty) {
      result = Tooltip(message: tooltip, child: result);
    }
    return result;
  }
}
