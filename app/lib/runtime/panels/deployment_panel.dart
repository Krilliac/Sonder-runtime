part of '../runtime_screen.dart';

class _DeploymentPanel extends StatelessWidget {
  final DeploymentInfo info;

  const _DeploymentPanel({super.key, required this.info});

  String _capabilityValue(DeploymentCapabilityInfo capability) {
    if (capability.available) {
      return capability.reason.isEmpty
          ? 'Available'
          : 'Available — ${capability.reason}';
    }
    return capability.reason.isEmpty
        ? 'Unavailable'
        : 'Unavailable — ${capability.reason}';
  }

  @override
  Widget build(BuildContext context) {
    final privateCompute = info.capability('private_compute');
    final takeover = info.capability('automatic_takeover');
    final failback = info.capability('automatic_failback');
    final replication = info.capability('acknowledged_state_replication');
    final fencing = info.capability('worker_epoch_fencing');
    final quorum = info.capability('quorum');
    final policy = info.partitionPolicy.replaceAll('_', ' ');
    return Column(
      key: const Key('deployment-panel-content'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _StatusRow(
          label: 'Profile',
          value: info.profile.isEmpty
              ? info.displayProfile
              : '${info.displayProfile} (${info.profile})',
          ok: info.profile.isNotEmpty || info.profileId.isNotEmpty,
        ),
        _StatusRow(
          label: 'Members',
          value: info.membersLabel,
          ok: info.configuredMembers.isNotEmpty,
        ),
        if (info.localNode.isNotEmpty)
          _StatusRow(label: 'Local node', value: info.localNode, ok: true),
        if (info.preferredPrimary.isNotEmpty)
          _StatusRow(
            label: 'Preferred primary',
            value: info.preferredPrimary,
            ok: true,
          ),
        _StatusRow(
          label: 'Private compute',
          value: _capabilityValue(privateCompute),
          ok: privateCompute.available,
          off: true,
        ),
        _StatusRow(
          label: 'Automatic takeover',
          value: _capabilityValue(takeover),
          ok: takeover.available,
          off: true,
        ),
        _StatusRow(
          label: 'Automatic failback',
          value: _capabilityValue(failback),
          ok: failback.available,
          off: true,
        ),
        if (info.recoveryPosture != null)
          _StatusRow(
            label: 'Recovery posture',
            value: info.recoveryPosture!.summary,
            ok: info.recoveryPosture!.automaticTakeoverAvailable &&
                info.recoveryPosture!.automaticFailbackAvailable,
          ),
        _StatusRow(
          label: 'State replication',
          value: _capabilityValue(replication),
          ok: replication.available,
          off: true,
        ),
        _StatusRow(
          label: 'Worker fencing',
          value: _capabilityValue(fencing),
          ok: fencing.available,
          off: true,
        ),
        _StatusRow(
          label: 'Quorum',
          value: _capabilityValue(quorum),
          ok: quorum.available,
          off: true,
        ),
        if (info.controlStateScope.isNotEmpty)
          _StatusRow(
            label: 'State scope',
            value: info.controlStateScope,
            ok: true,
          ),
        if (policy.isNotEmpty) ...[
          const SizedBox(height: 4),
          Text(
            'Partition policy: $policy.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
        if (!info.preferenceConfersAuthority) ...[
          const SizedBox(height: 4),
          Text(
            'Primary preference is advisory; it never grants promotion authority.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ],
    );
  }
}
