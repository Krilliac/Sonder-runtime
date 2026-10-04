/// The follow-up composer, on Chat's surface: a panel with a strong
/// hairline, the field without its own border, a quiet shortcut hint and a
/// 36 px send button in a 48 dp target. Ctrl/Cmd+Enter sends; Enter adds a
/// newline; an active input-method composition is never sent
/// (UX-CONTRACT.md).
library;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme.dart';
import '../ui/status_vocab.dart';
import '../workspace_ui.dart' show conversationWidth;

/// Whether the platform's primary modifier is Command.
bool get _apple =>
    defaultTargetPlatform == TargetPlatform.macOS ||
    defaultTargetPlatform == TargetPlatform.iOS;

class AgentComposer extends StatelessWidget {
  final TextEditingController controller;
  final FocusNode focusNode;
  final Key fieldKey;
  final bool enabled;
  final bool canSend;
  final VoidCallback onSend;
  final ValueChanged<String> onChanged;

  /// A one-line note above the field, e.g. that messages wait for Resume.
  final String? note;
  final StatusKind noteKind;

  /// Shows the keyboard hint (desktop widths).
  final bool showHint;

  const AgentComposer({
    super.key,
    required this.controller,
    required this.focusNode,
    required this.fieldKey,
    required this.enabled,
    required this.canSend,
    required this.onSend,
    required this.onChanged,
    this.note,
    this.noteKind = StatusKind.note,
    this.showHint = true,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    Widget field(EdgeInsets padding) => CallbackShortcuts(
          bindings: {
            const SingleActivator(LogicalKeyboardKey.enter, control: true):
                onSend,
            const SingleActivator(LogicalKeyboardKey.enter, meta: true): onSend,
          },
          child: TextField(
            key: fieldKey,
            controller: controller,
            focusNode: focusNode,
            enabled: enabled,
            minLines: 1,
            maxLines: 6,
            keyboardType: TextInputType.multiline,
            textInputAction: TextInputAction.newline,
            style: text.bodyMedium,
            onChanged: onChanged,
            decoration: InputDecoration(
              labelText: 'Message this agent',
              floatingLabelBehavior: FloatingLabelBehavior.never,
              filled: false,
              contentPadding: padding,
              border: InputBorder.none,
              enabledBorder: InputBorder.none,
              focusedBorder: InputBorder.none,
              disabledBorder: InputBorder.none,
            ),
          ),
        );
    final send = SizedBox(
      width: 48,
      height: 48,
      child: Center(
        child: IconButton.filled(
          tooltip: 'Send to agent',
          onPressed: canSend ? onSend : null,
          style: IconButton.styleFrom(
            fixedSize: const Size(36, 36),
            minimumSize: const Size(36, 36),
            padding: EdgeInsets.zero,
            backgroundColor: tokens.accent,
            foregroundColor: tokens.onAccent,
            disabledBackgroundColor: tokens.raised,
            disabledForegroundColor: tokens.muted,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(SonderRadius.row),
            ),
          ),
          icon: const Icon(Icons.arrow_upward, size: 18),
        ),
      ),
    );
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(
            SonderSpace.md, SonderSpace.xs, SonderSpace.md, SonderSpace.sm),
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: conversationWidth),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (note != null)
                  Padding(
                    padding: const EdgeInsets.fromLTRB(
                        SonderSpace.xs, 0, SonderSpace.xs, SonderSpace.sm),
                    child: Row(children: [
                      ExcludeSemantics(
                        child: Text(noteKind.glyph,
                            style: tokens.mono(12,
                                color: noteKind.color(tokens),
                                weight: FontWeight.w600)),
                      ),
                      const SizedBox(width: SonderSpace.sm),
                      Expanded(
                        child: Text(note!,
                            style: text.bodySmall?.copyWith(
                                color: noteKind == StatusKind.warn
                                    ? tokens.warn
                                    : tokens.text2)),
                      ),
                    ]),
                  ),
                AnimatedContainer(
                  duration: SonderMotion.of(context, SonderMotion.fast),
                  decoration: BoxDecoration(
                    color: enabled ? tokens.panel : tokens.canvas,
                    borderRadius: BorderRadius.circular(SonderRadius.sheet),
                    border: Border.all(
                        color:
                            enabled ? tokens.hairlineStrong : tokens.hairline),
                  ),
                  child: showHint
                      ? Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            field(const EdgeInsets.fromLTRB(
                                SonderSpace.md,
                                SonderSpace.md,
                                SonderSpace.md,
                                SonderSpace.xs)),
                            Padding(
                              padding: const EdgeInsets.fromLTRB(SonderSpace.sm,
                                  0, SonderSpace.sm, SonderSpace.xs),
                              child: Row(children: [
                                Expanded(
                                  child: Padding(
                                    padding: const EdgeInsets.symmetric(
                                        horizontal: SonderSpace.sm),
                                    child: Text(
                                      '${_apple ? 'Cmd' : 'Ctrl'} Enter send · Enter newline',
                                      textAlign: TextAlign.right,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style:
                                          tokens.mono(11, color: tokens.muted),
                                    ),
                                  ),
                                ),
                                send,
                              ]),
                            ),
                          ],
                        )
                      // Narrow: no keyboard hint, so the send button sits
                      // beside the field instead of on a row of its own.
                      : Row(
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: [
                            Expanded(
                              child: field(const EdgeInsets.fromLTRB(
                                  SonderSpace.md,
                                  SonderSpace.md + SonderSpace.xxs,
                                  SonderSpace.xs,
                                  SonderSpace.md + SonderSpace.xxs)),
                            ),
                            Padding(
                              padding: const EdgeInsets.fromLTRB(
                                  0, 0, SonderSpace.xs, SonderSpace.xxs),
                              child: send,
                            ),
                          ],
                        ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
