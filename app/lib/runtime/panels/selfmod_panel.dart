part of '../runtime_screen.dart';

class _SelfmodPanel extends StatelessWidget {
  final SelfmodInfo info;

  const _SelfmodPanel({required this.info});

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const Key('selfmod-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(label: Text(info.enabled ? 'Enabled' : 'Disabled')),
            Chip(label: Text('Mode: ${info.mode}')),
            Chip(label: Text('${info.active} active')),
            Chip(label: Text('${info.rollbackPoints} rollback points')),
          ],
        ),
        const SizedBox(height: 8),
        Text('Backups: ${info.backupRoot}'),
        if (info.runs.isNotEmpty) ...[
          const SizedBox(height: 8),
          ...info.runs.take(5).map((run) => Text(
                '${run['id']}  ${run['phase']}  ${run['risk']}\n${run['objective']}',
              )),
        ],
        const SizedBox(height: 8),
        const Text(
          'Inspect: /selfmod status · /selfmod diff <id> · '
          '/selfmod tests <id> · /selfmod rollback <id>',
        ),
      ],
    );
  }
}
