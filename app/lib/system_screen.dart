import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'local_manager.dart';
import 'models.dart';
import 'settings.dart';

class SystemScreen extends StatefulWidget {
  final Settings settings;

  const SystemScreen({super.key, required this.settings});

  @override
  State<SystemScreen> createState() => _SystemScreenState();
}

class _SystemScreenState extends State<SystemScreen> {
  final _customCommand = TextEditingController(text: '/diagnostics');
  final _trainCount = TextEditingController(text: '10');
  SystemInfo? _info;
  LocalInstallInfo? _localInfo;
  String? _message;
  bool _loading = false;
  bool _working = false;

  TrilobiteApi get _api => TrilobiteApi(
        baseUrl: widget.settings.serverUrl,
        apiKey: widget.settings.apiKey,
      );

  @override
  void dispose() {
    _customCommand.dispose();
    _trainCount.dispose();
    super.dispose();
  }

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    setState(() {
      _loading = true;
      _message = null;
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
      ], model: widget.settings.model, contextSize: widget.settings.contextSize);
      setState(() => _message = reply);
    } on TrilobiteException catch (e) {
      setState(() => _message = e.message);
    } finally {
      if (mounted) setState(() => _working = false);
    }
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
        title: const Text('System'),
        actions: [
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
                  onPressed: _working
                      ? null
                      : () => _run(LocalManager.updateFromGit),
                  icon: const Icon(Icons.system_update_alt),
                  label: const Text('Update from Git'),
                ),
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
                  onPressed: _working ? null : () => _sendCommand('/commands'),
                  icon: const Icon(Icons.terminal_outlined),
                  label: const Text('Commands'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/permissions'),
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
            if (info.context != null) ...[
              _Section(
                title: 'Context Health',
                child: _ContextHealthPanel(health: info.context!),
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
                child: _AgentStatusPanel(status: info.agents!),
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
              label: Text('${health.contextMode}: native ${health.nativeContextLimit}'),
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

class _AgentStatusPanel extends StatelessWidget {
  final AgentStatus status;

  const _AgentStatusPanel({required this.status});

  @override
  Widget build(BuildContext context) {
    final recent = status.agents.take(6).toList();
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
              label: Text('${status.totalListed} listed'),
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
                  text:
                      '${agent.id} [${agent.status}] ${agent.activity}\n'
                      'role=${agent.role} calls=${agent.toolCalls} '
                      'tokens=${agent.tokensIn}/${agent.tokensOut}\n'
                      'task: ${agent.task}',
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
              child:
                  Text(label, style: Theme.of(context).textTheme.labelLarge),
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
          backgroundColor: Theme.of(context).colorScheme.surfaceContainerHighest,
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
            width: 112,
            child: Text(label, style: Theme.of(context).textTheme.labelLarge),
          ),
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

  const _OutputCard({required this.text});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(8),
      ),
      child: _OutputText(text),
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
