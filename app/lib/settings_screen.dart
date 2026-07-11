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
  late final TextEditingController _model;
  late final TextEditingController _contextSize;
  late final TextEditingController _username;
  late final TextEditingController _password;
  late bool _dark;
  late bool _allowHosted;
  late bool _keepServerRunning;
  late bool _allowApproximateLocation;
  bool _obscureKey = true;
  String? _status;
  bool _statusOk = false;
  bool _testing = false;

  @override
  void initState() {
    super.initState();
    _server = TextEditingController(text: widget.settings.serverUrl);
    _key = TextEditingController(text: widget.settings.apiKey);
    _model = TextEditingController(text: widget.settings.model);
    _contextSize = TextEditingController(text: widget.settings.contextSize);
    _username = TextEditingController();
    _password = TextEditingController();
    _dark = widget.settings.darkMode;
    _allowHosted = widget.settings.allowHosted;
    _keepServerRunning = widget.settings.keepServerRunning;
    _allowApproximateLocation = widget.settings.allowApproximateLocation;
  }

  @override
  void dispose() {
    _server.dispose();
    _key.dispose();
    _model.dispose();
    _contextSize.dispose();
    _username.dispose();
    _password.dispose();
    super.dispose();
  }

  Settings _current() => Settings(
        serverUrl: _server.text,
        apiKey: _key.text,
        darkMode: _dark,
        allowHosted: _allowHosted,
        contextSize: _contextSize.text.trim().isEmpty
            ? '8192'
            : _contextSize.text.trim(),
        keepServerRunning: _keepServerRunning,
        allowApproximateLocation: _allowApproximateLocation,
        model: _model.text.trim().isEmpty
            ? Settings.defaultModel
            : _model.text.trim(),
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

  Future<void> _register() async {
    await _accountAction(register: true);
  }

  Future<void> _login() async {
    await _accountAction(register: false);
  }

  Future<void> _accountAction({required bool register}) async {
    setState(() {
      _testing = true;
      _status = null;
    });
    final api = TrilobiteApi(baseUrl: _server.text, apiKey: _key.text);
    try {
      if (register) {
        final msg = await api.register(_username.text, _password.text);
        setState(() {
          _statusOk = true;
          _status = msg;
        });
      } else {
        final token = await api.login(_username.text, _password.text);
        setState(() {
          _key.text = token;
          _statusOk = true;
          _status = 'Logged in. Token saved in the API key/token field.';
        });
      }
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
      appBar: AppBar(
        automaticallyImplyLeading: false,
        leading: IconButton(
          tooltip: 'Back to chat',
          onPressed: () => Navigator.of(context).maybePop(),
          icon: const Icon(Icons.arrow_back),
        ),
        title: const Text('Settings'),
        actions: [
          Tooltip(
            message: 'Return to main chat',
            child: TextButton.icon(
              onPressed: () => Navigator.of(context).maybePop(),
              icon: const Icon(Icons.chat_bubble_outline, size: 18),
              label: const Text('Chat'),
            ),
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          Text('Connection', style: Theme.of(context).textTheme.titleMedium),
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
                icon:
                    Icon(_obscureKey ? Icons.visibility : Icons.visibility_off),
                onPressed: () => setState(() => _obscureKey = !_obscureKey),
              ),
              border: const OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _model,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Default model / tier',
              hintText: 'trilobite, code, fast...',
              helperText: 'Used by chat and system commands',
              prefixIcon: Icon(Icons.memory_outlined),
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _contextSize,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Context size',
              hintText: '8192, 32k, 256k, 1m',
              helperText:
                  'Requested virtual context. Ollama native num_ctx is clamped safely.',
              prefixIcon: Icon(Icons.view_week_outlined),
              border: OutlineInputBorder(),
            ),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Allow hosted/cloud tiers'),
            subtitle: const Text(
              'Opt-in only. Prompts sent to cloud tiers leave this machine.',
            ),
            value: _allowHosted,
            onChanged: (v) => setState(() => _allowHosted = v),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Keep local server running after app closes'),
            subtitle: const Text(
              'Use this for headless/background mode. Turn it off if the app '
              'should stop its local server on exit.',
            ),
            value: _keepServerRunning,
            onChanged: (v) => setState(() => _keepServerRunning = v),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            secondary: const Icon(Icons.location_searching_outlined),
            title: const Text('Allow approximate IP location'),
            subtitle: const Text(
              'Off by default. For weather or nearby requests, the app asks '
              'ipwho.is for an approximate city/region. Raw IP is never sent '
              'to Trilobite, displayed, or retained.',
            ),
            value: _allowApproximateLocation,
            onChanged: (v) => setState(() => _allowApproximateLocation = v),
          ),
          const Divider(height: 40),
          Text('Account', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 12),
          TextField(
            controller: _username,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Username',
              prefixIcon: Icon(Icons.person_outline),
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _password,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            decoration: const InputDecoration(
              labelText: 'Password',
              helperText: 'At least 8 characters. First account becomes admin.',
              prefixIcon: Icon(Icons.lock_outline),
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              FilledButton.tonalIcon(
                onPressed: _testing ? null : _register,
                icon: const Icon(Icons.person_add_alt),
                label: const Text('Register'),
              ),
              FilledButton.icon(
                onPressed: _testing ? null : _login,
                icon: const Icon(Icons.login),
                label: const Text('Login'),
              ),
            ],
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
                    color:
                        _statusOk ? cs.onPrimaryContainer : cs.onErrorContainer,
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
          Text('Appearance', style: Theme.of(context).textTheme.titleMedium),
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
            'Trilobite is local-first. Hosted tiers, explicit web tools, and '
            'approximate location can contact external services only when you '
            'enable or invoke them. Run the server yourself with '
            'deploy_trilobite.sh --serve.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}
