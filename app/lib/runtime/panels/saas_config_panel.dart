import 'package:flutter/material.dart';
import '../../api.dart';
import '../../ui/kit.dart';

/// Admin panel to manage SaaS configuration and feature flags.
class SaasConfigPanel extends StatefulWidget {
  final SonderApi api;
  const SaasConfigPanel(this.api, {super.key});

  @override
  State<SaasConfigPanel> createState() => _SaasConfigPanelState();
}

class _SaasConfigPanelState extends State<SaasConfigPanel> {
  // Placeholder state for config fields
  final _freeTokens = TextEditingController(text: '50000');
  bool _meteringEnabled = true;
  bool _enterpriseEnabled = true;
  bool _cloudInferenceEnabled = true;
  bool _agentsEnabled = true;
  bool _adminUiEnabled = true;
  bool _observabilityEnabled = true;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(SonderSpace.md),
      child: ListView(
        children: [
          Text('SaaS Configuration', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: SonderSpace.md),
          SwitchListTile(
            title: const Text('Enable Usage Metering'),
            value: _meteringEnabled,
            onChanged: (v) => setState(() => _meteringEnabled = v),
          ),
          SwitchListTile(
            title: const Text('Enable Enterprise Tier'),
            value: _enterpriseEnabled,
            onChanged: (v) => setState(() => _enterpriseEnabled = v),
          ),
          SwitchListTile(
            title: const Text('Enable Cloud Inference'),
            value: _cloudInferenceEnabled,
            onChanged: (v) => setState(() => _cloudInferenceEnabled = v),
          ),
          SwitchListTile(
            title: const Text('Enable Advanced Agents'),
            value: _agentsEnabled,
            onChanged: (v) => setState(() => _agentsEnabled = v),
          ),
          SwitchListTile(
            title: const Text('Enable Admin UI'),
            value: _adminUiEnabled,
            onChanged: (v) => setState(() => _adminUiEnabled = v),
          ),
          SwitchListTile(
            title: const Text('Enable Observability'),
            value: _observabilityEnabled,
            onChanged: (v) => setState(() => _observabilityEnabled = v),
          ),
          TextField(
            controller: _freeTokens,
            decoration: const InputDecoration(labelText: 'Free Tier Monthly Token Limit'),
            keyboardType: TextInputType.number,
          ),
          const SizedBox(height: SonderSpace.md),
          FilledButton(
            onPressed: () {
              // TODO: Call API to save config
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(content: Text('Configuration saved')),
              );
            },
            child: const Text('Save Configuration'),
          ),
        ],
      ),
    );
  }
}
