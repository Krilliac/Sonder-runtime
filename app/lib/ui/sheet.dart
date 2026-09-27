import 'package:flutter/material.dart';

import '../theme.dart';
import 'status_row.dart';
import 'status_vocab.dart';

/// Width at which confirmation sheets become centred dialogs.
const sheetDialogBreakpoint = 600.0;

/// Shows [builder] as a modal bottom sheet on a phone and as a dialog on a
/// wide window (APP-PLAN §2.5). The result is whatever the content pops.
Future<T?> showSonderSheet<T>(BuildContext context,
    {required WidgetBuilder builder}) {
  final wide = MediaQuery.sizeOf(context).width >= sheetDialogBreakpoint;
  if (wide) {
    return showDialog<T>(
      context: context,
      builder: (context) => Dialog(
        insetPadding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520),
          child: builder(context),
        ),
      ),
    );
  }
  final tokens = SonderTokens.of(context);
  return showModalBottomSheet<T>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    showDragHandle: true,
    backgroundColor: tokens.panel,
    shape: RoundedRectangleBorder(
      borderRadius:
          const BorderRadius.vertical(top: Radius.circular(SonderRadius.sheet)),
      side: BorderSide(color: tokens.hairline),
    ),
    builder: builder,
  );
}

/// The shared layout of a confirmation sheet: a status header
/// (`? approve   write_file · call 3f9a12c0`), a scrollable body, and the
/// action row with the primary action last.
class SonderSheetFrame extends StatelessWidget {
  final StatusKind kind;
  final String word;
  final String title;
  final List<Widget> children;
  final List<Widget> actions;

  /// The accessible name of the sheet ("Approve this call").
  final String semanticsLabel;

  const SonderSheetFrame({
    super.key,
    required this.kind,
    required this.word,
    required this.title,
    required this.children,
    required this.actions,
    required this.semanticsLabel,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Semantics(
      scopesRoute: true,
      namesRoute: true,
      explicitChildNodes: true,
      label: semanticsLabel,
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Semantics(
              header: true,
              child: Wrap(
                spacing: 12,
                runSpacing: 4,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  StatusMark(kind, word: word, size: 14),
                  Text(title,
                      style: tokens.mono(14,
                          color: tokens.text, weight: FontWeight.w500)),
                ],
              ),
            ),
            const SizedBox(height: 16),
            DefaultTextStyle.merge(
              style: text.bodyMedium?.copyWith(color: tokens.text2),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: children,
              ),
            ),
            const SizedBox(height: 20),
            Align(
              alignment: Alignment.centerRight,
              child: Wrap(
                alignment: WrapAlignment.end,
                spacing: 8,
                runSpacing: 8,
                children: actions,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// A filled button in a risk tone: warn for raising to acceptEdits or
/// approving a call, danger for raising to auto. The label keeps 4.5:1 on
/// the fill in both themes (test/theme_contrast_test.dart).
class ToneButton extends StatelessWidget {
  final String label;
  final StatusRole role;
  final VoidCallback? onPressed;
  final bool busy;

  const ToneButton({
    super.key,
    required this.label,
    required this.role,
    required this.onPressed,
    this.busy = false,
  });

  /// The fill and label colours for [role] in [tokens].
  static (Color, Color) colors(SonderTokens tokens, StatusRole role) {
    final fill = roleColor(tokens, role);
    final dark = tokens.canvas.computeLuminance() < 0.2;
    return (fill, dark ? tokens.canvas : tokens.panel);
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final (fill, onFill) = colors(tokens, role);
    return FilledButton(
      style: FilledButton.styleFrom(
        backgroundColor: fill,
        foregroundColor: onFill,
        minimumSize: const Size(0, 44),
      ),
      onPressed: busy ? null : onPressed,
      child: busy
          ? SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(strokeWidth: 2, color: fill))
          : Text(label),
    );
  }
}
