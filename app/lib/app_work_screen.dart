import 'package:flutter/material.dart';
import 'app_control.dart';
import 'workspace_ui.dart';

class AppWorkScreen extends StatefulWidget {
  final AppControlClient client;
  const AppWorkScreen({super.key, required this.client});
  @override
  State<AppWorkScreen> createState() => _AppWorkScreenState();
}

class _AppWorkScreenState extends State<AppWorkScreen> {
  final _prompt = TextEditingController();
  final _focus = FocusNode();
  String? _notice;
  late int _generation;
  bool _leave = false;
  AppControlClient get client => widget.client;
  @override
  void initState() {
    super.initState();
    _generation = client.contextRevision;
    _prompt.text = client.workPrompt;
    client.addListener(_changed);
    _prompt.addListener(_changed);
  }

  void _changed() {
    if (!mounted) return;
    if (_generation != client.contextRevision) {
      _generation = client.contextRevision;
      _prompt.clear();
      _notice =
          'App control changed. Return to server conversations to reconnect.';
    }
    setState(() {});
  }

  @override
  void dispose() {
    client.removeListener(_changed);
    _prompt.dispose();
    _focus.dispose();
    super.dispose();
  }

  Future<void> _perform(Future<void> Function() action) async {
    setState(() => _notice = null);
    try {
      await action();
    } on AppControlFailure catch (error) {
      if (!mounted) return;
      setState(() => _notice = switch (error.code) {
            'APP_WORK_APPROVAL_PENDING' =>
              'Host approval is required before this work can run.',
            'APP_WORK_UNAVAILABLE' ||
            'APP_CONTROL_ROUTE_NOT_FOUND' =>
              'Managed work is not available on this server. No execution was confirmed.',
            'APP_WORK_APPROVAL_UNKNOWN' =>
              'The approval outcome is unknown. Check status; do not submit another run.',
            'WORK_SELECTION_CHANGED' =>
              'The selected conversation changed. This work cannot be controlled from the new selection.',
            'INVALID_REQUEST' => 'Enter a task of at most 8,000 UTF-8 bytes.',
            'APP_CONTROL_AUTH_REQUIRED' ||
            'SESSION_REQUIRED' ||
            'CONTEXT_REQUIRED' =>
              'Your control session is unavailable. Return to server conversations.',
            'APP_CONTROL_CONFLICT' ||
            'APP_CONTROL_GRANT_CHANGED' =>
              'Server authority changed. Refresh your conversation selection before continuing.',
            _ => error.unknown
                ? 'The request outcome is unknown. Keep this screen open and check the same request or status.'
                : 'The server could not confirm this request. Review the current session and try again.'
          });
      if (error.code == 'INVALID_REQUEST') _focus.requestFocus();
    } catch (_) {
      if (mounted) {
        setState(() => _notice =
            'The request could not be confirmed. Check status before attempting another action.');
      }
    }
  }

