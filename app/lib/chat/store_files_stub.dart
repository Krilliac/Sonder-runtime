import '../chat_store.dart';

/// Web has no file system; chat history stays in SharedPreferences.
ChatStoreBackend? directoryBackend(String path) => null;
