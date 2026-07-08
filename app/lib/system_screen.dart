import 'package:flutter/material.dart';

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
  SystemInfo? _info;
  String? _message;
  bool _loading = false;
  bool _working = false;

  TrilobiteApi get _api => TrilobiteApi(
        baseUrl: widget.settings.serverUrl,
        apiKey: widget.settings.apiKey,
      );

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
      setState(() => _info = info);
    } on TrilobiteException catch (e) {
      setState(() => _message = e.message);
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
    setState(() {
      _working = true;
      _message = null;
    });
    try {
      final reply = await _api.chat([
        ChatMessage(role: Role.user, content: command),
      ], model: widget.settings.model);
      setState(() => _message = reply);
    } on TrilobiteException catch (e) {
      setState(() => _message = e.message);
    } finally {
      if (mounted) setState(() => _working = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final info = _info;
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
          _Section(
            title: 'Local Runtime',
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton.icon(
                  onPressed: _working
                      ? null
                      : () => _run(LocalManager.startServer),
                  icon: const Icon(Icons.play_arrow_outlined),
                  label: const Text('Start server'),
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
                  onPressed: _working ? null : () => _sendCommand('/train 10'),
                  icon: const Icon(Icons.school_outlined),
                  label: const Text('Train 10'),
                ),
                OutlinedButton.icon(
                  onPressed: _working ? null : () => _sendCommand('/help'),
                  icon: const Icon(Icons.help_outline),
                  label: const Text('Help'),
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
            _Section(title: 'Learning', child: _OutputText(info.learnTiers)),
            const SizedBox(height: 12),
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
            'Android can connect to a LAN/hosted server, but cannot launch the Python server itself.',
            style: Theme.of(context).textTheme.bodySmall,
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
