import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'local_manager.dart';
import 'models.dart';
import 'settings.dart';

class SystemScreen extends StatefulWidget {
  final Settings settings;
  final SystemInfo? initialInfo;
  final bool liveUpdates;

  const SystemScreen({
    super.key,
    required this.settings,
    this.initialInfo,
    this.liveUpdates = true,
  });

  @override
  State<SystemScreen> createState() => _SystemScreenState();
}

class _SystemScreenState extends State<SystemScreen> {
  final _customCommand = TextEditingController(text: '/diagnostics');
  final _trainCount = TextEditingController(text: '10');
  final _autopilotGoal = TextEditingController();
  bool _autopilotObserve = false;
  bool _autopilotWeb = true;
  bool _autopilotAdaptive = true;
  SystemInfo? _info;
  LocalInstallInfo? _localInfo;
  String? _message;
  bool _loading = false;
  bool _working = false;
  bool _polling = false;
  Timer? _pollTimer;

  TrilobiteApi get _api => TrilobiteApi(
        baseUrl: widget.settings.serverUrl,
        apiKey: widget.settings.apiKey,
      );

  @override
  void dispose() {
    _customCommand.dispose();
    _trainCount.dispose();
    _autopilotGoal.dispose();
    _pollTimer?.cancel();
    super.dispose();
  }

  @override
  void initState() {
    super.initState();
    _info = widget.initialInfo;
    if (widget.liveUpdates) {
      _refresh();
      _pollTimer = Timer.periodic(
        const Duration(seconds: 2),
        (_) => _pollSystemInfo(),
      );
    }
  }

  Future<void> _pollSystemInfo() async {
    if (!mounted || _loading || _working || _polling) return;
    _polling = true;
    try {
      final info = await _api.systemInfo();
      if (mounted) setState(() => _info = info);
    } catch (_) {
      // The explicit Refresh path reports connection errors. Background polls
      // preserve the last useful snapshot and keep the screen visually stable.
    } finally {
      _polling = false;
    }
  }

