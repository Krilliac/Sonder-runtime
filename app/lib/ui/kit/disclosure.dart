import 'package:flutter/material.dart';

import '../../theme.dart';
import 'motion.dart';

/// A titled row that opens to reveal secondary content: details, or a list
/// that should not load (or take room) until someone asks for it. The body
/// is built only while open, so [onChanged] can start a lazy read.
///
/// The header is one 48 dp target that says whether it is open; [trailing]
/// (a refresh button, a count) sits beside it, outside the toggle.
class Disclosure extends StatefulWidget {
  final String title;

  /// One line under the title, e.g. what opening it will load.
  final String? subtitle;
  final IconData? icon;
  final Widget? trailing;
  final bool initiallyOpen;

  /// Called after each toggle with the new state.
  final ValueChanged<bool>? onChanged;
  final Widget child;

  const Disclosure({
    super.key,
    required this.title,
    required this.child,
    this.subtitle,
    this.icon,
    this.trailing,
    this.initiallyOpen = false,
    this.onChanged,
  });

  @override
  State<Disclosure> createState() => _DisclosureState();
}

class _DisclosureState extends State<Disclosure> {
  late bool _open = widget.initiallyOpen;

  void _toggle() {
    setState(() => _open = !_open);
    widget.onChanged?.call(_open);
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final header = HoverSurface(
      onTap: _toggle,
      borderRadius: BorderRadius.zero,
      semanticLabel: '${widget.title}, ${_open ? 'expanded' : 'collapsed'}',
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 48),
        child: Padding(
          padding: const EdgeInsets.symmetric(
              horizontal: SonderSpace.lg, vertical: SonderSpace.md),
          child: ExcludeSemantics(
            child: Row(children: [
              if (widget.icon != null) ...[
                Icon(widget.icon, size: 18, color: tokens.text2),
                const SizedBox(width: SonderSpace.md),
              ],
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(widget.title,
                        style: text.bodyMedium
                            ?.copyWith(fontWeight: FontWeight.w500)),
                    if (widget.subtitle != null)
                      Padding(
                        padding: const EdgeInsets.only(top: SonderSpace.xxs),
                        child: Text(widget.subtitle!, style: text.bodySmall),
                      ),
                  ],
                ),
              ),
              const SizedBox(width: SonderSpace.sm),
              AnimatedRotation(
                turns: _open ? 0.25 : 0,
                duration: SonderMotion.of(context, SonderMotion.fast),
                curve: SonderMotion.standard,
                child: Icon(Icons.chevron_right, size: 20, color: tokens.text2),
              ),
            ]),
          ),
        ),
      ),
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(children: [
          Expanded(child: header),
          if (widget.trailing != null) ...[
            Padding(
              padding: const EdgeInsets.only(right: SonderSpace.xs),
              child: widget.trailing!,
            ),
          ],
        ]),
        SonderReveal(visible: _open, child: widget.child),
      ],
    );
  }
}
