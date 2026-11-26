import 'package:flutter/material.dart';

import '../models.dart';
import '../theme.dart';

/// Compact execution-route pill badge rendered above an assistant turn when
/// `sonder_receipt.execution_route` is present.
///
/// Examples:
/// - `Auto · Hard · Ensemble (code+reasoning)`
/// - `Auto · Quick · fast`
/// - `Manual · Standard · Fanout (code+reasoning)`
///
/// Hovering (or long-pressing on touch) reveals a tooltip with difficulty score
/// and routing reason; tapping opens a details dialog with full routing fields.
class ExecutionRouteBadge extends StatelessWidget {
  final ExecutionRoute route;

  const ExecutionRouteBadge({super.key, required this.route});

  IconData _icon() => switch (route.mode) {
        'ensemble' => Icons.hub_outlined,
        'fanout' => Icons.call_split_rounded,
        'agent' => Icons.smart_toy_outlined,
        'single' => route.band == 'quick'
            ? Icons.bolt_outlined
            : Icons.alt_route_rounded,
        _ => Icons.alt_route_rounded,
      };

  Color _accentColor(SonderTokens tokens) => switch (route.mode) {
        'ensemble' => tokens.accentText,
        'fanout' => tokens.info,
        'agent' => tokens.warn,
        'single' => route.band == 'quick' ? tokens.ok : tokens.text2,
        _ => tokens.text2,
      };

  Color _borderColor(SonderTokens tokens) => switch (route.mode) {
        'ensemble' => tokens.accent.withValues(alpha: 0.40),
        'fanout' => tokens.info.withValues(alpha: 0.40),
        'agent' => tokens.warn.withValues(alpha: 0.45),
        'single' => route.band == 'quick'
            ? tokens.ok.withValues(alpha: 0.40)
            : tokens.hairlineStrong,
        _ => tokens.hairlineStrong,
      };

  Color _fillColor(SonderTokens tokens) => switch (route.mode) {
        'ensemble' => tokens.accent.withValues(alpha: 0.08),
        'fanout' => tokens.info.withValues(alpha: 0.08),
        'agent' => tokens.warn.withValues(alpha: 0.08),
        'single' => route.band == 'quick'
            ? tokens.ok.withValues(alpha: 0.08)
            : tokens.panel,
        _ => tokens.panel,
      };

  void _showDetails(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        key: const Key('execution-route-details-dialog'),
        title: Row(
          children: [
            Icon(_icon(), size: 18, color: _accentColor(tokens)),
            const SizedBox(width: SonderSpace.sm),
            const Expanded(child: Text('Execution route')),
          ],
        ),
        content: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _DetailRow(
                label: 'Selection',
                value: route.autoSelected ? 'Auto router' : 'Manual override',
              ),
              if (route.modeLabel.isNotEmpty)
                _DetailRow(label: 'Mode', value: route.modeLabel),
              if (route.bandLabel.isNotEmpty)
                _DetailRow(label: 'Difficulty band', value: route.bandLabel),
              if (route.scoreText.isNotEmpty)
                _DetailRow(label: 'Difficulty score', value: route.scoreText),
              if (route.recommendedTiers.isNotEmpty)
                _DetailRow(
                  label: 'Recommended tiers',
                  value: route.recommendedTiers.join(', '),
                ),
              if (route.ensembleMode.isNotEmpty && route.mode == 'ensemble')
                _DetailRow(label: 'Ensemble style', value: route.ensembleMode),
              if (route.fanoutScope.isNotEmpty && route.mode == 'fanout')
                _DetailRow(label: 'Fanout scope', value: route.fanoutScope),
              if (route.signals.isNotEmpty)
                _DetailRow(
                  label: 'Signals',
                  value: route.signals.join(', '),
                ),
              if (route.reason.isNotEmpty) ...[
                const SizedBox(height: SonderSpace.sm),
                Text(
                  'Reason',
                  style: text.labelSmall?.copyWith(color: tokens.muted),
                ),
                const SizedBox(height: SonderSpace.xs),
                SelectableText(
                  route.reason,
                  style: text.bodySmall?.copyWith(color: tokens.text),
                ),
              ],
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final label = route.compactLabel;
    if (label.isEmpty) return const SizedBox.shrink();
    final color = _accentColor(tokens);
    return Semantics(
      container: true,
      button: true,
      label: route.semanticLabel,
      hint: 'Show execution route details',
      child: Tooltip(
        key: const Key('execution-route-tooltip'),
        message: route.tooltipText,
        child: Material(
          color: Colors.transparent,
          child: InkWell(
            key: const Key('execution-route-badge'),
            onTap: () => _showDetails(context),
            borderRadius: BorderRadius.circular(SonderRadius.pill),
            child: Container(
              padding: const EdgeInsets.symmetric(
                horizontal: SonderSpace.sm,
                vertical: SonderSpace.xs,
              ),
              decoration: BoxDecoration(
                color: _fillColor(tokens),
                borderRadius: BorderRadius.circular(SonderRadius.pill),
                border: Border.all(color: _borderColor(tokens)),
              ),
              child: ExcludeSemantics(
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(_icon(), size: 13, color: color),
                    const SizedBox(width: SonderSpace.tight),
                    Flexible(
                      child: Text(
                        label,
                        key: const Key('execution-route-label'),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: tokens.mono(
                          11,
                          color: color,
                          weight: FontWeight.w500,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _DetailRow extends StatelessWidget {
  final String label;
  final String value;

  const _DetailRow({required this.label, required this.value});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.only(bottom: SonderSpace.xs),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 132,
            child: Text(
              label,
              style: text.labelSmall?.copyWith(color: tokens.muted),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: tokens.mono(12, color: tokens.text),
            ),
          ),
        ],
      ),
    );
  }
}
