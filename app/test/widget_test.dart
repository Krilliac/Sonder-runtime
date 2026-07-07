import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:trilobite/main.dart';

void main() {
  testWidgets('App boots to the chat screen', (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const TrilobiteApp());
    await tester.pumpAndSettle();

    // AppBar title renders once settings resolve.
    expect(find.text('trilobite'), findsOneWidget);
    // Empty state shows the message composer.
    expect(find.byType(TextField), findsOneWidget);
  });
}
