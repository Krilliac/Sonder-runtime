part of '../runtime_screen.dart';

/// The quick commands: label and command.
const _quickCommands = <(String, String)>[
  ('Stats', '/stats'),
  ('Context', '/context'),
  ('Compact', '/compact'),
  ('Tasks', '/todo'),
  ('Quality', '/quality'),
  ('Improve', '/improve'),
  ('Agents', '/agents'),
  ('Capacity', '/capacity'),
  ('Commands', '/commands'),
  ('Dump', '/dump app'),
  ('Permissions', '/permissions'),
  ('Help', '/help'),
];

/// Developer: quick commands and a console whose replies land under it
/// (newest first, the last few kept), and the raw status report.
class _DeveloperPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _DeveloperPage(this.s);

  @override
  Widget build(BuildContext context) {
    final info = s._info;
    return _PageColumn(children: [
      SettingsSection(
        key: const Key('quick-commands'),
        title: 'Quick commands',
        description: 'Their replies land in the console below.',
        children: [
          RuntimeCardBody(
            child: LayoutBuilder(builder: (context, constraints) {
              const gap = SonderSpace.sm;
              const cell = 136.0;
              final columns = ((constraints.maxWidth + gap) / (cell + gap))
                  .floor()
                  .clamp(2, 6);
              final width =
                  (constraints.maxWidth - gap * (columns - 1)) / columns;
              Widget sized(Widget child) =>
                  SizedBox(width: width, child: child);
              return Wrap(
                spacing: gap,
                runSpacing: gap,
                children: [
                  for (final (label, command) in _quickCommands)
                    sized(_QuickCommand(
                      label: label,
                      tooltip: command,
                      onPressed: () => s._runConsole(command),
                    )),
                  sized(_QuickCommand(
                    label: 'Cancel active',
                    tooltip: '/agentcancel all',
                    confirm: s._confirmCancelAgents,
                    onPressed: s._cancelAgentsFromConsole,
                  )),
                ],
              );
            }),
          ),
        ],
      ),
      _ConsoleSection(s),
      SettingsSection(
        title: 'Raw status',
        description: 'The status report exactly as the server sent it.',
        children: [
          if (info == null)
            const RuntimeEmptyRow('No status loaded yet.',
                icon: Icons.cloud_off_outlined)
          else
            RawDisclosure(
              key: const Key('raw-status'),
              title: 'Status report',
              text: info.status.isEmpty ? '(empty)' : info.status,
            ),
        ],
      ),
    ]);
  }
}

class _QuickCommand extends StatelessWidget {
  final String label;

  /// The slash command it sends, shown on hover.
  final String tooltip;
  final Future<void> Function() onPressed;
  final Future<bool?> Function()? confirm;

  const _QuickCommand({
    required this.label,
    required this.tooltip,
    required this.onPressed,
    this.confirm,
  });

  @override
  Widget build(BuildContext context) {
    return AsyncActionButton(
      label: label,
      tooltip: tooltip,
      doneLabel: null,
      confirm: confirm,
      onPressed: onPressed,
      onError: (_, __) {},
    );
  }
}

/// The command console: a field and Send, then each reply under its command.
class _ConsoleSection extends StatelessWidget {
  final _RuntimeScreenState s;
  const _ConsoleSection(this.s);