  Future<void> _refresh({bool preserveMessage = false}) async {
    setState(() {
      _loading = true;
      if (!preserveMessage) _message = null;
    });
    try {
      final info = await _api.systemInfo();
      final localInfo = await LocalManager.inspect();
      if (!mounted) return;
      setState(() {
        _info = info;
        _localInfo = localInfo;
      });
    } on TrilobiteException catch (e) {
      final localInfo = await LocalManager.inspect();
      if (!mounted) return;
      setState(() {
        _localInfo = localInfo;
        _message = e.message;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _message = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _run(Future<LocalActionResult> Function() action) async {
    setState(() {
      _working = true;
      _message = null;
    });
    final result = await action();
    if (!mounted) return;
    setState(() {
      _working = false;
      _message = result.message;
    });
    if (result.ok) {
      await Future<void>.delayed(const Duration(seconds: 1));
      if (mounted) _refresh();
    }
  }

  Future<void> _sendCommand(String command) async {
    final text = command.trim();
    if (text.isEmpty) return;
    setState(() {
      _working = true;
      _message = null;
    });
    try {
      final reply = await _api.chat([
        ChatMessage(role: Role.user, content: text),
      ],
          model: widget.settings.model,
          contextSize: widget.settings.contextSize);
      if (mounted) setState(() => _message = reply);
    } on TrilobiteException catch (e) {
      if (mounted) setState(() => _message = e.message);
    } finally {
      if (mounted) setState(() => _working = false);
    }
  }

  Future<void> _cancelActiveAgents() async {
    final active = _info?.agents?.activeAgents ?? 0;
    if (active <= 0) {
      setState(() => _message = 'No active agents to cancel.');
      return;
    }
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Cancel active agents?'),
        content: Text(
          'This will cancel queued work immediately and request cancellation '
          'for $active running agent${active == 1 ? '' : 's'}. Active model '
          'calls finish in the background and their late results are discarded.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Keep running'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Cancel agents'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    await _sendCommand('/agentcancel all');
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 250));
    if (mounted) await _refresh();
  }

  Future<void> _retryPersistedAgent(String agentId) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Retry interrupted work?'),
        content: Text(
          'Trilobite will rerun $agentId from its private restart-safe ledger. '
          'Retries use the local code tier unless you run /agentretry manually '
          'with a different tier.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Not now'),
          ),
          FilledButton.icon(
            onPressed: () => Navigator.pop(context, true),
            icon: const Icon(Icons.replay_outlined),
            label: const Text('Retry'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    await _sendCommand('/agentretry $agentId');
    if (mounted) await _refresh();
  }

  Future<void> _startAutopilot({required bool planOnly}) async {
    final objective = _autopilotGoal.text.trim();
    if (objective.isEmpty) {
      setState(() => _message = 'Enter an autonomous goal first.');
      return;
    }
    final options = <String>[
      if (_autopilotObserve) '--observe',
      if (!_autopilotWeb) '--no-web',
      if (!_autopilotAdaptive) '--static',
    ];
    final command = [
      '/autopilot',
      planOnly ? 'plan' : 'run',
      ...options,
      objective,
    ].join(' ');
    await _sendCommand(command);
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 300));
    if (mounted) await _refresh(preserveMessage: true);
  }

  Future<void> _controlAutopilot(String action, AutopilotRun run) async {
    if (action == 'cancel') {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Cancel autonomous run?'),
          content: Text(
            'Cancel ${run.id}? Any active task may finish locally, but its late '
            'result will be discarded and the run cannot be resumed.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Keep running'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Cancel run'),
            ),
          ],
        ),
      );
      if (confirmed != true || !mounted) return;
    }
    await _sendCommand('/autopilot $action ${run.id}');
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 250));
    if (mounted) await _refresh(preserveMessage: true);
  }

  String _trainCommand() {
    final parsed = int.tryParse(_trainCount.text.trim()) ?? 10;
    final count = parsed.clamp(1, 500);
    return '/train $count';
  }

  Future<void> _copy(String text) async {
    await Clipboard.setData(ClipboardData(text: text));
    if (!mounted) return;
    setState(() => _message = 'Copied to clipboard.');
  }

  @override
  Widget build(BuildContext context) {
    final info = _info;
    final localInfo = _localInfo;
    return Scaffold(
      appBar: AppBar(
        automaticallyImplyLeading: false,
        leading: IconButton(
          tooltip: 'Back to chat',
          onPressed: () => Navigator.of(context).maybePop(),
          icon: const Icon(Icons.arrow_back),
        ),
        title: const Text('System'),
        actions: [
          Tooltip(
            message: 'Return to main chat',
            child: TextButton.icon(
              onPressed: () => Navigator.of(context).maybePop(),
              icon: const Icon(Icons.chat_bubble_outline, size: 18),
              label: const Text('Chat'),
            ),
          ),
          IconButton(
            tooltip: 'Refresh',
            onPressed: _loading ? null : _refresh,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (localInfo != null) ...[
            _Section(
              title: 'Install',
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _StatusRow(
                    label: 'Platform',
                    value: localInfo.platform,
                    ok: localInfo.canLaunch,
                  ),
                  _StatusRow(
                    label: 'Local system',
                    value: localInfo.systemExists
                        ? localInfo.systemDir
                        : 'Not bundled',
                    ok: localInfo.systemExists,
                  ),
                  _StatusRow(
                    label: 'Shared memory',
                    value: localInfo.sharedHome,
                    ok: true,
                    onCopy: () => _copy(localInfo.sharedHome),
                  ),
                  _StatusRow(
                    label: 'Local server',
                    value: localInfo.defaultServerReachable
                        ? 'Reachable on 127.0.0.1:11435'
                        : 'Not detected on 127.0.0.1:11435',
                    ok: localInfo.defaultServerReachable,
                  ),
                  _StatusRow(
                    label: 'Updater',
                    value: localInfo.gitCheckout
                        ? 'Git pull enabled'
                        : 'First update will replace bundled folder from Git',
                    ok: true,
                  ),
                  _StatusRow(
                    label: 'Engine setup',
                    value: localInfo.bootstrapScript
                        ? 'One-click setup available'
                        : 'Bootstrap script not bundled',
                    ok: localInfo.bootstrapScript || !localInfo.canLaunch,
                  ),
                  _StatusRow(
                    label: 'Runtime payload',
                    value: localInfo.engineBundle
                        ? 'Sealed offline engine included'
                        : 'Host runtimes; downloads may be needed',
                    ok: localInfo.engineBundle,
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
          ],
          _Section(
            title: 'Local Runtime',
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton.icon(
                  onPressed: _working
                      ? null
                      : () => _run(() => LocalManager.setupEngine(
                            allowHosted: widget.settings.allowHosted,
                            contextSize: widget.settings.contextSize,
                          )),
                  icon: const Icon(Icons.auto_fix_high_outlined),
                  label: const Text('Setup engine'),
                ),
                FilledButton.icon(
                  onPressed: _working
                      ? null
                      : () => _run(() => LocalManager.startServer(
                            allowHosted: widget.settings.allowHosted,
                            contextSize: widget.settings.contextSize,
                            persistOnAppClose:
                                widget.settings.keepServerRunning,
                          )),
                  icon: const Icon(Icons.play_arrow_outlined),
                  label: const Text('Start server'),
                ),
                FilledButton.tonalIcon(
                  onPressed:
                      _working ? null : () => _run(LocalManager.stopServers),
                  icon: const Icon(Icons.stop_circle_outlined),
                  label: const Text('Stop servers'),
                ),
                FilledButton.tonalIcon(
                  onPressed: _working
                      ? null
                      : () => _run(LocalManager.startEndlessTraining),
                  icon: const Icon(Icons.all_inclusive),
                  label: const Text('Endless train'),
                ),
                OutlinedButton.icon(
                  onPressed:
                      _working ? null : () => _run(LocalManager.updateFromGit),
                  icon: const Icon(Icons.system_update_alt),
                  label: const Text('Update from Git'),
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          _Section(
            title: 'Autopilot',
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Give Trilobite an outcome, then let its local planner build '
                  'a persistent checklist, execute one guarded task at a time, '
                  'validate the result, and pause safely when a budget or '
                  'decision boundary is reached.',
                  style: Theme.of(context).textTheme.bodyMedium,
                ),
                const SizedBox(height: 12),
                TextField(
                  key: const Key('autopilot-goal'),
                  controller: _autopilotGoal,
                  enabled: !_working,
                  minLines: 2,
                  maxLines: 4,
                  decoration: const InputDecoration(
                    labelText: 'Autonomous goal',
                    hintText:
                        'Inspect this project, implement the missing feature, and run its tests',
                    alignLabelWithHint: true,
                    border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    ChoiceChip(
                      label: const Text('Workspace'),
                      avatar: const Icon(Icons.edit_note_outlined, size: 18),
                      selected: !_autopilotObserve,
                      onSelected: _working
                          ? null
                          : (_) => setState(() => _autopilotObserve = false),
                    ),
                    ChoiceChip(
                      label: const Text('Observe only'),
                      avatar: const Icon(Icons.visibility_outlined, size: 18),
                      selected: _autopilotObserve,
                      onSelected: _working
                          ? null
                          : (_) => setState(() => _autopilotObserve = true),
                    ),
                    FilterChip(
                      label: const Text('Public web'),
                      avatar: const Icon(Icons.public_outlined, size: 18),
                      selected: _autopilotWeb,
                      onSelected: _working
                          ? null
                          : (value) => setState(() => _autopilotWeb = value),
                    ),
                    FilterChip(
                      label: const Text('Adaptive review'),
                      avatar: const Icon(Icons.route_outlined, size: 18),
                      selected: _autopilotAdaptive,
                      onSelected: _working
                          ? null
                          : (value) =>
                              setState(() => _autopilotAdaptive = value),
                    ),
                    Tooltip(
                      message:
                          'Autopilot never receives location consent, cloud tiers, delete, account, permission, or fleet controls.',
                      child: Icon(
                        Icons.shield_outlined,
                        size: 20,
                        color: Theme.of(context).colorScheme.primary,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    FilledButton.tonalIcon(
                      key: const Key('autopilot-plan'),
                      onPressed: _working
                          ? null
                          : () => _startAutopilot(planOnly: true),
                      icon: const Icon(Icons.account_tree_outlined),
                      label: const Text('Plan only'),
                    ),
                    FilledButton.icon(
                      key: const Key('autopilot-run'),
                      onPressed: _working
                          ? null
                          : () => _startAutopilot(planOnly: false),
                      icon: const Icon(Icons.rocket_launch_outlined),
                      label: const Text('Run goal'),
                    ),
                    OutlinedButton.icon(
                      onPressed: _working
                          ? null
                          : () => _sendCommand('/autopilot status'),
                      icon: const Icon(Icons.manage_search_outlined),
                      label: const Text('Status'),
                    ),
                  ],
                ),
                if (info?.autopilot != null) ...[
                  const SizedBox(height: 14),
                  const Divider(),
                  const SizedBox(height: 8),
                  _AutopilotPanel(
                    status: info!.autopilot!,
                    onResume: (run) => _controlAutopilot('resume', run),
                    onPause: (run) => _controlAutopilot('pause', run),
                    onCancel: (run) => _controlAutopilot('cancel', run),
                  ),
                ],
              ],
            ),
          ),
          const SizedBox(height: 12),
          _Section(
            title: 'Server Actions',
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/stats'),
                  icon: const Icon(Icons.query_stats),
                  label: const Text('Stats'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/context'),
                  icon: const Icon(Icons.monitor_heart_outlined),
                  label: const Text('Context'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/compact'),
                  icon: const Icon(Icons.compress_outlined),
                  label: const Text('Compact'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/todo'),
                  icon: const Icon(Icons.task_alt_outlined),
                  label: const Text('Tasks'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/quality'),
                  icon: const Icon(Icons.fact_check_outlined),
                  label: const Text('Quality'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/improve'),
                  icon: const Icon(Icons.tips_and_updates_outlined),
                  label: const Text('Improve'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/agents'),
                  icon: const Icon(Icons.hub_outlined),
                  label: const Text('Agents'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/capacity'),
                  icon: const Icon(Icons.memory_outlined),
                  label: const Text('Capacity'),
                ),
                if ((info?.agents?.activeAgents ?? 0) > 0)
                  OutlinedButton.icon(
                    onPressed: _working ? null : _cancelActiveAgents,
                    icon: const Icon(Icons.cancel_schedule_send_outlined),
                    label: const Text('Cancel active'),
                  ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/commands'),
                  icon: const Icon(Icons.terminal_outlined),
                  label: const Text('Commands'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/dump app'),
                  icon: const Icon(Icons.description_outlined),
                  label: const Text('Dump'),
                ),
                OutlinedButton.icon(
                  onPressed:
                      _working ? null : () => _sendCommand('/permissions'),
                  icon: const Icon(Icons.security_outlined),
                  label: const Text('Permissions'),
                ),
                SizedBox(
                  width: 120,
                  child: TextField(
                    controller: _trainCount,
                    enabled: !_working,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      isDense: true,
                      labelText: 'Train',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                OutlinedButton.icon(
                  onPressed:
                      _working ? null : () => _sendCommand(_trainCommand()),
                  icon: const Icon(Icons.school_outlined),
                  label: const Text('Run'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/help'),
                  icon: const Icon(Icons.help_outline),
                  label: const Text('Help'),
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          _Section(
            title: 'Command',
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _customCommand,
                    enabled: !_working,
                    autocorrect: false,
                    decoration: const InputDecoration(
                      isDense: true,
                      hintText: '/diagnostics',
                      border: OutlineInputBorder(),
                    ),
                    onSubmitted: (_) {
                      if (!_working) _sendCommand(_customCommand.text);
                    },
                  ),
                ),
                const SizedBox(width: 8),
                FilledButton.icon(
                  onPressed: _working
                      ? null
                      : () => _sendCommand(_customCommand.text.trim()),
                  icon: const Icon(Icons.terminal),
                  label: const Text('Send'),
                ),
              ],
            ),
          ),
          if (_loading || _working) ...[
            const SizedBox(height: 16),
            const LinearProgressIndicator(),
          ],
          if (_message != null) ...[
            const SizedBox(height: 16),
            _OutputCard(text: _message!),
          ],
          const SizedBox(height: 12),
          if (info != null) ...[
            _Section(title: 'Status', child: _OutputText(info.status)),
            const SizedBox(height: 12),
            if (info.runtimePolicy != null) ...[
              _Section(
                title: 'Local Runtime Policy',
                child: _RuntimePolicyPanel(policy: info.runtimePolicy!),
              ),
              const SizedBox(height: 12),
            ],
            if (info.mcpRuntime != null) ...[
              _Section(
                title: 'Runtime Convergence',
                child: _McpRuntimePanel(runtime: info.mcpRuntime!),
              ),
              const SizedBox(height: 12),
            ],
            if (info.learningHealth != null) ...[
              _Section(
                title: 'Learning Quality',
                child: _LearningHealthPanel(health: info.learningHealth!),
              ),
              const SizedBox(height: 12),
            ],
            if (info.context != null) ...[
              _Section(
                title: 'Context Health',
                child: _ContextHealthPanel(health: info.context!),
              ),
              const SizedBox(height: 12),
            ],
            if (info.activity?.displayResponse != null) ...[
              _Section(
                title: 'Workbench Activity',
                child: WorkbenchActivityPanel(
                  response: info.activity!.displayResponse!,
                  totalToolCalls: info.activity!.totalToolCalls,
                ),
              ),
              const SizedBox(height: 12),
            ],
            _Section(
              title: 'Server State',
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (info.dbPath.isNotEmpty)
                    _StatusRow(
                      label: 'Database',
                      value: info.dbPath,
                      ok: true,
                      onCopy: () => _copy(info.dbPath),
                    ),
                  if (info.stateHome.isNotEmpty)
                    _StatusRow(
                      label: 'Home',
                      value: info.stateHome,
                      ok: true,
                      onCopy: () => _copy(info.stateHome),
                    ),
                  if (info.dbPath.isEmpty && info.stateHome.isEmpty)
                    const _OutputText('Server did not report state paths.'),
                ],
              ),
            ),
            const SizedBox(height: 12),
            _Section(title: 'Learning', child: _OutputText(info.learnTiers)),
            const SizedBox(height: 12),
            if (info.agents != null) ...[
              _Section(
                title: 'Agents',
                child: _AgentStatusPanel(
                  status: info.agents!,
                  onRetry: _retryPersistedAgent,
                ),
              ),
              const SizedBox(height: 12),
            ],
            if (info.improvements.isNotEmpty) ...[
              _Section(
                title: 'Improvements',
                child: _OutputText(info.improvements),
              ),
              const SizedBox(height: 12),
            ],
            _Section(title: 'Stats', child: _OutputText(info.stats)),
            const SizedBox(height: 12),
            _Section(
              title: 'Models',
              child: Wrap(
                spacing: 8,
                runSpacing: 8,
                children: info.models
                    .map((m) => Chip(
                          label: Text('${m.id} - ${m.ownedBy}'),
                          avatar: Icon(
                            m.ownedBy == 'cloud'
                                ? Icons.cloud_outlined
                                : Icons.memory_outlined,
                            size: 18,
                          ),
                        ))
                    .toList(),
              ),
            ),
          ],
          const SizedBox(height: 24),
          Text(
            'Desktop builds look for a bundled local-system folder next to the app. '
            'A sealed engine payload can include Python, Ollama, and models for offline setup; '
            'otherwise setup uses installed runtimes and may download missing components. '
            'Runtime memory is shared through '
            '${localInfo?.sharedHome ?? LocalManager.sharedHomePath()}. '
            'Android can connect to a LAN/hosted server, but cannot launch the Python server itself.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}

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

class _McpRuntimePanel extends StatelessWidget {
  final McpRuntimeInfo runtime;

  const _McpRuntimePanel({required this.runtime});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final healthy = runtime.status == 'current' && !runtime.hasWarning;
    final warnings = <String>[
      if (runtime.sourceChanged)
        'Newer source is waiting for the next atomic MCP refresh.',
      if (runtime.lastError.isNotEmpty)
        '${runtime.lastError} (last known-good tools remain active)',
      if (runtime.lastNotificationError.isNotEmpty)
        'Tool-list notification: ${runtime.lastNotificationError}',
    ];
    return Column(
      key: const Key('mcp-runtime-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(
              avatar: Icon(
                healthy
                    ? Icons.sync_lock_outlined
                    : Icons.warning_amber_outlined,
                size: 18,
                color: healthy ? cs.primary : cs.error,
              ),
              label: Text('MCP ${runtime.status}'),
            ),
            Chip(
              avatar: const Icon(Icons.build_outlined, size: 18),
              label: Text('${runtime.registeredTools} tools'),
            ),
            Chip(
              avatar: const Icon(Icons.refresh_outlined, size: 18),
              label: Text('${runtime.refreshCount} atomic refreshes'),
            ),
            Chip(
              avatar: Icon(
                runtime.protocolListChanged
                    ? Icons.notifications_active_outlined
                    : Icons.notifications_off_outlined,
                size: 18,
              ),
              label: Text(
                runtime.protocolListChanged
                    ? 'Live tool-list updates'
                    : 'Static tool list',
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          'Tool implementations and schemas stage in isolation, then replace '
          'the active registry only after the updated source loads cleanly.',
          style: Theme.of(context).textTheme.bodyMedium,
        ),
        const SizedBox(height: 10),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: cs.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(10),
          ),
          child: SelectableText(
            'loaded  ${runtime.loadedShort.isEmpty ? 'unknown' : runtime.loadedShort}\n'
            'current ${runtime.currentShort.isEmpty ? 'unknown' : runtime.currentShort}',
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  fontFamily: 'monospace',
                ),
          ),
        ),
        if (warnings.isNotEmpty) ...[
          const SizedBox(height: 10),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: cs.errorContainer,
              borderRadius: BorderRadius.circular(10),
            ),
            child: SelectableText(
              warnings.join('\n'),
              style: TextStyle(color: cs.onErrorContainer),
            ),
          ),
        ],
        if (runtime.path.isNotEmpty) ...[
          const SizedBox(height: 10),
          SelectableText(
            'Loaded source: ${runtime.path}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
        const SizedBox(height: 6),
        Text(
          'Inspect or retry safely with /mcp status or /mcp refresh.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ],
    );
  }
}

class _LearningHealthPanel extends StatelessWidget {
  final LearningHealthInfo health;

  const _LearningHealthPanel({required this.health});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final statusColor = _statusColor(context);
    final yieldText = health.distillationYield == null
        ? 'Yield building'
        : '${health.distillationYield!.toStringAsFixed(3)} lesson / positive';
    final sources = health.lessonSources.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final signals = health.signals.take(6).toList();
    final issueCount = health.quality.issueCount;
    return Column(
      key: const Key('learning-health-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            Chip(
              avatar: Icon(
                _statusIcon(),
                size: 18,
                color: statusColor,
              ),
              label: Text('Learning ${health.status}'),
            ),
            Chip(
              avatar: const Icon(Icons.school_outlined, size: 18),
              label: Text('${health.lessons} lessons'),
            ),
            Chip(
              avatar: const Icon(Icons.verified_outlined, size: 18),
              label: Text('${health.outcomes} outcomes'),
            ),
            Chip(
              avatar: const Icon(Icons.science_outlined, size: 18),
              label: Text(yieldText),
            ),
          ],
        ),
        const SizedBox(height: 12),
        _MeterBar(
          label: 'Grounded',
          percent: health.outcomeCoveragePercent,
          detail:
              '${health.outcomeInteractions}/${health.interactions} interactions have outcomes',
          color: statusColor,
        ),
        const SizedBox(height: 10),
        _MeterBar(
          label: 'Positive',
          percent: health.positivePercent,
          detail:
              '${health.goodOutcomes} positive, ${health.badOutcomes} negative signals',
        ),
        const SizedBox(height: 10),
        _MeterBar(
          label: 'Embedded',
          percent: health.quality.embeddingPercent,
          detail:
              '${health.lessons - health.quality.missingEmbeddings}/${health.lessons} lessons searchable semantically',
        ),
        const SizedBox(height: 12),
        Text('Lesson provenance',
            style: Theme.of(context).textTheme.labelLarge),
        const SizedBox(height: 6),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: sources.isEmpty
              ? const [Chip(label: Text('No lessons yet'))]
              : sources
                  .map(
                    (entry) => Chip(
                      avatar: Icon(
                        entry.key == 'interaction'
                            ? Icons.link_outlined
                            : Icons.auto_awesome_outlined,
                        size: 17,
                      ),
                      label: Text('${entry.key}  ${entry.value}'),
                    ),
                  )
                  .toList(growable: false),
        ),
        if (signals.isNotEmpty) ...[
          const SizedBox(height: 12),
          Text('Outcome signals',
              style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 6),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: signals
                .map(
                  (signal) => Tooltip(
                    message:
                        'Average reward ${signal.averageReward.toStringAsFixed(2)}',
                    child: Chip(
                      avatar: Icon(
                        signal.good
                            ? Icons.thumb_up_alt_outlined
                            : Icons.thumb_down_alt_outlined,
                        size: 16,
                        color: signal.good ? cs.primary : cs.error,
                      ),
                      label: Text(
                        '${signal.signal.replaceAll('_', ' ')}  ${signal.count}',
                      ),
                    ),
                  ),
                )
                .toList(growable: false),
          ),
        ],
        const SizedBox(height: 12),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(11),
          decoration: BoxDecoration(
            color: issueCount == 0
                ? cs.primaryContainer.withValues(alpha: 0.42)
                : cs.errorContainer,
            borderRadius: BorderRadius.circular(10),
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(
                issueCount == 0
                    ? Icons.shield_outlined
                    : Icons.warning_amber_outlined,
                size: 18,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  issueCount == 0
                      ? 'Memory hygiene is clean: no duplicate, embedding, index, source, or privacy defects.'
                      : _issueText(),
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 7),
        Text(
          'Inspect the exact report with /learning; review rows with /quality.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ],
    );
  }

  String _issueText() {
    final q = health.quality;
    final parts = <String>[
      if (q.duplicateRows > 0) '${q.duplicateRows} duplicate rows',
      if (q.missingEmbeddings > 0) '${q.missingEmbeddings} missing embeddings',
      if (q.vagueLessons > 0) '${q.vagueLessons} vague lessons',
      if (q.privacyFlags > 0) '${q.privacyFlags} privacy flags',
      if (q.missingSources > 0) '${q.missingSources} missing sources',
      if (q.missingFts + q.orphanFts > 0)
        '${q.missingFts + q.orphanFts} search-index defects',
    ];
    return 'Memory hygiene needs review: ${parts.join(', ')}.';
  }

  IconData _statusIcon() {
    if (health.status == 'healthy') return Icons.check_circle_outline;
    if (health.status == 'attention') return Icons.error_outline;
    if (health.status == 'watch') return Icons.visibility_outlined;
    return Icons.hourglass_top_outlined;
  }

  Color _statusColor(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    if (health.status == 'healthy') return cs.primary;
    if (health.status == 'attention') return cs.error;
    if (health.status == 'watch') return Colors.amber.shade800;
    return cs.secondary;
  }
}

class _ContextHealthPanel extends StatelessWidget {
  final ContextHealth health;

  const _ContextHealthPanel({required this.health});

  @override
  Widget build(BuildContext context) {
    final status = health.status.isEmpty ? 'unknown' : health.status;
    final sessionTitle = health.title.isEmpty ? health.session : health.title;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            Chip(
              avatar: Icon(
                _statusIcon(status),
                size: 18,
                color: _statusColor(context, status),
              ),
              label: Text('Context $status'),
            ),
            Chip(
              avatar: const Icon(Icons.forum_outlined, size: 18),
              label: Text('Session: $sessionTitle'),
            ),
            Chip(
              avatar: const Icon(Icons.folder_copy_outlined, size: 18),
              label: Text('Project: ${health.project}'),
            ),
            Chip(
              avatar: const Icon(Icons.view_week_outlined, size: 18),
              label: Text(
                  '${health.contextMode}: native ${health.nativeContextLimit}'),
            ),
          ],
        ),
        const SizedBox(height: 12),
        _MeterBar(
          label: 'Context',
          percent: health.contextPercent,
          detail:
              '~${health.estimatedTokens}/${health.contextLimit} virtual tokens',
          color: _statusColor(context, status),
        ),
        const SizedBox(height: 10),
        _MeterBar(
          label: 'Live turns',
          percent: health.turnPercent,
          detail:
              '${health.liveTurns}/${health.maxLiveTurns} kept live, ${health.totalTurns} total',
        ),
        const SizedBox(height: 10),
        _MeterBar(
          label: 'Memory',
          percent: health.memoryPercent,
          detail:
              '${health.lessons} lessons, ${health.facts} facts, ${health.interactions} interactions',
        ),
        const SizedBox(height: 12),
        _OutputCard(text: health.consoleText()),
        if (health.updatedTs.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            'Last updated ${health.updatedTs}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ],
    );
  }

  IconData _statusIcon(String status) {
    if (status == 'hot') return Icons.warning_amber_outlined;
    if (status == 'warm') return Icons.thermostat_outlined;
    if (status == 'healthy') return Icons.check_circle_outline;
    return Icons.info_outline;
  }

  Color _statusColor(BuildContext context, String status) {
    final cs = Theme.of(context).colorScheme;
    if (status == 'hot') return cs.error;
    if (status == 'warm') return Colors.amber.shade800;
    if (status == 'healthy') return cs.primary;
    return cs.outline;
  }
}

class _AutopilotPanel extends StatelessWidget {
  final AutopilotStatus status;
  final ValueChanged<AutopilotRun> onResume;
  final ValueChanged<AutopilotRun> onPause;
  final ValueChanged<AutopilotRun> onCancel;

  const _AutopilotPanel({
    required this.status,
    required this.onResume,
    required this.onPause,
    required this.onCancel,
  });

  @override
  Widget build(BuildContext context) {
    final run = status.latest;
    final colors = Theme.of(context).colorScheme;
    if (run == null) {
      return const _OutputText(
        'No autonomous goals yet. Planning creates a restart-persistent run.',
      );
    }
    final passed = run.tasks.where((task) => task.status == 'passed').length;
    final superseded =
        run.tasks.where((task) => task.status == 'superseded').length;
    final complete = passed + superseded;
    final progress = run.tasks.isEmpty ? 0.0 : complete / run.tasks.length;
    final color = _runColor(colors, run.status);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(
              avatar: Icon(_runIcon(run.status), size: 18, color: color),
              label: Text(run.status.replaceAll('_', ' ')),
            ),
            Chip(
              avatar: const Icon(Icons.layers_outlined, size: 18),
              label: Text('${status.activeRuns} active'),
            ),
            Chip(
              avatar: const Icon(Icons.pause_circle_outline, size: 18),
              label: Text('${status.resumableRuns} resumable'),
            ),
            Chip(
              avatar: Icon(
                run.policy == 'observe'
                    ? Icons.visibility_outlined
                    : Icons.edit_note_outlined,
                size: 18,
              ),
              label: Text(run.policy),
            ),
            Chip(
              avatar: const Icon(Icons.memory_outlined, size: 18),
              label: Text(run.tier.isEmpty ? 'local' : 'local ${run.tier}'),
            ),
            Chip(
              avatar: Icon(
                run.allowWeb
                    ? Icons.public_outlined
                    : Icons.public_off_outlined,
                size: 18,
              ),
              label: Text(run.allowWeb ? 'web on' : 'web off'),
            ),
            Chip(
              avatar: Icon(
                run.adaptive ? Icons.route_outlined : Icons.linear_scale,
                size: 18,
              ),
              label: Text(run.adaptive ? 'adaptive' : 'static plan'),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          run.objective,
          style: Theme.of(context).textTheme.titleSmall?.copyWith(
                fontWeight: FontWeight.w700,
              ),
        ),
        const SizedBox(height: 4),
        SelectableText(
          '${run.id} • ${run.phase} • ${run.project.isEmpty ? 'default project' : run.project}',
          style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: colors.onSurfaceVariant,
                fontFamily: 'monospace',
              ),
        ),
        const SizedBox(height: 10),
        LinearProgressIndicator(
          value: progress.clamp(0.0, 1.0),
          minHeight: 7,
          borderRadius: BorderRadius.circular(99),
          color: color,
        ),
        const SizedBox(height: 6),
        Text(
          '$complete/${run.tasks.length} tasks settled • '
          '${run.cycles} cycles • ${run.failures}/${run.maxFailures} failures • '
          '${run.checkpoints} checkpoint${run.checkpoints == 1 ? '' : 's'} • '
          '${run.replans}/${run.maxReplans} replans',
          style: Theme.of(context).textTheme.bodySmall,
        ),
        if (run.summary.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text(run.summary),
        ],
        if (run.lastError.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            run.lastError,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: colors.error,
                  fontWeight: FontWeight.w600,
                ),
          ),
        ],
        const SizedBox(height: 10),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            if (run.isResumable)
              FilledButton.tonalIcon(
                onPressed: () => onResume(run),
                icon: const Icon(Icons.play_arrow_outlined),
                label: const Text('Resume'),
              ),
            if (run.isActive)
              OutlinedButton.icon(
                onPressed: () => onPause(run),
                icon: const Icon(Icons.pause_outlined),
                label: const Text('Pause'),
              ),
            if (!run.isTerminal)
              TextButton.icon(
                onPressed: () => onCancel(run),
                icon: const Icon(Icons.close_outlined),
                label: const Text('Cancel'),
              ),
          ],
        ),
        if (run.criteria.isNotEmpty) ...[
          const SizedBox(height: 14),
          Text('Success gates', style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 6),
          ...run.criteria.map(
            (criterion) => Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.flag_outlined, size: 16, color: colors.primary),
                  const SizedBox(width: 7),
                  Expanded(child: Text(criterion)),
                ],
              ),
            ),
          ),
        ],
        if (run.tasks.isNotEmpty) ...[
          const SizedBox(height: 14),
          Text('Persistent checklist',
              style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 7),
          ...run.tasks.map(
            (task) => Container(
              width: double.infinity,
              margin: const EdgeInsets.only(bottom: 7),
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: colors.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: colors.outlineVariant),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(
                    _taskIcon(task.status),
                    size: 19,
                    color: _runColor(colors, task.status),
                  ),
                  const SizedBox(width: 9),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          '${task.id} • ${task.kind} • ${task.title}',
                          style: Theme.of(context).textTheme.labelLarge,
                        ),
                        const SizedBox(height: 3),
                        Text(
                          task.error.isNotEmpty ? task.error : task.instruction,
                          maxLines: 3,
                          overflow: TextOverflow.ellipsis,
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 8),
                  Text(
                    task.status.replaceAll('_', ' '),
                    style: Theme.of(context).textTheme.labelSmall?.copyWith(
                          color: _runColor(colors, task.status),
                        ),
                  ),
                ],
              ),
            ),
          ),
        ],
        if (status.events.isNotEmpty) ...[
          const SizedBox(height: 8),
          ExpansionTile(
            tilePadding: EdgeInsets.zero,
            childrenPadding: const EdgeInsets.only(bottom: 8),
            title: const Text('Run events'),
            subtitle: Text('${status.events.length} persisted checkpoints'),
            children: [
              _OutputCard(
                text: status.events
                    .map((event) => '${event.kind}: ${event.message}')
                    .join('\n'),
              ),
            ],
          ),
        ],
        if (run.finalReport.isNotEmpty) ...[
          ExpansionTile(
            tilePadding: EdgeInsets.zero,
            childrenPadding: const EdgeInsets.only(bottom: 8),
            title: const Text('End report'),
            subtitle: const Text('Evidence-backed task ledger'),
            children: [_OutputCard(text: run.finalReport)],
          ),
        ],
      ],
    );
  }

  static IconData _runIcon(String value) {
    if (value == 'completed' || value == 'passed') {
      return Icons.check_circle_outline;
    }
    if (value == 'running' || value == 'planning' || value == 'in_progress') {
      return Icons.sync;
    }
    if (value == 'failed' || value == 'blocked') return Icons.error_outline;
    if (value == 'cancelled' || value == 'superseded') {
      return Icons.remove_circle_outline;
    }
    return Icons.pause_circle_outline;
  }

  static IconData _taskIcon(String value) => _runIcon(value);

  static Color _runColor(ColorScheme colors, String value) {
    if (value == 'completed' || value == 'passed') return colors.primary;
    if (value == 'failed' || value == 'blocked') return colors.error;
    if (value == 'running' || value == 'planning' || value == 'in_progress') {
      return Colors.amber.shade800;
    }
    return colors.outline;
  }
}

