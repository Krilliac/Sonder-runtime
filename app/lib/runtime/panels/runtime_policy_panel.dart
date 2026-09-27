part of '../runtime_screen.dart';

class _RuntimePolicyPanel extends StatelessWidget {
  final RuntimePolicyInfo policy;

  const _RuntimePolicyPanel({required this.policy});

  static const _tiers = ['fast', 'code', 'general'];
  static const _lanes = [
    'router',
    'workbench',
    'autopilot',
    'fleet',
    'review',
  ];

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final warnings = <String>[
      if (policy.error.isNotEmpty) '${policy.error} (safe defaults are active)',
      if (policy.inventoryError.isNotEmpty)
        'Model inventory unavailable: ${policy.inventoryError}',
      if (policy.missingModels.isNotEmpty)
        'Missing local models: ${policy.missingModels.join(', ')}',
    ];
    return Column(
      key: const Key('runtime-policy-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            Chip(
              avatar: Icon(
                policy.hasWarning
                    ? Icons.warning_amber_outlined
                    : Icons.sync_outlined,
                size: 18,
                color: policy.hasWarning ? cs.error : cs.primary,
              ),
              label: Text('Shared policy r${policy.revision}'),
            ),
            if (policy.source.isNotEmpty)
              Chip(
                avatar: const Icon(Icons.history_outlined, size: 18),
                label: Text(policy.source),
              ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          'Local model aliases',
          style: Theme.of(context).textTheme.labelLarge,
        ),
        const SizedBox(height: 6),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final tier in _tiers)
              Chip(
                avatar: Icon(_tierIcon(tier), size: 18),
                label:
                    Text('$tier  ${policy.localModels[tier] ?? 'unassigned'}'),
              ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          'Automatic execution lanes',
          style: Theme.of(context).textTheme.labelLarge,
        ),
        const SizedBox(height: 6),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final lane in _lanes)
              Tooltip(
                message: policy.modelForLane(lane).isEmpty
                    ? 'No local model resolved'
                    : policy.modelForLane(lane),
                child: Chip(
                  avatar: const Icon(Icons.route_outlined, size: 18),
                  label: Text('$lane  ${policy.routing[lane] ?? 'unassigned'}'),
                ),
              ),
          ],
        ),
        if (warnings.isNotEmpty) ...[
          const SizedBox(height: 12),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: cs.errorContainer,
              borderRadius: BorderRadius.circular(10),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.warning_amber_outlined,
                    size: 20, color: cs.onErrorContainer),
                const SizedBox(width: 8),
                Expanded(
                  child: SelectableText(
                    warnings.join('\n'),
                    style: TextStyle(color: cs.onErrorContainer),
                  ),
                ),
              ],
            ),
          ),
        ],
        if (policy.path.isNotEmpty) ...[
          const SizedBox(height: 12),
          SelectableText(
            'Policy file: ${policy.path}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
        const SizedBox(height: 6),
        Text(
          'Guarded edits: /runtime set workbench=general',
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ],
    );
  }

  IconData _tierIcon(String tier) {
    if (tier == 'fast') return Icons.bolt_outlined;
    if (tier == 'code') return Icons.terminal_outlined;
    return Icons.psychology_outlined;
  }
}
