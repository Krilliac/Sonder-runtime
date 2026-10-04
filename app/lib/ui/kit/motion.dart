import 'package:flutter/material.dart';

import '../../theme.dart';

/// Cross-fades between children with a short rise, keyed by [child]'s key.
///
/// Use for content that swaps in place (a category page, a status word, a
/// loaded list replacing its skeleton). Reduced motion swaps instantly.
class SonderSwitcher extends StatelessWidget {
  final Widget child;
  final Duration duration;

  /// How far (in logical pixels) the incoming child rises as it fades in.
  final double rise;
  final AlignmentGeometry alignment;

  const SonderSwitcher({
    super.key,
    required this.child,
    this.duration = SonderMotion.medium,
    this.rise = 6,
    this.alignment = Alignment.topLeft,
  });

  @override
  Widget build(BuildContext context) {
    return AnimatedSwitcher(
      duration: SonderMotion.of(context, duration),
      reverseDuration: SonderMotion.of(context, SonderMotion.fast),
      switchInCurve: SonderMotion.enter,
      switchOutCurve: SonderMotion.exit,
      layoutBuilder: (current, previous) => Stack(
        alignment: alignment,
        children: [...previous, if (current != null) current],
      ),
      transitionBuilder: (child, animation) {
        final offset = Tween<Offset>(
          begin: Offset(0, rise),
          end: Offset.zero,
        ).animate(animation);
        return FadeTransition(
          opacity: animation,
          child: AnimatedBuilder(
            animation: offset,
            builder: (context, child) =>
                Transform.translate(offset: offset.value, child: child),
            child: child,
          ),
        );
      },
      child: child,
    );
  }
}

/// Shows or hides [child] with a size-and-fade animation, so content that
/// appears under a row (a result, an error, extra options) slides open
/// instead of jumping the layout.
class SonderReveal extends StatelessWidget {
  final bool visible;
  final Widget child;
  final Duration duration;

  const SonderReveal({
    super.key,
    required this.visible,
    required this.child,
    this.duration = SonderMotion.medium,
  });

  @override
  Widget build(BuildContext context) {
    final d = SonderMotion.of(context, duration);
    return AnimatedSize(
      duration: d,
      curve: SonderMotion.standard,
      alignment: Alignment.topCenter,
      child: AnimatedOpacity(
        duration: d,
        curve: SonderMotion.standard,
        opacity: visible ? 1 : 0,
        child: visible ? child : const SizedBox(width: double.infinity),
      ),
    );
  }
}

/// Paints a hover/press surface behind [child] on pointer devices and keeps
/// keyboard focus visible, without changing layout. Use for list rows and
/// tiles that are tappable as a whole.
class HoverSurface extends StatefulWidget {
  final Widget child;
  final VoidCallback? onTap;
  final bool selected;
  final BorderRadius borderRadius;
  final String? semanticLabel;
  final FocusNode? focusNode;

  const HoverSurface({
    super.key,
    required this.child,
    this.onTap,
    this.selected = false,
    this.borderRadius = const BorderRadius.all(Radius.circular(SonderRadius.row)),
    this.semanticLabel,
    this.focusNode,
  });

  @override
  State<HoverSurface> createState() => _HoverSurfaceState();
}

class _HoverSurfaceState extends State<HoverSurface> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final background = widget.selected
        ? tokens.raised
        : _hover && widget.onTap != null
            ? tokens.raised.withValues(alpha: 0.6)
            : Colors.transparent;
    final surface = AnimatedContainer(
      duration: SonderMotion.of(context, SonderMotion.fast),
      curve: SonderMotion.standard,
      decoration: BoxDecoration(
        color: background,
        borderRadius: widget.borderRadius,
      ),
      child: widget.child,
    );
    if (widget.onTap == null) return surface;
    return Semantics(
      button: true,
      selected: widget.selected,
      label: widget.semanticLabel,
      child: MouseRegion(
        onEnter: (_) => setState(() => _hover = true),
        onExit: (_) => setState(() => _hover = false),
        child: Material(
          type: MaterialType.transparency,
          child: InkWell(
            focusNode: widget.focusNode,
            borderRadius: widget.borderRadius,
            onTap: widget.onTap,
            child: surface,
          ),
        ),
      ),
    );
  }
}
