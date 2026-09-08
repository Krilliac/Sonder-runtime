// Browser-only probe: dart compile js test/agent_command_id_web.dart -o probe.js
// Load probe.js after <body> in a local HTML page. A passing run writes PASS.
import 'dart:js_interop';
import 'package:sonder_runtime/agent_command_id.dart';

@JS('document.body.textContent')
external set resultText(String text);

void main() {
  final seen = <String>{};
  final pattern = RegExp(r'^ui-[0-9a-f]{32}$');
  for (var i = 0; i < 1000; i++) {
    final id = newAgentCommandId();
    if (!pattern.hasMatch(id) || !seen.add(id)) {
      throw StateError('Invalid or duplicate command identity');
    }
  }
  resultText = 'PASS: 1000 web command identities generated without errors.';
}
