import 'package:flutter/material.dart';
import '../theme.dart';
import '../chat_store.dart';

class ToneProvider extends InheritedWidget {
  final double tone;
  const ToneProvider({super.key, required this.tone, required super.child});

  static double of(BuildContext context) {
    return context.dependOnInheritedWidgetOfExactType<ToneProvider>()?.tone ?? 0.0;
  }

  @override
  bool updateShouldNotify(ToneProvider oldWidget) => tone != oldWidget.tone;
}

class TonalTheme extends StatelessWidget {
  final Widget child;
  const TonalTheme({super.key, required this.child});

  @override
  Widget build(BuildContext context) {
    final tone = ToneProvider.of(context);
    final theme = Theme.of(context);
    final tokens = SonderTokens.of(context).withTone(tone);
    
    return Theme(
      data: theme.copyWith(extensions: [tokens]),
      child: child,
    );
  }
}