class _AgentStatusPanel extends StatelessWidget {
  final AgentStatus status;
  final ValueChanged<String>? onRetry;

  const _AgentStatusPanel({required this.status, this.onRetry});

  @override
  Widget build(BuildContext context) {
    final recent = status.agents.take(6).toList();
    final capacity = status.capacity;
    final availableGiB = capacity == null
        ? 0.0
        : capacity.availableMemoryBytes / (1024 * 1024 * 1024);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(
              avatar: const Icon(Icons.hub_outlined, size: 18),
              label: Text('${status.activeAgents} active'),
            ),
            Chip(
              avatar: const Icon(Icons.list_alt_outlined, size: 18),
              label: Text('${status.totalAgents} total'),
            ),
            if (capacity != null)
              Chip(
                avatar: const Icon(Icons.dynamic_feed_outlined, size: 18),
                label: Text('${capacity.agentCeiling} queued cap'),
              ),
            if (capacity != null)
              Chip(
                avatar: const Icon(Icons.memory_outlined, size: 18),
                label: Text('${capacity.workerSlots} worker slots'),
              ),
            if (capacity != null)
              Chip(
                avatar: const Icon(Icons.storage_outlined, size: 18),
                label: Text('${availableGiB.toStringAsFixed(1)} GiB free'),
              ),
            if (status.cancelPending > 0)
              Chip(
                avatar:
                    const Icon(Icons.cancel_schedule_send_outlined, size: 18),
                label: Text('${status.cancelPending} cancelling'),
              ),
            if (status.interruptedAgents > 0)
              Chip(
                avatar: const Icon(Icons.restore_outlined, size: 18),
                label: Text('${status.interruptedAgents} recoverable'),
              ),
            Chip(
              avatar: const Icon(Icons.keyboard_double_arrow_down, size: 18),
              label: Text('${status.tokensIn} in'),
            ),
            Chip(
              avatar: const Icon(Icons.keyboard_double_arrow_up, size: 18),
              label: Text('${status.tokensOut} out'),
            ),
          ],
        ),
        const SizedBox(height: 12),
        if (recent.isEmpty)
          const _OutputText('No master or subagent activity yet.')
        else
          ...recent.map((agent) => Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: _OutputCard(
                  text: '${agent.id} [${agent.status}] ${agent.activity}\n'
                      'role=${agent.role} calls=${agent.toolCalls} '
                      'tokens=${agent.tokensIn}/${agent.tokensOut}\n'
                      'task: ${agent.task}',
                  action: agent.role == 'master' &&
                          agent.status == 'interrupted' &&
                          onRetry != null
                      ? TextButton.icon(
                          onPressed: () => onRetry!(agent.id),
                          icon: const Icon(Icons.replay_outlined, size: 18),
                          label: const Text('Retry locally'),
                        )
                      : null,
                ),
              )),
        if (status.events.isNotEmpty) ...[
          const SizedBox(height: 4),
          _OutputCard(text: status.events.take(8).join('\n')),
        ],
      ],
    );
  }
}