  Future<void> _back(bool didPop) async {
    if (didPop) return;
    final leave = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
                title: const Text('Discard this draft?'),
                content: const Text(
                    'This task has not been prepared. Its draft is only held on this screen.'),
                actions: [
                  TextButton(
                      onPressed: () => Navigator.pop(context, false),
                      child: const Text('Keep editing')),
                  FilledButton(
                      onPressed: () => Navigator.pop(context, true),
                      child: const Text('Discard draft'))
                ]));
    if (leave == true && mounted) {
      setState(() => _leave = true);
      Navigator.pop(context);
    }
  }

  @override
  Widget build(BuildContext context) {
    final work = client.work;
    final pending = client.workApproval;
    final available = client.hasSession &&
        client.selectionKnown &&
        client.selection?.bindingId != null;
    final locked = work != null || client.workPreparationPending;
    final status = client.workExecutionUnknown && work?.state == 'prepared'
        ? 'Execution outcome unknown'
        : switch (work?.state) {
            'prepared' => 'Prepared · not started',
            'admitted' || 'run_binding' => 'Accepted · waiting for host',
            'running' => 'Running',
            'verification_pending' => 'Verification needs approval',
            'unknown' => 'Outcome unknown',
            'terminal' => 'Host turn recorded',
            _ => 'Prepare a task'
          };
    return PopScope(
        canPop: _leave || _prompt.text.isEmpty || locked,
        onPopInvokedWithResult: (didPop, _) => _back(didPop),
        child: Scaffold(
            appBar: AppBar(title: const Text('Managed work')),
            body: SafeArea(
                child: Align(
                    alignment: Alignment.topCenter,
                    child: ConstrainedBox(
                        constraints:
                            const BoxConstraints(maxWidth: conversationWidth),
                        child: ListView(
                            padding: const EdgeInsets.all(20),
                            children: [
                              SizedBox(
                                  height: 4,
                                  child: client.busy
                                      ? const LinearProgressIndicator()
                                      : null),
                              const SizedBox(height: 20),
                              Text(status,
                                  style: Theme.of(context)
                                      .textTheme
                                      .headlineSmall),
                              const SizedBox(height: 8),
                              Text(
                                  '${client.project ?? 'No project'} · ${client.origin ?? 'Server unavailable'}',
                                  style: Theme.of(context).textTheme.bodySmall),
                              const SizedBox(height: 20),
                              const WorkspaceNotice(
                                  message:
                                      'Prepare first, then explicitly run on the selected server conversation. Server support and host approval are required.'),
                              if (_notice != null) ...[
                                const SizedBox(height: 12),
                                WorkspaceNotice(
                                    message: _notice!, tone: NoticeTone.warning)
                              ],
                              if (!available ||
                                  locked && !client.workScopeCurrent) ...[
                                const SizedBox(height: 12),
                                const WorkspaceNotice(
                                    message:
                                        'This selection is no longer available. Return to server conversations. Reconnecting does not automatically resume this work.',
                                    tone: NoticeTone.warning)
                              ],
                              const SizedBox(height: 24),
                              TextField(
                                  key: const Key('work-prompt'),
                                  controller: _prompt,
                                  focusNode: _focus,
                                  readOnly: locked,
                                  enabled: available && !client.busy,
                                  minLines: 5,
                                  maxLines: 12,
                                  decoration: InputDecoration(
                                      labelText: 'Task',
                                      hintText:
                                          'Describe the repository work to perform',
                                      helperText: locked
                                          ? 'Original task sent for preparation.'
                                          : 'Your draft stays in this app until you prepare it.')),
                              const SizedBox(height: 16),
                              Text(
                                  'Automatic model route · 8 steps · web and location off',
                                  style: Theme.of(context).textTheme.bodySmall),
                              const SizedBox(height: 20),
                              Wrap(spacing: 12, runSpacing: 12, children: [
                                if (!locked)
                                  FilledButton(
                                      onPressed: !client.busy && available
                                          ? () => _perform(() =>
                                              client.prepareWork(
                                                  prompt: _prompt.text))
                                          : null,
                                      child: const Text('Prepare task')),
                                if (client.workPreparationPending)
                                  OutlinedButton(
                                      onPressed: !client.busy &&
                                              client.workScopeCurrent
                                          ? () => _perform(
                                              client.retryWorkPreparation)
                                          : null,
                                      child:
                                          const Text('Check same preparation')),
                                if (work?.state == 'prepared')
                                  FilledButton(
                                      onPressed: !client.busy &&
                                              client.workScopeCurrent &&
                                              !client.workExecutionUnknown
                                          ? () => _perform(client.executeWork)
                                          : null,
                                      child: Text(pending == null
                                          ? 'Run prepared task'
                                          : 'Retry after host approval')),
                                if (work != null)
                                  OutlinedButton.icon(
                                      onPressed: !client.busy &&
                                              client.workScopeCurrent
                                          ? () => _perform(client.refreshWork)
                                          : null,
                                      icon: const Icon(Icons.refresh),
                                      label: const Text('Refresh status')),
                              ]),
                              if (pending != null) ...[
                                const SizedBox(height: 24),
                                const Divider(),
                                const Text(
                                    'Ask the host operator to review this exact approval request. This app cannot grant approval.'),
                                const SizedBox(height: 8),
                                SelectableText(pending.callId)
                              ],
                              if (client.workExecutionUnknown) ...[
                                const SizedBox(height: 16),
                                const WorkspaceNotice(
                                    message:
                                        'Execution may have been admitted. Only status checks are available here; another run will not be submitted.',
                                    tone: NoticeTone.warning)
                              ],
                              if (work != null) ...[
                                const SizedBox(height: 24),
                                const Divider(),
                                Text('Server reference',
                                    style:
                                        Theme.of(context).textTheme.titleSmall),
                                const SizedBox(height: 8),
                                SelectableText(work.id),
                                const SizedBox(height: 16),
                                WorkspaceNotice(
                                    message: work.state ==
                                            'verification_pending'
                                        ? 'Verification approval is pending. Its status is not permission to resume. App recovery is not available in this workflow.'
                                        : 'This view shows server status only. A recorded host turn does not confirm success. Task output is not available in this view.'),
                              ],
                            ]))))));
  }
}
