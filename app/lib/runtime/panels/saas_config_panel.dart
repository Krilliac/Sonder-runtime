import 'package:flutter/material.dart';

import '../../theme.dart';

/// Admin page for the SaaS tier and feature-flag configuration.
///
/// The server exposes no endpoint to read or change that configuration yet,
/// so this page says so instead of showing controls that would not persist.
/// SaaS mode and usage metering stay off unless the server is configured for
/// them.
class SaasConfigPanel extends StatelessWidget {
  const SaasConfigPanel({super.key});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(SonderSpace.md),
      child: ListView(
        children: [
          Text('SaaS Configuration',
              style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: SonderSpace.md),
          const Text(
            'SaaS mode, usage metering and per-feature toggles are set on the '
            'server. This app cannot read or change them yet, so nothing here '
            'is editable.',
          ),
        ],
      ),
    );
  }
}
