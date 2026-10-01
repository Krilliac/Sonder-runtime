import 'dart:async';

import 'package:flutter/material.dart';

import '../../theme.dart';
import '../status_vocab.dart';

enum ActionButtonStyle { filled, outlined, text }

/// A button that runs an async action and shows its own progress: a spinner
/// while it runs (after a short delay, so fast actions never flicker), then
/// a brief "✓ done" or "✗ failed" before returning to its label.
///
/// Each button owns its busy state. Running one action no longer disables
/// every other control on the page.
class AsyncActionButton extends StatefulWidget {
  final String label;
  final IconData? icon;
  final Future<void> Function()? onPressed;
  final ActionButtonStyle style;

  /// Shown while running, e.g. "Starting…". Defaults to [label].
  final String? busyLabel;

  /// Shown briefly after success, e.g. "Started". Null skips the done state.
  final String? doneLabel;
  final String? tooltip;

  /// Receives a failure. Without it the error is reported to FlutterError,
  /// so a failure is never silent.
  final void Function(Object error, StackTrace stack)? onError;

  /// Key for the underlying Material button.
  final Key? buttonKey;

  const AsyncActionButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.icon,
    this.style = ActionButtonStyle.outlined,
    this.busyLabel,
    this.doneLabel = 'Done',
    this.tooltip,
    this.onError,
    this.buttonKey,
  });

  @override
  State<AsyncActionButton> createState() => _AsyncActionButtonState();
}

enum _Phase { idle, busy, done, failed }

class _AsyncActionButtonState extends State<AsyncActionButton> {
  _Phase _phase = _Phase.idle;
  bool _spinnerShown = false;
  Timer? _spinnerDelay;
  Timer? _settle;

  static const _spinnerAfter = Duration(milliseconds: 120);
  static const _doneFor = Duration(milliseconds: 1400);
  static const _failedFor = Duration(milliseconds: 2200);

  @override
  void dispose() {
    _spinnerDelay?.cancel();
    _settle?.cancel();
    super.dispose();
  }

  Future<void> _run() async {
    final action = widget.onPressed;
    if (action == null || _phase == _Phase.busy) return;
    _settle?.cancel();
    setState(() {
      _phase = _Phase.busy;
      _spinnerShown = false;
    });
    _spinnerDelay = Timer(_spinnerAfter, () {
      if (mounted && _phase == _Phase.busy) {
        setState(() => _spinnerShown = true);
      }
    });
    try {
      await action();
      if (!mounted) return;
      _spinnerDelay?.cancel();
      if (widget.doneLabel == null) {
        setState(() => _phase = _Phase.idle);
        return;
      }
      setState(() => _phase = _Phase.done);
      _settle = Timer(_doneFor, () {
        if (mounted) setState(() => _phase = _Phase.idle);
      });
    } catch (error, stack) {
      _spinnerDelay?.cancel();
      if (mounted) {
        setState(() => _phase = _Phase.failed);
        _settle = Timer(_failedFor, () {
          if (mounted) setState(() => _phase = _Phase.idle);
        });
      }
      final onError = widget.onError;
      if (onError != null) {
        onError(error, stack);
      } else {
        FlutterError.reportError(FlutterErrorDetails(
          exception: error,
          stack: stack,
          library: 'sonder kit',
          context: ErrorDescription('running "${widget.label}"'),
        ));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final busy = _phase == _Phase.busy;
    final String label;
    Widget? leading;
    switch (_phase) {
      case _Phase.busy:
        label = widget.busyLabel ?? widget.label;
        leading = _spinnerShown
            ? SizedBox(
                width: 14,
                height: 14,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  color: widget.style == ActionButtonStyle.filled
                      ? tokens.onAccent
                      : tokens.accentText,
                ),
              )
            : (widget.icon == null ? null : Icon(widget.icon, size: 18));
      case _Phase.done:
        label = widget.doneLabel ?? widget.label;
        leading = Text(StatusKind.ok.glyph,
            style: tokens.mono(13,
                color: widget.style == ActionButtonStyle.filled
                    ? tokens.onAccent
                    : tokens.ok,
                weight: FontWeight.w600));
      case _Phase.failed:
        label = 'Failed';
        leading = Text(StatusKind.fail.glyph,
            style: tokens.mono(13,
                color: widget.style == ActionButtonStyle.filled
                    ? tokens.onAccent
                    : tokens.danger,
                weight: FontWeight.w600));
      case _Phase.idle:
        label = widget.label;
        leading = widget.icon == null ? null : Icon(widget.icon, size: 18);
    }
    final onPressed = widget.onPressed == null || busy ? null : _run;
    final child = AnimatedSize(
      duration: SonderMotion.of(context, SonderMotion.fast),
      curve: SonderMotion.standard,
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        if (leading != null) ...[leading, const SizedBox(width: SonderSpace.sm)],
        Text(label),
      ]),
    );
    final Widget button = switch (widget.style) {
      ActionButtonStyle.filled =>
        FilledButton(key: widget.buttonKey, onPressed: onPressed, child: child),
      ActionButtonStyle.outlined => OutlinedButton(
          key: widget.buttonKey, onPressed: onPressed, child: child),
      ActionButtonStyle.text =>
        TextButton(key: widget.buttonKey, onPressed: onPressed, child: child),
    };
    final semantic = Semantics(
      liveRegion: _phase != _Phase.idle,
      child: button,
    );
    if (widget.tooltip == null) return semantic;
    return Tooltip(message: widget.tooltip!, child: semantic);
  }
}
