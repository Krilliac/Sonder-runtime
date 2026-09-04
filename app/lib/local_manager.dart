// Browser builds never import native process, filesystem or Platform APIs.
export 'local_manager_models.dart';
export 'local_manager_web.dart'
    if (dart.library.io) 'local_manager_native.dart';
