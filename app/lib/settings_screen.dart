import 'package:flutter/material.dart';

import 'api.dart';
import 'settings.dart';

/// Connection settings: server URL, API key, theme, plus a "Test connection"
/// button that hits /v1/models so the user gets immediate feedback.
class SettingsScreen extends StatefulWidget {
  final Settings settings;
  final ValueChanged<Settings> onChanged;

  const SettingsScreen({
    super.key,
    required this.settings,
    required this.onChanged,
  });

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _server;
  late final TextEditingController _key;
  late bool _dark;
  bool _obscureKey = true;
  String? _status;
  bool _statusOk = false;
  bool _testing = false;

  @override
  void initState() {
    super.initState();
    _server = TextEditingController(text: widget.settings.serverUrl);
    _key = TextEditingController(text: widget.settings.apiKey);
    _dark = widget.settings.darkMode;
  }

  @override
  void dispose() {
    _server.dispose();
    _key.dispose();
    super.dispose();
  }

  Settings _current() => Settings(
        serverUrl: _server.text,
        apiKey: _key.text,
        darkMode: _dark,
      );

  Future<void> _test() async {
    setState(() {
      _testing = true;
      _status = null;
    });
    final api = TrilobiteApi(
      baseUrl: _server.text,
      apiKey: _key.text,
    );
    try {
      final models = await api.listModels();
      setState(() {
        _statusOk = true;
        _status = 'Connected. Models: ${models.join(", ")}';
      });
    } on TrilobiteException catch (e) {
      setState(() {
        _statusOk = false;
        _status = e.message;
      });
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  void _save() {
    final s = _current();
    s.save();
    widget.onChanged(s);
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Settings saved')),
    );
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          Text('Connection',
              style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 12),
          TextField(
            controller: _server,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Server URL',
              hintText: 'http://your-host:11435',
              helperText: 'Where trilobite_serve.py is listening',
              prefixIcon: Icon(Icons.dns_outlined),
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _key,
            obscureText: _obscureKey,
            autocorrect: false,
            enableSuggestions: false,
            decoration: InputDecoration(
              labelText: 'API key (optional)',
              helperText: 'Leave blank if the server has auth disabled',
              prefixIcon: const Icon(Icons.key_outlined),
              suffixIcon: IconButton(
                icon: Icon(
                    _obscureKey ? Icons.visibility : Icons.visibility_off),
                onPressed: () =>
                    setState(() => _obscureKey = !_obscureKey),
              ),
              border: const OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          FilledButton.tonalIcon(
            onPressed: _testing ? null : _test,
            icon: _testing
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.wifi_tethering),
            label: Text(_testing ? 'Testing…' : 'Test connection'),
          ),
          if (_status != null) ...[
            const SizedBox(height: 12),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: (_statusOk ? cs.primaryContainer : cs.errorContainer),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Row(
                children: [
                  Icon(
                    _statusOk ? Icons.check_circle : Icons.error_outline,
                    color: _statusOk
                        ? cs.onPrimaryContainer
                        : cs.onErrorContainer,
                    size: 20,
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      _status!,
                      style: TextStyle(
                        color: _statusOk
                            ? cs.onPrimaryContainer
                            : cs.onErrorContainer,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
          const Divider(height: 40),
          Text('Appearance',
              style: Theme.of(context).textTheme.titleMedium),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Dark mode'),
            value: _dark,
            onChanged: (v) => setState(() => _dark = v),
          ),
          const SizedBox(height: 24),
          FilledButton.icon(
            onPressed: _save,
            icon: const Icon(Icons.save_outlined),
            label: const Text('Save'),
          ),
          const SizedBox(height: 24),
          Text(
            'Trilobite is a private, local AI. Nothing you type leaves the '
            'server you point this app at. Run that server yourself with '
            'deploy_trilobite.sh --serve.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}
