import 'dart:convert';
import 'package:flutter/material.dart';
import 'app_control.dart';
import 'theme.dart';
import 'workspace_ui.dart';

String appControlError(Object error) {
  if (error is! AppControlFailure) {
    return 'The request could not be confirmed. Refresh before continuing.';
  }
  return switch (error.code) {
    'CONTEXT_REQUIRED' ||
    'APP_CONTROL_AUTH_REQUIRED' =>
      'Sign in to this server in Settings, then enable app control again.',
    'CONTEXT_CHANGED' =>
      'The account or server changed. Server conversations was cleared from this app.',
    'SESSION_REQUIRED' =>
      'This app-control session expired or is disconnected. Enter your password to enable it again.',
    'APP_CONTROL_REFUSED' ||
    'APP_CONTROL_TRANSPORT_REFUSED' =>
      'This account or connection cannot use app control. Check your server access.',
    'APP_CONTROL_GRANT_CHANGED' =>
      'Server access changed. Enable app control again with a fresh password check.',
    'CREDENTIAL_DELIVERY_UNKNOWN' =>
      'The server enrolled the session, but its credential cannot be recovered. Enter your password to start a new session. Server limits still apply.',
    'APP_CONTROL_CONFLICT' =>
      'The conversation or selection changed. Refresh, then review the current selection before trying again.',
    'APP_CONTROL_BUSY' ||
    'APP_CONTROL_CAPACITY' =>
      'The server is busy or its session limit is reached. Wait, then retry this request.',
    'APP_BINDING_NOT_FOUND' =>
      'This conversation is no longer available. Refresh the list.',
    'APP_CONTROL_ROUTE_NOT_FOUND' ||
    'APP_CONTROL_UNAVAILABLE' =>
      'Server conversations is unavailable on this server. Check its configuration or refresh later.',
    'INVALID_LABEL' =>
      'Use at most 256 UTF-8 bytes without control characters.',
    'PASSWORD_REQUIRED' => 'Enter your account password.',
    'REFRESH_REQUIRED' => 'Refresh the current selection before changing it.',
    'REQUEST_PENDING' =>
      'A request needs to finish or be reconciled before another change.',
    _ => error.unknown
        ? 'The server outcome is unknown. Check the same request before making another change.'
        : 'The server response could not be read. Refresh or check the server version.',
  };
}

class AppControlScreen extends StatefulWidget {
  final AppControlClient client;
  final String initialProject, localHistoryAlias;
  final VoidCallback? onSettings;
  const AppControlScreen(
      {super.key,
      required this.client,
      this.initialProject = 'default',
      this.localHistoryAlias = '',
      this.onSettings});
  @override
  State<AppControlScreen> createState() => _AppControlScreenState();
}

