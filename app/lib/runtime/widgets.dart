part of 'runtime_screen.dart';

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
    final tokens = SonderTokens.of(context);
    final value = (percent / 100).clamp(0.0, 1.0).toDouble();
    final barColor = color ?? tokens.accent;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.baseline,
          textBaseline: TextBaseline.alphabetic,
          children: [
            SizedBox(
              width: 96,
              child: Text(label, style: Theme.of(context).textTheme.labelLarge),
            ),
            Expanded(
              child: Text(detail, style: tokens.mono(12, color: tokens.text2)),
            ),
            const SizedBox(width: 8),
            Text(
              '${percent.toStringAsFixed(1)}%',
              style: tokens.mono(12, weight: FontWeight.w500),
            ),
          ],
        ),
        const SizedBox(height: 6),
        LinearProgressIndicator(
          value: value,
          minHeight: 4,
          color: barColor,
          backgroundColor: tokens.hairline,
          borderRadius: BorderRadius.circular(2),
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
    final tokens = SonderTokens.of(context);
    final color = ok ? tokens.ok : tokens.danger;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Container(
              width: 7,
              height: 7,
              decoration: BoxDecoration(
                color: color,
                borderRadius: BorderRadius.circular(4),
              ),
            ),
          ),
          const SizedBox(width: 10),
          SizedBox(
            width: 120,
            child: Text(label, style: Theme.of(context).textTheme.labelLarge),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: SelectableText(value, style: tokens.mono(12)),
          ),
          if (onCopy != null)
            IconButton(
              tooltip: 'Copy',
              visualDensity: VisualDensity.compact,
              onPressed: onCopy,
              icon: const Icon(Icons.copy, size: 16),
            ),
        ],
      ),
    );
  }
}

/// One section of the System screen: an eyebrow, a hairline, its content.
/// Sections are breaks in one column rather than cards, so the screen reads
/// as a single instrument panel and the anchors the rail scrolls to stay
/// exactly where they were.
class _Section extends StatelessWidget {
  final String title;
  final Widget child;

  const _Section({super.key, required this.title, required this.child});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Padding(
      padding: const EdgeInsets.only(top: 6, bottom: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: Theme.of(context).textTheme.labelSmall),
          const SizedBox(height: 8),
          Divider(height: 1, color: tokens.hairline),
          const SizedBox(height: 12),
          child,
        ],
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
    final tokens = SonderTokens.of(context);
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        border: Border.all(color: tokens.hairline),
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

/// Persistent, above-the-fold report for a failed runtime action. The message
/// already names what failed and where the startup log is; the button reopens
/// the log tail after the modal has been dismissed.
class _RuntimeFailureCard extends StatelessWidget {
  final String label;
  final LocalActionResult result;
  final VoidCallback onShowLog;

  const _RuntimeFailureCard({
    required this.label,
    required this.result,
    required this.onShowLog,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Container(
      key: const Key('runtime-failure'),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: cs.errorContainer,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: cs.error),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(Icons.error_outline, size: 18, color: cs.onErrorContainer),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  label.isEmpty ? 'Action failed' : '$label failed',
                  style: Theme.of(context).textTheme.labelLarge?.copyWith(
                        color: cs.onErrorContainer,
                      ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          SelectableText(
            result.message,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: cs.onErrorContainer,
                  fontFamily: SonderTheme.mono,
                  height: 1.3,
                ),
          ),
          if (result.hasLogDetail) ...[
            const SizedBox(height: 8),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                key: const Key('runtime-failure-log'),
                onPressed: onShowLog,
                icon: const Icon(Icons.description_outlined, size: 18),
                label: const Text('View startup log'),
              ),
            ),
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
            fontFamily: SonderTheme.mono,
            height: 1.3,
          ),
    );
  }
}