class WorkbenchActivityPanel extends StatelessWidget {
  final ActivityResponse response;
  final int totalToolCalls;

  const WorkbenchActivityPanel({
    super.key,
    required this.response,
    required this.totalToolCalls,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final recentActions = response.actions.length <= 8
        ? response.actions
        : response.actions.sublist(response.actions.length - 8);
    final completed =
        response.checklist.where((item) => item.status == 'done').length;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(
              avatar: Icon(
                response.status == 'error'
                    ? Icons.error_outline
                    : response.status == 'running'
                        ? Icons.sync
                        : Icons.check_circle_outline,
                size: 18,
              ),
              label:
                  Text(response.status.isEmpty ? 'Unknown' : response.status),
            ),
            Chip(
              avatar: const Icon(Icons.build_outlined, size: 18),
              label: Text('${response.toolCalls} actions'),
            ),
            Chip(
              avatar: const Icon(Icons.history, size: 18),
              label: Text('$totalToolCalls total'),
            ),
            Chip(
              avatar: const Icon(Icons.timer_outlined, size: 18),
              label: Text('${response.elapsedMs} ms'),
            ),
          ],
        ),
        if (response.resultSummary.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text(
            response.resultSummary,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  fontWeight: FontWeight.w600,
                ),
          ),
        ],
        if (response.checklist.isNotEmpty) ...[
          const SizedBox(height: 14),
          Row(
            children: [
              Icon(Icons.checklist_rounded, size: 19, color: cs.primary),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  response.checklistTitle.isEmpty
                      ? 'Checklist'
                      : response.checklistTitle,
                  style: Theme.of(context).textTheme.titleSmall,
                ),
              ),
              Text('$completed/${response.checklist.length}'),
            ],
          ),
          const SizedBox(height: 8),
          ...response.checklist.map((item) => Padding(
                padding: const EdgeInsets.only(bottom: 7),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(
                      _checklistIcon(item.status),
                      size: 18,
                      color: _checklistColor(cs, item.status),
                    ),
                    const SizedBox(width: 8),
                    Expanded(child: Text(item.title)),
                    const SizedBox(width: 8),
                    Text(
                      item.status.replaceAll('_', ' '),
                      style: Theme.of(context).textTheme.labelSmall?.copyWith(
                            color: _checklistColor(cs, item.status),
                          ),
                    ),
                  ],
                ),
              )),
        ],
        const SizedBox(height: 10),
        Row(
          children: [
            Icon(Icons.receipt_long_outlined, size: 19, color: cs.primary),
            const SizedBox(width: 7),
            Text('Exact actions',
                style: Theme.of(context).textTheme.titleSmall),
          ],
        ),
        const SizedBox(height: 8),
        if (recentActions.isEmpty)
          const _OutputText('No tool actions recorded yet.')
        else
          ...recentActions.map((action) => Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(11),
                  decoration: BoxDecoration(
                    color: cs.surfaceContainerHighest,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(
                      color: action.ok
                          ? cs.outlineVariant
                          : cs.error.withValues(alpha: 0.55),
                    ),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(
                            action.ok
                                ? Icons.check_circle_outline
                                : Icons.error_outline,
                            size: 17,
                            color: action.ok ? cs.primary : cs.error,
                          ),
                          const SizedBox(width: 7),
                          Expanded(
                            child: Text(
                              action.title,
                              style: Theme.of(context)
                                  .textTheme
                                  .labelLarge
                                  ?.copyWith(fontWeight: FontWeight.w700),
                            ),
                          ),
                          Text('+${action.elapsedMs}ms'),
                        ],
                      ),
                      if (action.evidence.isNotEmpty) ...[
                        const SizedBox(height: 7),
                        SelectableText(
                          action.evidence,
                          style:
                              Theme.of(context).textTheme.bodySmall?.copyWith(
                                    fontFamily: 'monospace',
                                    height: 1.3,
                                  ),
                        ),
                      ],
                    ],
                  ),
                ),
              )),
      ],
    );
  }

  IconData _checklistIcon(String status) {
    if (status == 'done') return Icons.check_circle;
    if (status == 'in_progress') return Icons.pending;
    if (status == 'blocked') return Icons.error;
    return Icons.radio_button_unchecked;
  }

  Color _checklistColor(ColorScheme colors, String status) {
    if (status == 'done') return colors.primary;
    if (status == 'blocked') return colors.error;
    if (status == 'in_progress') return Colors.amber.shade800;
    return colors.outline;
  }
}

