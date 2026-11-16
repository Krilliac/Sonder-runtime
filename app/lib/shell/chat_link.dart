import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../ui/kit.dart';

/// Sharing a chat with a local assistant (Claude Code and the like) and
/// opening one from a link. Everything stays on this machine: a link or code
/// names a session id, nothing is uploaded.
///
/// * link: `<origin>/app/#/c/<session_id>`
/// * code: `sonder:chat/<session_id>`
///
/// The session id is the server session the chat writes to (the thread id,
/// or `<id>-<n>` after a rotated first turn), so an assistant can read the
/// transcript with `GET /v1/sessions/<session_id>/transcript`.

/// What a share menu item puts on the clipboard.
enum ChatShareKind { link, code }

/// `[A-Za-z0-9_.-]{1,128}`, never `.` / `..`: the same rule the server and the
/// MCP resolver apply. Checked before an id is looked up or sent anywhere.
final RegExp _chatRefPattern = RegExp(r'^[A-Za-z0-9_.\-]{1,128}$');

bool isValidChatRef(String ref) =>
    _chatRefPattern.hasMatch(ref) && ref.replaceAll('.', '').isNotEmpty;

/// The shareable code for [sessionId].
String chatShareCode(String sessionId) => 'sonder:chat/$sessionId';

/// The link for [sessionId] on [origin] (`http://host:port`, any trailing
/// slash tolerated), or null when [origin] is not an http(s) origin.
String? chatShareLink(String sessionId, String? origin) {
  if (origin == null) return null;
  final uri = Uri.tryParse(origin.trim());
  if (uri == null ||
      !(uri.scheme == 'http' || uri.scheme == 'https') ||
      uri.host.isEmpty) {
    return null;
  }
  return '${uri.origin}/app/#/c/$sessionId';
}

/// Copy the link or code for [sessionId] and say so. Returns what was copied,
/// or null when the item has nothing to copy (no usable origin).
Future<String?> copyChatShare(
  BuildContext context,
  ChatShareKind kind,
  String sessionId, {
  required String? origin,
}) async {
  final text = switch (kind) {
    ChatShareKind.link => chatShareLink(sessionId, origin),
    ChatShareKind.code => chatShareCode(sessionId),
  };
  if (text == null) return null;
  await Clipboard.setData(ClipboardData(text: text));
  if (context.mounted) {
    showSonderToast(context, 'Copied');
  }
  return text;
}

/// A request to open a chat, from a `/c/<id>` route. A fresh [serial] makes
/// the same id requestable twice.
@immutable
class ChatLinkRequest {
  final String ref;
  final int serial;
  const ChatLinkRequest(this.ref, this.serial);
}

/// The pending deep link. The shell consumes it once chats are loaded.
class ChatDeepLink {
  ChatDeepLink._();

  static final ValueNotifier<ChatLinkRequest?> pending =
      ValueNotifier<ChatLinkRequest?>(null);
  static int _serial = 0;

  static void request(String ref, {ValueNotifier<ChatLinkRequest?>? into}) {
    (into ?? pending).value = ChatLinkRequest(ref, ++_serial);
  }
}

/// The id segment of a `/c/<id>` route name, else null. The segment is
/// returned as typed (it may be invalid; [isValidChatRef] decides).
String? chatRefFromRoute(String? route) {
  if (route == null) return null;
  var path = route.trim();
  if (path.startsWith('#')) path = path.substring(1);
  const prefix = '/c/';
  if (!path.startsWith(prefix)) return null;
  var ref = path.substring(prefix.length);
  final end = ref.indexOf(RegExp(r'[?#]'));
  if (end >= 0) ref = ref.substring(0, end);
  if (ref.endsWith('/')) ref = ref.substring(0, ref.length - 1);
  if (ref.isEmpty) return null;
  try {
    return Uri.decodeComponent(ref);
  } on ArgumentError {
    return ref;
  }
}

/// `onGenerateRoute` for `/c/<id>`: hands the id to [ChatDeepLink] and leaves
/// no page behind (the shell is already underneath). Any other route is left
/// to the app (null).
Route<void>? chatLinkRoute(RouteSettings settings,
    {ValueNotifier<ChatLinkRequest?>? into}) {
  final ref = chatRefFromRoute(settings.name);
  if (ref == null) return null;
  return PageRouteBuilder<void>(
    settings: settings,
    opaque: false,
    transitionDuration: Duration.zero,
    reverseTransitionDuration: Duration.zero,
    pageBuilder: (context, _, __) => _ChatLinkLanding(ref: ref, into: into),
  );
}

class _ChatLinkLanding extends StatefulWidget {
  final String ref;
  final ValueNotifier<ChatLinkRequest?>? into;
  const _ChatLinkLanding({required this.ref, this.into});

  @override
  State<_ChatLinkLanding> createState() => _ChatLinkLandingState();
}

class _ChatLinkLandingState extends State<_ChatLinkLanding> {
  @override
  void initState() {
    super.initState();
    ChatDeepLink.request(widget.ref, into: widget.into);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final navigator = Navigator.of(context);
      if (navigator.canPop()) navigator.pop();
    });
  }

  @override
  Widget build(BuildContext context) => const SizedBox.shrink();
}
