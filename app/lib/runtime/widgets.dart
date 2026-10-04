part of 'runtime_screen.dart';

/// What a tracked action shows under its control: a live progress line
/// while it runs (when there is something to say), then its outcome, and
/// for a failed local or launcher action the startup log, inline.
///
/// Null when there is nothing to show, so it fits a row's `below:` slot.
Widget? _trackedView(
  _RuntimeScreenState s,
  String id, {
  String? busyLabel,
  Key? busyKey,
  Key? failureKey,
}) {
  final tracked = s._tracked[id];
  if (tracked == null) return null;
  if (tracked.busy) {
    final text = [
      if (busyLabel != null) busyLabel,
      if (tracked.progress != null && tracked.progress!.isNotEmpty)
        tracked.progress!,
    ].join(' · ');
    if (text.isEmpty) return null;
    return _BusyLine(key: busyKey, text: text);
  }
  final outcome = tracked.outcome;
  if (outcome == null) return null;
  final local = tracked.local;
  if (local != null) {
    return _FailureView(
      key: failureKey,
      outcome: outcome,
      result: local,
      onDismiss: () => s._dismiss(id),
    );
  }
  return OutcomeView(outcome, onDismiss: () => s._dismiss(id));
}

/// `◈ working  Starting server… · Host start is waiting for health`.
class _BusyLine extends StatelessWidget {
  final String text;
  const _BusyLine({super.key, required this.text});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Semantics(
      liveRegion: true,
      label: 'working: $text',
      excludeSemantics: true,
      child: Row(children: [
        SizedBox(
          width: 14,
          height: 14,
          child: CircularProgressIndicator(
              strokeWidth: 2, color: tokens.accentText),
        ),
        const SizedBox(width: SonderSpace.md),
        Expanded(
          child: Text(text,
              style: Theme.of(context)
                  .textTheme
                  .bodyMedium
                  ?.copyWith(color: tokens.text2)),
        ),
      ]),
    );
  }
}

/// A failed local or launcher action: its message, and the startup log
/// opened in place (it used to be a blocking dialog).
class _FailureView extends StatefulWidget {
  final ActionOutcome outcome;
  final LocalActionResult result;
  final VoidCallback? onDismiss;

  const _FailureView({
    super.key,
    required this.outcome,
    required this.result,
    this.onDismiss,
  });

  @override
  State<_FailureView> createState() => _FailureViewState();
}

class _FailureViewState extends State<_FailureView> {
  bool _logOpen = false;

  @override
  Widget build(BuildContext context) {
    final result = widget.result;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        OutcomeView(
          ActionOutcome(widget.outcome.kind, widget.outcome.title,
              detail: result.message.isEmpty ? null : result.message,
              word: widget.outcome.word),
          onDismiss: widget.onDismiss,
        ),
        if (result.hasLogDetail)
          Padding(
            padding: const EdgeInsets.only(top: SonderSpace.xs),
            child: Wrap(spacing: SonderSpace.sm, children: [
              if (result.logTail.isNotEmpty)
                TextButton.icon(
                  key: const Key('runtime-failure-log'),
                  onPressed: () => setState(() => _logOpen = !_logOpen),
                  icon: Icon(
                      _logOpen ? Icons.expand_less : Icons.description_outlined,
                      size: 18),
                  label:
                      Text(_logOpen ? 'Hide startup log' : 'View startup log'),
                ),
              if (result.logPath.isNotEmpty)
                TextButton.icon(
                  key: result.logTail.isEmpty
                      ? const Key('runtime-failure-log')
                      : null,
                  onPressed: () async {
                    await Clipboard.setData(
                        ClipboardData(text: result.logPath));
                    if (context.mounted) {
                      showSonderToast(context, 'Startup log path copied');
                    }
                  },
                  icon: const Icon(Icons.copy_outlined, size: 18),
                  label: const Text('Copy log path'),
                ),
            ]),
          ),
        SonderReveal(
          visible: _logOpen && result.logTail.isNotEmpty,
          child: Padding(
            padding: const EdgeInsets.only(top: SonderSpace.sm),
            child: RawOutput(result.logTail,
                label: 'Startup log', collapsedLines: 18),
          ),
        ),
      ],
    );
  }
}

/// A slash command shown as a copyable mono line, for things that are only
/// done from the console ("/runtime set workbench=general").
class _CommandLine extends StatelessWidget {
  final String command;
  final String label;

  const _CommandLine(this.command, {required this.label});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Container(
      padding: const EdgeInsets.only(left: SonderSpace.md),
      decoration: BoxDecoration(
        color: tokens.canvas,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        border: Border.all(color: tokens.hairline),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Flexible(
          child: SelectableText(command,
              maxLines: 1, style: tokens.mono(12.5, color: tokens.text)),
        ),
        IconButton(
          tooltip: 'Copy $label',
          iconSize: 16,
          icon: const Icon(Icons.copy_outlined),
          onPressed: () async {
            await Clipboard.setData(ClipboardData(text: command));
            if (context.mounted) showSonderToast(context, 'Command copied');
          },
        ),
      ]),
    );
  }
}

/// The line under a [Meter] that says what its figure counts.
class _MeterCaption extends StatelessWidget {
  final String text;
  const _MeterCaption(this.text);

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: SonderSpace.xs),
        child: Text(text, style: Theme.of(context).textTheme.bodySmall),
      );
}

/// The column every page builds on: sections stacked with the kit's rhythm.
class _PageColumn extends StatelessWidget {
  final List<Widget> children;
  const _PageColumn({required this.children});

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: children,
      );
}

/// A section that has nothing to show because the status has not loaded.
class _NotLoadedSection extends StatelessWidget {
  final String title;
  final bool loading;

  const _NotLoadedSection({required this.title, required this.loading});

  @override
  Widget build(BuildContext context) => SettingsSection(
        title: title,
        children: [
          loading
              ? SkeletonRows(rows: 2, semanticLabel: 'Loading $title')
              : const RuntimeEmptyRow('No status loaded yet.',
                  icon: Icons.cloud_off_outlined),
        ],
      );
}