  @override
  Widget build(BuildContext context) {
    final hasDone = s._console.any((entry) => !entry.running);
    return SettingsSection(
      key: const Key('runtime-console'),
      title: 'Console',
      description: 'Send any slash command. The last '
          '${_RuntimeScreenState._consoleKeep} replies stay here.',
      trailing: hasDone
          ? TextButton(onPressed: s._clearConsole, child: const Text('Clear'))
          : null,
      children: [
        RuntimeCardBody(
          child: Row(crossAxisAlignment: CrossAxisAlignment.center, children: [
            Expanded(
              child: TextField(
                key: const Key('runtime-command'),
                controller: s._consoleInput,
                autocorrect: false,
                style: SonderTokens.of(context).mono(13),
                decoration: const InputDecoration(
                  isDense: true,
                  hintText: '/diagnostics',
                  labelText: 'Command',
                ),
                onSubmitted: (value) =>
                    s._runConsole(value).catchError((Object _) {}),
              ),
            ),
            const SizedBox(width: SonderSpace.sm),
            AsyncActionButton(
              buttonKey: const Key('runtime-command-send'),
              label: 'Send',
              icon: Icons.send_outlined,
              busyLabel: 'Sending…',
              doneLabel: null,
              style: ActionButtonStyle.filled,
              onPressed: () => s._runConsole(s._consoleInput.text),
              onError: (_, __) {},
            ),
          ]),
        ),
        if (s._console.isEmpty)
          const RuntimeEmptyRow('Replies show here, newest first.',
              icon: Icons.terminal_outlined),
        for (final entry in s._console) _ConsoleEntryView(entry),
      ],
    );
  }
}

class _ConsoleEntryView extends StatelessWidget {
  final _ConsoleEntry entry;
  const _ConsoleEntryView(this.entry);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final outcome = entry.outcome;
    final kind = entry.running ? StatusKind.running : outcome!.kind;
    final word = entry.running ? 'working' : outcome!.shownWord;
    final output = outcome?.output;
    return Padding(
      key: Key('console-entry-${entry.id}'),
      padding: const EdgeInsets.all(SonderSpace.lg),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(crossAxisAlignment: CrossAxisAlignment.center, children: [
            Text('${statusGlyphs['prompt']} ',
                style: tokens.mono(13, color: tokens.accentText)),
            Expanded(
              child: SelectableText(entry.command,
                  maxLines: 1, style: tokens.mono(13, weight: FontWeight.w500)),
            ),
            Text(clockLabel(entry.at),
                style: tokens.mono(12, color: tokens.muted)),
            const SizedBox(width: SonderSpace.md),
            StatusMark(kind, word: word, size: 12),
          ]),
          if (!entry.running && output != null && output.trim().isNotEmpty) ...[
            const SizedBox(height: SonderSpace.sm),
            RawOutput(output, label: 'Reply'),
          ] else if (!entry.running && outcome != null) ...[
            const SizedBox(height: SonderSpace.xs),
            Text(
              outcome.detail ?? outcome.title,
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ],
        ],
      ),
    );
  }
}

/// About: what is running, and how the runtime fits together, said once.
class _AboutPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _AboutPage(this.s);

  static const architecture =
      'Sonder Runtime is the orchestration layer, not a standalone '
      'foundation model. Ollama loads and serves the local base-model '
      'weights; the runtime adds routing, prompts, memory, tools and '
      'policy. Adapter training runs through PEFT/Hugging Face, and only '
      'validated adapters or merged models are deployed to Ollama.';

  @override
  Widget build(BuildContext context) {
    final update = s._updateStatus;
    final runtime = s._ecosystem?.status;
    final version = update?.runningVersion ?? runtime?.runtimeVersion;
    final commit = update?.runningCommit ?? '';
    return _PageColumn(children: [
      SettingsSection(
        key: const Key('about-version'),
        title: 'This runtime',
        children: [
          ValueRow(
            label: 'Version',
            value:
                version == null || version.isEmpty ? 'Not reported' : version,
            mono: version != null && version.isNotEmpty,
          ),
          if (commit.isNotEmpty)
            ValueRow(
              label: 'Commit',
              value: commit.length > 12 ? commit.substring(0, 12) : commit,
              mono: true,
              copyable: true,
            ),
          if (update != null)
            ValueRow(
                label: 'Platform',
                value: '${update.platform}/${update.architecture}'),
          if (runtime?.nodeId != null)
            ValueRow(label: 'Node', value: runtime!.nodeId!, mono: true),
          ValueRow(
            label: 'Server',
            value: s.widget.settings.serverUrl,
            mono: true,
            copyable: true,
          ),
        ],
      ),
      const SettingsSection(
        key: Key('about-architecture'),
        title: 'How it fits together',
        children: [
          RuntimeCardBody(child: Text(architecture)),
        ],
      ),
    ]);
  }
}