class _AppControlScreenState extends State<AppControlScreen> {
  final _form = GlobalKey<FormState>();
  final _project = TextEditingController();
  final _password = TextEditingController();
  final _title = TextEditingController();
  bool _showPassword = false, _linkHistory = false, _replacing = false;
  String? _notice;
  bool _warning = false;
  late int _revision;
  AppControlClient get client => widget.client;
  @override
  void initState() {
    super.initState();
    client.synchronize();
    _revision = client.contextRevision;
    _project.text = client.project ?? widget.initialProject;
    client.addListener(_changed);
    if (client.hasSession) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _refresh();
      });
    }
  }

  void _changed() {
    if (!mounted) return;
    if (_revision != client.contextRevision) {
      _revision = client.contextRevision;
      _password.clear();
      _title.clear();
      _replacing = false;
      _notice =
          'Server conversations is disconnected or expired. Enable it again to continue.';
      _warning = true;
    }
    setState(() {});
  }

  @override
  void dispose() {
    client.removeListener(_changed);
    _project.dispose();
    _password.dispose();
    _title.dispose();
    super.dispose();
  }

  Future<void> _perform(Future<void> Function() action,
      {String? success, bool refresh = false}) async {
    setState(() {
      _notice = null;
      _warning = false;
    });
    try {
      await action();
      if (refresh) {
        await client.loadBindings();
        await client.loadSelection();
      }
      if (mounted) {
        setState(() {
          _notice = success;
          _warning = false;
        });
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          _notice = appControlError(error);
          _warning = true;
        });
      }
    }
  }

  Future<void> _refresh() => _perform(() async {
        await client.loadBindings(afterPosition: client.pagePosition);
        await client.loadSelection();
      });
  Future<void> _enable() async {
    if (!_form.currentState!.validate()) return;
    final password = _password.text;
    _password.clear();
    await _perform(() async {
      if (client.enrollmentPending) {
        await client.reconcileEnrollment(password: password);
      } else {
        await client.enroll(
            project: _project.text.trim(),
            password: password,
            replace: _replacing);
      }
      if (mounted) {
        setState(() {
          _replacing = false;
        });
      }
    },
        success: 'Server conversations enabled for this session.',
        refresh: true);
  }

  Future<bool> _confirm(String title, String message, String action) async =>
      await showDialog<bool>(
          context: context,
          builder: (context) =>
              AlertDialog(title: Text(title), content: Text(message), actions: [
                TextButton(
                    autofocus: true,
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('Cancel')),
                FilledButton(
                    onPressed: () => Navigator.pop(context, true),
                    child: Text(action)),
              ])) ??
      false;
  Future<void> _revoke(AppConversationBinding binding) async {
    if (!await _confirm(
        'Revoke ${binding.displayTitle}?',
        'This removes its app-control binding and clears matching selections. It does not cancel independently granted child work.',
        'Revoke conversation')) {
      return;
    }
    if (!mounted) return;
    await _perform(() => client.revokeBinding(binding),
        success: 'Conversation revoked.', refresh: true);
  }

  Future<void> _forget() async {
    if (!await _confirm(
        'Disconnect app control?',
        'This forgets the credential in this app. It does not revoke the server session or resolve an uncertain request.',
        'Disconnect')) {
      return;
    }
    client.forget();
  }

  bool get _canChange =>
      !client.busy && !client.mutationPending && !client.enrollmentPending;
  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Server conversations')),
      body: SafeArea(
          child: Align(
              alignment: Alignment.topCenter,
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: conversationWidth),
                child: ListView(padding: const EdgeInsets.all(20), children: [
                  SizedBox(
                      height: 4,
                      child:
                          client.busy ? const LinearProgressIndicator() : null),
                  const SizedBox(height: 20),
                  Text('Choose a server conversation',
                      style: Theme.of(context).textTheme.headlineSmall),
                  const SizedBox(height: 8),
                  Text(client.origin ?? 'Server not configured',
                      style: Theme.of(context).textTheme.bodySmall),
                  const SizedBox(height: 20),
                  const WorkspaceNotice(
                      message:
                          'Running tasks is not available on this server yet. You can create and select conversations.'),
                  if (_notice != null) ...[
                    const SizedBox(height: 12),
                    WorkspaceNotice(
                        message: _notice!,
                        tone:
                            _warning ? NoticeTone.warning : NoticeTone.success)
                  ],
                  const SizedBox(height: 24),
                  if (!client.accountAvailable)
                    WorkspaceNotice(
                        message:
                            'Sign in with an account for this server before enabling app control.',
                        action: widget.onSettings == null
                            ? null
                            : TextButton(
                                onPressed: widget.onSettings,
                                child: const Text('Open Settings')))
                  else if (!client.hasSession ||
                      client.enrollmentPending ||
                      _replacing)
                    _enrollmentForm()
                  else ...[
                    _selectionPanel(tokens),
                    const SizedBox(height: 16),
                    Wrap(spacing: 8, runSpacing: 8, children: [
                      OutlinedButton.icon(
                          onPressed: client.busy ? null : _refresh,
                          icon: const Icon(Icons.refresh, size: 18),
                          label: const Text('Refresh')),
                      TextButton(
                          onPressed: _canChange
                              ? () => setState(() {
                                    _replacing = true;
                                    _project.text = client.project ?? '';
                                  })
                              : null,
                          child: const Text('Replace session')),
                      TextButton(
                          onPressed: client.busy ? null : _forget,
                          child: const Text('Disconnect')),
                    ]),
                    if (client.expiresAt != null)
                      Padding(
                          padding: const EdgeInsets.only(top: 8),
                          child: Text(
                              'Session expires ${MaterialLocalizations.of(context).formatMediumDate(client.expiresAt!.toLocal())} at ${MaterialLocalizations.of(context).formatTimeOfDay(TimeOfDay.fromDateTime(client.expiresAt!.toLocal()))}',
                              style: Theme.of(context).textTheme.bodySmall)),
                    if (client.mutationPending) ...[
                      const SizedBox(height: 16),
                      WorkspaceNotice(
                          message:
                              'A ${client.pendingAction} request has an unknown outcome. The exact request is retained in memory.',
                          tone: NoticeTone.warning,
                          action: OutlinedButton(
                              onPressed: client.busy
                                  ? null
                                  : () => _perform(client.retryMutation,
                                      success: 'Request confirmed.',
                                      refresh: true),
                              child: const Text('Check same request')))
                    ],
                    const SizedBox(height: 24),
                    const Divider(),
                    const SizedBox(height: 16),
                    Text('New conversation',
                        style: Theme.of(context).textTheme.titleMedium),
                    const SizedBox(height: 12),
                    TextField(
                        key: const Key('control-title'),
                        controller: _title,
                        enabled: _canChange,
                        decoration: const InputDecoration(
                            labelText: 'Conversation title',
                            hintText: 'What are you working on?')),
                    if (widget.localHistoryAlias.isNotEmpty)
                      CheckboxListTile(
                          contentPadding: EdgeInsets.zero,
                          value: _linkHistory,
                          onChanged: _canChange
                              ? (value) =>
                                  setState(() => _linkHistory = value ?? false)
                              : null,
                          title: const Text(
                              'Link the current local chat as a label'),
                          subtitle: const Text(
                              'Its local ID does not grant control of a server conversation.')),
                    const SizedBox(height: 12),
                    Align(
                        alignment: Alignment.centerLeft,
                        child: FilledButton.icon(
                            onPressed: _canChange
                                ? () => _perform(() async {
                                      await client.createBinding(
                                          title: _title.text,
                                          localHistoryAlias: _linkHistory
                                              ? widget.localHistoryAlias
                                              : '');
                                      _title.clear();
                                    },
                                        success: 'Conversation created.',
                                        refresh: true)
                                : null,
                            icon: const Icon(Icons.add, size: 18),
                            label: const Text('Create conversation'))),
                    const SizedBox(height: 28),
                    Row(children: [
                      Expanded(
                          child: Text('Server conversations',
                              style: Theme.of(context).textTheme.titleMedium)),
                      Text('${client.bindings.length} on this page',
                          style: Theme.of(context).textTheme.bodySmall)
                    ]),
                    const SizedBox(height: 12),
                    if (!client.bindingsLoaded)
                      const Padding(
                          padding: EdgeInsets.symmetric(vertical: 24),
                          child: Text(
                              'Conversations have not been loaded. Refresh to try again.')),
                    if (client.bindingsLoaded && client.bindings.isEmpty)
                      const Padding(
                          padding: EdgeInsets.symmetric(vertical: 24),
                          child: Text('No server conversations yet')),
                    for (final binding in client.bindings)
                      _bindingRow(binding, tokens),
                    const SizedBox(height: 16),
                    Wrap(spacing: 8, runSpacing: 8, children: [
                      if (client.pagePosition > 0)
                        OutlinedButton(
                            onPressed: client.busy
                                ? null
                                : () => _perform(() => client.loadBindings()),
                            child: const Text('First page')),
                      if (client.nextPosition != null)
                        OutlinedButton(
                            onPressed: client.busy
                                ? null
                                : () => _perform(() => client.loadBindings(
                                    afterPosition: client.nextPosition!)),
                            child: const Text('Next page')),
                      if (client.nextPosition == null &&
                          client.bindings.isNotEmpty)
                        const Text('End of conversations'),
                    ]),
                  ],
                  const SizedBox(height: 32),
                ]),
              ))),
    );
  }

  Widget _enrollmentForm() => Form(
      key: _form,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(_replacing ? 'Replace app-control session' : 'Enable app control',
            style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        const Text(
            'Confirm your project and account password. Access stays in memory until this app closes, disconnects or the session expires.'),
        const SizedBox(height: 16),
        TextFormField(
            controller: _project,
            enabled: !client.busy && !client.enrollmentPending,
            decoration: const InputDecoration(labelText: 'Project'),
            validator: (value) => RegExp(r'^[A-Za-z0-9_-]{1,128}$')
                    .hasMatch(value?.trim() ?? '')
                ? null
                : 'Use letters, numbers, underscores or hyphens (up to 128).'),
        const SizedBox(height: 16),
        TextFormField(
            key: const Key('control-password'),
            controller: _password,
            enabled: !client.busy,
            obscureText: !_showPassword,
            autofillHints: const [AutofillHints.password],
            enableSuggestions: false,
            autocorrect: false,
            decoration: InputDecoration(
                labelText: 'Account password',
                suffixIcon: IconButton(
                    tooltip: _showPassword ? 'Hide password' : 'Show password',
                    onPressed: () =>
                        setState(() => _showPassword = !_showPassword),
                    icon: Icon(_showPassword
                        ? Icons.visibility_off_outlined
                        : Icons.visibility_outlined))),
            validator: (value) => value == null || value.isEmpty
                ? 'Enter your account password.'
                : utf8.encode(value).length > 4096
                    ? 'Password is too long.'
                    : null),
        const SizedBox(height: 20),
        Wrap(spacing: 8, runSpacing: 8, children: [
          FilledButton(
              onPressed: client.busy ? null : _enable,
              child: Text(client.enrollmentPending
                  ? 'Check same enrollment'
                  : _replacing
                      ? 'Replace session'
                      : 'Enable app control')),
          if (_replacing && !client.enrollmentPending)
            TextButton(
                onPressed: client.busy
                    ? null
                    : () => setState(() => _replacing = false),
                child: const Text('Cancel')),
          if (client.enrollmentPending)
            TextButton(
                onPressed: client.busy ? null : _forget,
                child: const Text('Forget pending enrollment')),
        ]),
      ]));
  Widget _selectionPanel(SonderTokens tokens) {
    final selection = client.selection;
    final matches = client.bindings.where((b) => b.id == selection?.bindingId);
    return Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
            color: tokens.panel,
            border: Border(left: BorderSide(color: tokens.accent, width: 2))),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(
              client.selectionKnown && selection?.bindingId != null
                  ? 'Selected conversation'
                  : 'Current selection',
              style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 8),
          Text(
              !client.selectionKnown
                  ? 'Refresh to confirm the current selection.'
                  : selection?.bindingId == null
                      ? 'No conversation selected'
                      : matches.isNotEmpty
                          ? matches.first.displayTitle
                          : 'Conversation ${selection!.bindingId}',
              style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          Text('Project: ${client.project ?? ''}',
              style: Theme.of(context).textTheme.bodySmall),
          if (client.selectionKnown && selection?.bindingId != null) ...[
            const SizedBox(height: 8),
            TextButton(
                onPressed: _canChange
                    ? () => _perform(client.clearSelection,
                        success: 'Selection cleared.', refresh: true)
                    : null,
                child: const Text('Clear selection')),
          ],
        ]));
  }

  Widget _bindingRow(AppConversationBinding binding, SonderTokens tokens) {
    final expired = !binding.expiresAt.isAfter(DateTime.now());
    final selected =
        client.selectionKnown && client.selection?.bindingId == binding.id;
    return Container(
        padding: const EdgeInsets.symmetric(vertical: 16),
        decoration: BoxDecoration(
            border: Border(bottom: BorderSide(color: tokens.hairline))),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Icon(
                selected
                    ? Icons.check_circle_outline
                    : Icons.chat_bubble_outline,
                color: selected ? tokens.accent : tokens.text2,
                size: 20),
            const SizedBox(width: 12),
            Expanded(
                child: Text(binding.displayTitle,
                    style: Theme.of(context).textTheme.titleSmall)),
          ]),
          const SizedBox(height: 8),
          Text(
              binding.revoked
                  ? 'Revoked'
                  : expired
                      ? 'Expired'
                      : selected
                          ? 'Selected'
                          : 'Available',
              style: Theme.of(context).textTheme.bodySmall),
          if (binding.localHistoryAlias.isNotEmpty)
            Text('Local history label: ${binding.localHistoryAlias}',
                style: Theme.of(context).textTheme.bodySmall),
          ExpansionTile(
              tilePadding: EdgeInsets.zero,
              title: const Text('Conversation details'),
              children: [
                Align(
                    alignment: Alignment.centerLeft,
                    child: SelectableText(binding.hostConversationId,
                        style: const TextStyle(
                            fontFamily: SonderTheme.mono, fontSize: 12))),
              ]),
          Wrap(spacing: 8, runSpacing: 8, children: [
            OutlinedButton(
                onPressed: _canChange &&
                        client.selectionKnown &&
                        !binding.revoked &&
                        !expired &&
                        !selected
                    ? () => _perform(() => client.selectBinding(binding),
                        success: 'Conversation selected.', refresh: true)
                    : null,
                child: Text(selected ? 'Selected' : 'Select')),
            TextButton(
                onPressed: _canChange && !binding.revoked
                    ? () => _revoke(binding)
                    : null,
                child: const Text('Revoke')),
          ]),
        ]));
  }
}
