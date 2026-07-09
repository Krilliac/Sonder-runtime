import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:trilobite/main.dart';

void main() {
  testWidgets('App boots to the chat screen', (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const TrilobiteApp(manageLocalServer: false));
    await tester.pumpAndSettle();

    // Model picker in the title bar shows the local model once settings resolve.
    expect(find.textContaining('trilobite'), findsWidgets);
    // Empty state shows the message composer.
    expect(find.byType(TextField), findsOneWidget);
  });
}
