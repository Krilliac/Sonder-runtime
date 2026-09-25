part of 'runtime_screen.dart';

class _SystemRail extends StatelessWidget {
  final ValueChanged<GlobalKey> onSelect;
  final GlobalKey runtimeKey;
  final GlobalKey autopilotKey;
  final GlobalKey activityKey;
  final GlobalKey learningKey;
  final GlobalKey policyKey;
  final GlobalKey extensionsKey;

  const _SystemRail({
    required this.onSelect,
    required this.runtimeKey,
    required this.autopilotKey,
    required this.activityKey,
    required this.learningKey,
    required this.policyKey,
    required this.extensionsKey,
  });

  @override
  Widget build(BuildContext context) {
    final destinations = [
      (label: 'Runtime', icon: Icons.tune_outlined, key: runtimeKey),
      (
        label: 'Autopilot',
        icon: Icons.rocket_launch_outlined,
        key: autopilotKey
      ),
      (label: 'Activity', icon: Icons.timeline_outlined, key: activityKey),
      (label: 'Learning', icon: Icons.school_outlined, key: learningKey),
      (label: 'Policy', icon: Icons.security_outlined, key: policyKey),
      (label: 'Extensions', icon: Icons.extension_outlined, key: extensionsKey),
    ];
    return NavigationRail(
      key: const Key('system-section-rail'),
      extended: MediaQuery.sizeOf(context).width >= 1200,
      minExtendedWidth: 220,
      selectedIndex: null,
      onDestinationSelected: (index) => onSelect(destinations[index].key),
      labelType: MediaQuery.sizeOf(context).width >= 1200
          ? NavigationRailLabelType.none
          : NavigationRailLabelType.all,
      destinations: [
        for (final destination in destinations)
          NavigationRailDestination(
            icon: Icon(destination.icon),
            label: Text(destination.label),
          ),
      ],
    );
  }
}

class _SystemCompactNav extends StatelessWidget {
  final ValueChanged<GlobalKey> onSelect;
  final GlobalKey runtimeKey;
  final GlobalKey autopilotKey;
  final GlobalKey activityKey;
  final GlobalKey learningKey;
  final GlobalKey policyKey;
  final GlobalKey extensionsKey;

  const _SystemCompactNav({
    required this.onSelect,
    required this.runtimeKey,
    required this.autopilotKey,
    required this.activityKey,
    required this.learningKey,
    required this.policyKey,
    required this.extensionsKey,
  });

  @override
  Widget build(BuildContext context) {
    final destinations = [
      (label: 'Runtime', icon: Icons.tune_outlined, key: runtimeKey),
      (
        label: 'Autopilot',
        icon: Icons.rocket_launch_outlined,
        key: autopilotKey
      ),
      (label: 'Activity', icon: Icons.timeline_outlined, key: activityKey),
      (label: 'Learning', icon: Icons.school_outlined, key: learningKey),
      (label: 'Policy', icon: Icons.security_outlined, key: policyKey),
      (label: 'Extensions', icon: Icons.extension_outlined, key: extensionsKey),
    ];
    return Semantics(
      container: true,
      label: 'System sections',
      child: SizedBox(
        key: const Key('system-section-nav'),
        height: 52,
        child: ListView.separated(
          scrollDirection: Axis.horizontal,
          itemCount: destinations.length,
          separatorBuilder: (_, __) => const SizedBox(width: 8),
          itemBuilder: (context, index) {
            final destination = destinations[index];
            return ActionChip(
              avatar: Icon(destination.icon, size: 17),
              label: Text(destination.label),
              onPressed: () => onSelect(destination.key),
            );
          },
        ),
      ),
    );
  }
}
