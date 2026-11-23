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
///
/// The fill is a soft pill of the raised tone: full for the selected row,
/// a third as strong under the pointer. Selection is also known by its
/// accent icon and heavier label, which the hover never carries. [inset] pulls the pill in from the hit area,
/// so a 48 px target can draw a lighter row; keyboard focus adds a ring in
/// the accent, drawn on the pill itself.
class HoverSurface extends StatefulWidget {
  final Widget child;
  final VoidCallback? onTap;
  final bool selected;

  /// Whether [selected] is drawn. It is always reported to semantics.
  final bool showSelected;
  final BorderRadius borderRadius;

  /// Space between the hit area and the painted pill.
  final EdgeInsets inset;
  final String? semanticLabel;
  final FocusNode? focusNode;

  const HoverSurface({
    super.key,
    required this.child,
    this.onTap,
    this.selected = false,
    this.showSelected = true,
    this.borderRadius = const BorderRadius.all(Radius.circular(SonderRadius.row)),
    this.inset = EdgeInsets.zero,
    this.semanticLabel,
    this.focusNode,
  });

  @override
  State<HoverSurface> createState() => _HoverSurfaceState();
}

class _HoverSurfaceState extends State<HoverSurface> {
  bool _hover = false;
  bool _focus = false;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final background = widget.selected && widget.showSelected
        ? tokens.raised
        : _hover && widget.onTap != null
            ? tokens.raised.withValues(alpha: 0.35)
            : Colors.transparent;
    final reduced = MediaQuery.disableAnimationsOf(context);
    final pressedOverlay = WidgetStateProperty.resolveWith<Color?>((states) {
      if (states.contains(WidgetState.pressed)) {
        return tokens.accent.withValues(alpha: 0.28);
      }
      return Colors.transparent;
    });
    final pill = Padding(
      padding: widget.inset,
      child: AnimatedContainer(
        duration: SonderMotion.of(context, SonderMotion.fast),
        curve: SonderMotion.standard,
        decoration: BoxDecoration(
          color: background,
          borderRadius: widget.borderRadius,
        ),
        foregroundDecoration: BoxDecoration(
          borderRadius: widget.borderRadius,
          border: Border.all(
            color: _focus ? tokens.accentText : Colors.transparent,
            width: 1.5,
          ),
        ),
        child: widget.onTap == null ? widget.child : null,
      ),
    );
    if (widget.onTap == null) return pill;
    return Semantics(
      button: true,
      selected: widget.selected,
      label: widget.semanticLabel,
      child: MouseRegion(
        onEnter: (_) => setState(() => _hover = true),
        onExit: (_) => setState(() => _hover = false),
        child: Stack(
          children: [
            Positioned.fill(child: pill),
            Material(
              type: MaterialType.transparency,
              child: InkWell(
                focusNode: widget.focusNode,
                borderRadius: widget.borderRadius,
                splashFactory: reduced ? NoSplash.splashFactory : null,
                // Keep hover/focus pills clean while preserving Material 3's
                // InkSparkle burst on press, painted above the pill fill while
                // keeping the full outer tap target (48 px).
                overlayColor: pressedOverlay,
                onFocusChange: (focused) {
                  // Only keyboard focus draws the ring, as with every control.
                  final keyboard = FocusManager.instance.highlightMode ==
                      FocusHighlightMode.traditional;
                  final show = focused && keyboard;
                  if (show != _focus) setState(() => _focus = show);
                },
                onTap: widget.onTap,
                child: Padding(
                  padding: widget.inset,
                  child: widget.child,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
