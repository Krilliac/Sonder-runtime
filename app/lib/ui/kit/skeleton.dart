import 'package:flutter/material.dart';

import '../../theme.dart';

/// A placeholder block that shimmers gently while content loads, so a page
/// keeps its shape instead of collapsing to a centred spinner. Reduced
/// motion draws it static. Announced once as [semanticLabel].
class Skeleton extends StatefulWidget {
  final double? width;
  final double height;
  final double radius;

  const Skeleton({
    super.key,
    this.width,
    this.height = 14,
    this.radius = SonderRadius.control,
  });

  @override
  State<Skeleton> createState() => _SkeletonState();
}

class _SkeletonState extends State<Skeleton>
    with SingleTickerProviderStateMixin {
  late final AnimationController _shimmer = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1400),
  );

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final still = MediaQuery.maybeDisableAnimationsOf(context) == true;
    if (still) {
      _shimmer.stop();
    } else if (!_shimmer.isAnimating) {
      _shimmer.repeat();
    }
  }

  @override
  void dispose() {
    _shimmer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final base = tokens.raised;
    final highlight = Color.lerp(tokens.raised, tokens.hairlineStrong, 0.18)!;
    return ExcludeSemantics(
      child: AnimatedBuilder(
        animation: _shimmer,
        builder: (context, _) {
          final t = _shimmer.value;
          return Container(
            width: widget.width,
            height: widget.height,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(widget.radius),
              gradient: LinearGradient(
                begin: Alignment(-1.0 + 2.5 * t - 0.6, 0),
                end: Alignment(-1.0 + 2.5 * t + 0.6, 0),
                colors: [base, highlight, base],
                stops: const [0.0, 0.5, 1.0],
              ),
            ),
          );
        },
      ),
    );
  }
}

/// A few skeleton rows shaped like a settings or list section.
class SkeletonRows extends StatelessWidget {
  final int rows;
  final String semanticLabel;

  const SkeletonRows({
    super.key,
    this.rows = 3,
    this.semanticLabel = 'Loading',
  });

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: semanticLabel,
      liveRegion: true,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (var i = 0; i < rows; i++)
            Padding(
              padding: const EdgeInsets.symmetric(
                  horizontal: SonderSpace.lg, vertical: SonderSpace.md),
              child: Row(children: [
                Expanded(
                  flex: 3,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Skeleton(width: 120.0 + (i % 3) * 40, height: 13),
                      const SizedBox(height: SonderSpace.sm),
                      Skeleton(width: 200.0 + (i % 2) * 60, height: 10),
                    ],
                  ),
                ),
                const SizedBox(width: SonderSpace.lg),
                const Skeleton(width: 64, height: 22, radius: SonderRadius.row),
              ]),
            ),
        ],
      ),
    );
  }
}

/// A calm empty state: icon, one-line title, a sentence, and an action.
class EmptyState extends StatelessWidget {
  final IconData icon;
  final String title;
  final String? message;
  final Widget? action;

  const EmptyState({
    super.key,
    required this.icon,
    required this.title,
    this.message,
    this.action,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.xxl, vertical: SonderSpace.x3),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              color: tokens.raised,
              borderRadius: BorderRadius.circular(SonderRadius.sheet),
            ),
            child: Icon(icon, size: 22, color: tokens.text2),
          ),
          const SizedBox(height: SonderSpace.md),
          Text(title,
              textAlign: TextAlign.center,
              style: text.titleSmall?.copyWith(fontWeight: FontWeight.w600)),
          if (message != null) ...[
            const SizedBox(height: SonderSpace.xs),
            Text(message!, textAlign: TextAlign.center, style: text.bodySmall),
          ],
          if (action != null) ...[
            const SizedBox(height: SonderSpace.lg),
            action!,
          ],
        ],
      ),
    );
  }
}
