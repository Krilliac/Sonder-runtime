import 'dart:async';

import 'package:flutter/material.dart';

import '../theme.dart';
import '../ui/sonder_mark.dart';

/// What the app shows while it reads its settings: the mark and the name,
/// calm on the canvas. A thin progress line fades in only if loading takes
/// long enough to notice, so a fast start never flashes a spinner.
class ShellSplash extends StatefulWidget {
  /// How long before the progress line appears.
  final Duration progressAfter;

  const ShellSplash({
    super.key,
    this.progressAfter = const Duration(milliseconds: 400),
  });

  @override
  State<ShellSplash> createState() => _ShellSplashState();
}

class _ShellSplashState extends State<ShellSplash> {
  Timer? _timer;
  bool _showProgress = false;

  @override
  void initState() {
    super.initState();
    _timer = Timer(widget.progressAfter, () {
      if (mounted) setState(() => _showProgress = true);
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Scaffold(
      key: const Key('shell-splash'),
      backgroundColor: tokens.canvas,
      body: Center(
        child: Semantics(
          label: 'Starting Sonder',
          liveRegion: true,
          child: ExcludeSemantics(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const SonderMark(size: 48),
                const SizedBox(height: SonderSpace.lg),
                Text('Sonder',
                    style: text.titleMedium?.copyWith(
                        fontWeight: FontWeight.w600, color: tokens.text)),
                const SizedBox(height: SonderSpace.xxl),
                SizedBox(
                  width: 96,
                  height: 2,
                  child: AnimatedOpacity(
                    opacity: _showProgress ? 1 : 0,
                    duration: SonderMotion.of(context, SonderMotion.slow),
                    curve: SonderMotion.standard,
                    child: _showProgress
                        ? ClipRRect(
                            borderRadius:
                                BorderRadius.circular(SonderRadius.pill),
                            child: LinearProgressIndicator(
                              minHeight: 2,
                              color: tokens.accent,
                              backgroundColor: tokens.hairline,
                            ),
                          )
                        : const SizedBox.shrink(),
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
