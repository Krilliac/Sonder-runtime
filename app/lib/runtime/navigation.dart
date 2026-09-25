part of 'runtime_screen.dart';

/// One place the Runtime page can jump to: the Overview or a Details group.
class _RuntimeDestination {
  final String id;
  final String label;
  final IconData icon;
  final GlobalKey key;
  const _RuntimeDestination(this.id, this.label, this.icon, this.key);
}

/// Wide layouts: a rail whose items scroll to (and open) one section.
class _SystemRail extends StatelessWidget {
  final List<_RuntimeDestination> destinations;
  final ValueChanged<_RuntimeDestination> onSelect;

  const _SystemRail({required this.destinations, required this.onSelect});

  @override
  Widget build(BuildContext context) {
    final extended = MediaQuery.sizeOf(context).width >= 1200;
    return Semantics(
      container: true,
      label: 'Jump to section',
      explicitChildNodes: true,
      child: NavigationRail(
        key: const Key('system-section-rail'),
        extended: extended,
        minExtendedWidth: 220,
        selectedIndex: null,
        onDestinationSelected: (index) => onSelect(destinations[index]),
        labelType: extended
            ? NavigationRailLabelType.none
            : NavigationRailLabelType.all,
        destinations: [
          for (final destination in destinations)
            NavigationRailDestination(
              icon: Tooltip(
                message: 'Jump to ${destination.label}',
                child: Icon(destination.icon),
              ),
              label: Text(destination.label),
            ),
        ],
      ),
    );
  }
}

/// Narrow layouts: the same destinations as a horizontal chip row.
class _SystemCompactNav extends StatelessWidget {
  final List<_RuntimeDestination> destinations;
  final ValueChanged<_RuntimeDestination> onSelect;

  const _SystemCompactNav({required this.destinations, required this.onSelect});

  @override
  Widget build(BuildContext context) {
    return Semantics(
      container: true,
      label: 'Jump to section',
      explicitChildNodes: true,
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
              tooltip: 'Jump to ${destination.label}',
              onPressed: () => onSelect(destination),
            );
          },
        ),
      ),
    );
  }
}

/// A collapsible "Details" group: a ≥48 dp header that says what it holds,
/// then its sections. Collapsed groups build nothing below the header.
class _DetailsGroup extends StatelessWidget {
  final String title;
  final String? summary;
  final bool expanded;
  final ValueChanged<bool> onChanged;
  final List<Widget> children;

  const _DetailsGroup({
    super.key,
    required this.title,
    required this.expanded,
    required this.onChanged,
    required this.children,
    this.summary,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Semantics(
            button: true,
            expanded: expanded,
            label: '$title details',
            excludeSemantics: true,
            child: InkWell(
              onTap: () => onChanged(!expanded),
              borderRadius: BorderRadius.circular(SonderRadius.row),
              child: ConstrainedBox(
                constraints: const BoxConstraints(minHeight: 48),
                child: Row(children: [
                  Icon(expanded ? Icons.expand_more : Icons.chevron_right,
                      size: 20, color: tokens.muted),
                  const SizedBox(width: 10),
                  Flexible(
                    child: Text(title,
                        style: text.titleSmall,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis),
                  ),
                  if (summary != null && summary!.isNotEmpty) ...[
                    const SizedBox(width: 10),
                    Flexible(
                      child: Text(summary!,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: tokens.mono(12, color: tokens.muted)),
                    ),
                  ],
                ]),
              ),
            ),
          ),
          Divider(height: 1, color: tokens.hairline),
          if (expanded)
            Padding(
              padding: const EdgeInsets.only(top: 8, bottom: 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: children,
              ),
            ),
        ],
      ),
    );
  }
}
