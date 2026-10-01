part of '../runtime_screen.dart';

/// Permissions: the approvals queue (actionable), the permission mode, the
/// developer tools the host has, and MCP tool convergence.
class _PermissionsPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _PermissionsPage(this.s);

  @override
  Widget build(BuildContext context) {
    final info = s._info;
    return _PageColumn(children: [
      ApprovalsPanel(
        page: s._approvals,
        error: s._approvalsError,
        loading: s._loadingExtras,
        now: s._now,
        onRefresh: s._loadExtras,
        onApprove: s._approve,
        onRevoke: s._revoke,
        busy: s._approvalBusy,
        approving: s._approving,
        outcomes: s._approvalOutcomes,
        onDismiss: s._dismissApprovalOutcome,
      ),
      _PermissionModeSection(s),
      HostToolsPanel(source: s._data),
      if (info?.mcpRuntime != null)
        _McpRuntimePanel(s: s, runtime: info!.mcpRuntime!),
    ]);
  }
}

/// Risk-class names as the server sends them, in words.
String _riskLabel(String riskClass) =>
    _capitalized(riskClass.replaceAll('_', ' ').replaceAll('-', ' '));

/// What a policy word does, in words.
String _policyWords(String policy) => switch (policy) {
      'allow' => 'Runs without asking',
      'ask' => 'Asks first',
      'deny' => 'Refused',
      final other => other,
    };

/// The permission mode, read-only: what it lets agents do, the privilege
/// axis, the resolved risk matrix, and where the mode is changed. Changing
/// it goes through Chat's one mode flow (picker, raise sheet, one POST);
/// the shell can wire that flow in as [RuntimeScreen.onChangePermissionMode].
class _PermissionModeSection extends StatelessWidget {
  final _RuntimeScreenState s;
  const _PermissionModeSection(this.s);

  @override
  Widget build(BuildContext context) {
    final mode = s._mode;
    final error = s._modeError;
    final shell = ShellScope.maybeOf(context);
    final change = s.widget.onChangePermissionMode;
    final rows = <Widget>[];
    if (!s._modeLoaded) {
      rows.add(s._loadingExtras || s._loading
          ? const SkeletonRows(rows: 2, semanticLabel: 'Loading the mode')
          : const RuntimePanelNote(
              status: StatusKind.unknown, text: 'Not loaded yet.'));
    } else if (error != null) {
      rows.add(RuntimePanelNote(
        status: StatusKind.fail,
        text: error is SonderException
            ? error.message
            : 'Could not read the permission mode.',
        action: TextButton(
            onPressed: s._loadExtras, child: const Text(SonderStrings.retry)),
      ));
    } else if (mode == null) {
      rows.add(const RuntimePanelNote(
          status: StatusKind.skipped,
          word: 'n/a',
          text: 'This server does not publish a permission mode.'));
    } else {
      rows.add(ValueRow(
        key: const Key('permission-mode-current'),
        label: 'Current mode',
        description: mode.blurb.isEmpty ? null : mode.blurb,
        value: mode.displayLabel,
      ));
      rows.add(ValueRow(
        key: const Key('permission-privilege'),
        label: 'Privilege',
        description: mode.elevated
            ? (mode.elevationReason.isEmpty
                ? 'A separate switch: no mode turns it on, and changing '
                    'the mode does not turn it off.'
                : mode.elevationReason)
            : 'Elevation is a separate switch; no mode grants it.',
        value: mode.elevated ? 'Elevated' : 'Normal',
      ));
      final matrix = mode.matrix.entries.toList();
      for (final entry in matrix) {
        rows.add(ValueRow(
          key: Key('permission-risk-${entry.key}'),
          label: _riskLabel(entry.key),
          value: _policyWords(entry.value),
        ));
      }
    }
    rows.add(SettingRow(
      label: 'Change the mode',
      description: change != null
          ? 'Raising it asks you to confirm first.'
          : 'Use the mode chip under the chat composer, Shift+Tab in Chat, '
              'or /mode. Raising it asks you to confirm first.',
      trailing: change != null
          ? OutlinedButton(
              key: const Key('permission-mode-change'),
              onPressed: change,
              child: const Text('Change mode…'),
            )
          : shell != null
              ? OutlinedButton(
                  onPressed: () => shell.navigate(WorkspaceDestination.chat),
                  child: const Text('Open Chat'),
                )
              : null,
    ));
    return SettingsSection(
      key: const Key('permission-mode-section'),
      title: 'Permission mode',
      description: 'How often agents stop to ask, for every chat and agent '
          'on this server.',
      children: rows,
    );
  }
}
