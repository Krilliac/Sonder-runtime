import 'package:shared_preferences/shared_preferences.dart';

/// The shell's own remembered layout: whether the wide sidebar is
/// collapsed to its icon rail. Kept apart from `Settings` because it is a
/// view preference, saved the moment it changes, never part of a form.
abstract final class ShellPreferences {
  static const sidebarCollapsedKey = 'sonder_sidebar_collapsed';

  static Future<bool> sidebarCollapsed() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      return prefs.getBool(sidebarCollapsedKey) ?? false;
    } catch (_) {
      return false;
    }
  }

  static Future<void> setSidebarCollapsed(bool collapsed) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool(sidebarCollapsedKey, collapsed);
    } catch (_) {
      // A view preference that fails to save costs nothing but the memory.
    }
  }
}