class _MeterBar extends StatelessWidget {
  final String label;
  final double percent;
  final String detail;
  final Color? color;

  const _MeterBar({
    required this.label,
    required this.percent,
    required this.detail,
    this.color,
  });

  @override
  Widget build(BuildContext context) {
    final value = (percent / 100).clamp(0.0, 1.0).toDouble();
    final barColor = color ?? Theme.of(context).colorScheme.primary;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            SizedBox(
              width: 96,
              child: Text(label, style: Theme.of(context).textTheme.labelLarge),
            ),
            Expanded(child: Text(detail)),
            const SizedBox(width: 8),
            Text('${percent.toStringAsFixed(1)}%'),
          ],
        ),
        const SizedBox(height: 6),
        LinearProgressIndicator(
          value: value,
          minHeight: 8,
          color: barColor,
          backgroundColor:
              Theme.of(context).colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(8),
        ),
      ],
    );
  }
}

class _StatusRow extends StatelessWidget {
  final String label;
  final String value;
  final bool ok;
  final VoidCallback? onCopy;

  const _StatusRow({
    required this.label,
    required this.value,
    required this.ok,
    this.onCopy,
  });

  @override
  Widget build(BuildContext context) {
    final color = ok
        ? Theme.of(context).colorScheme.primary
        : Theme.of(context).colorScheme.error;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            ok ? Icons.check_circle_outline : Icons.info_outline,
            color: color,
            size: 18,
          ),
          const SizedBox(width: 8),
          SizedBox(
            width: 120,
            child: Text(label, style: Theme.of(context).textTheme.labelLarge),
          ),
          const SizedBox(width: 12),
          Expanded(child: SelectableText(value)),
          if (onCopy != null)
            IconButton(
              tooltip: 'Copy',
              visualDensity: VisualDensity.compact,
              onPressed: onCopy,
              icon: const Icon(Icons.copy, size: 18),
            ),
        ],
      ),
    );
  }
}

class _Section extends StatelessWidget {
  final String title;
  final Widget child;

  const _Section({required this.title, required this.child});

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 10),
            child,
          ],
        ),
      ),
    );
  }
}

class _OutputCard extends StatelessWidget {
  final String text;
  final Widget? action;

  const _OutputCard({required this.text, this.action});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _OutputText(text),
          if (action != null) ...[
            const SizedBox(height: 8),
            Align(alignment: Alignment.centerRight, child: action!),
          ],
        ],
      ),
    );
  }
}

class _OutputText extends StatelessWidget {
  final String text;

  const _OutputText(this.text);

  @override
  Widget build(BuildContext context) {
    return SelectableText(
      text.isEmpty ? '(empty)' : text,
      style: Theme.of(context).textTheme.bodySmall?.copyWith(
            fontFamily: 'monospace',
            height: 1.3,
          ),
    );
  }
}
