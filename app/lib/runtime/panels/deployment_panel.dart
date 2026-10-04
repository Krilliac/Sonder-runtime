part of '../runtime_screen.dart';

/// The deployment profile: members, the preferred primary, and the honest
/// limits of recovery. Each capability states its reason; unavailable reads
/// `– off`, never red.
class _DeploymentPanel extends StatelessWidget {
  final DeploymentInfo info;

  const _DeploymentPanel({super.key, required this.info});

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final policy = info.partitionPolicy.replaceAll('_', ' ');
    CapabilityRow row(String label, String name) {
      final capability = info.capability(name);
      return CapabilityRow(
        label: label,
        available: capability.available,
        reason: capability.reason,
      );
    }

    final posture = info.recoveryPosture;
    return SettingsSection(
      key: const Key('deployment-panel-content'),
      title: 'Deployment profile',
      description: 'How this runtime shares work and state with other PCs.',
      children: [
        ValueRow(
          label: 'Profile',
          value: info.profile.isEmpty
              ? info.displayProfile
              : '${info.displayProfile} (${info.profile})',
        ),
        ValueRow(label: 'Members', value: info.membersLabel),
        if (info.localNode.isNotEmpty)
          ValueRow(label: 'Local node', value: info.localNode, mono: true),
        if (info.preferredPrimary.isNotEmpty)
          ValueRow(
              label: 'Preferred primary',
              value: info.preferredPrimary,
              mono: true),
        row('Private compute', 'private_compute'),
        row('Automatic takeover', 'automatic_takeover'),
        row('Automatic failback', 'automatic_failback'),
        if (posture != null)
          CapabilityRow(
            label: 'Recovery posture',
            available: posture.automaticTakeoverAvailable &&
                posture.automaticFailbackAvailable,
            reason: posture.summary,
          ),
        row('State replication', 'acknowledged_state_replication'),
        row('Worker fencing', 'worker_epoch_fencing'),
        row('Quorum', 'quorum'),
        if (info.controlStateScope.isNotEmpty)
          ValueRow(
              label: 'State scope', value: info.controlStateScope, mono: true),
        if (policy.isNotEmpty || !info.preferenceConfersAuthority)
          RuntimeCardBody(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (policy.isNotEmpty)
                  Text('Partition policy: $policy.', style: text.bodySmall),
                if (!info.preferenceConfersAuthority) ...[
                  if (policy.isNotEmpty) const SizedBox(height: SonderSpace.xs),
                  Text(
                    'Primary preference is advisory; it never grants '
                    'promotion authority.',
                    style: text.bodySmall,
                  ),
                ],
              ],
            ),
          ),
      ],
    );
  }
}
