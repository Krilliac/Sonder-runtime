import '../api.dart';

/// Offline fallback for the command palette.
///
/// The real surface lives in the server's `command_catalog.py` and arrives
/// over GET /v1/commands; this short list is what the palette falls back to
/// when that fetch fails, so typing "/" offline still offers something
/// rather than an empty panel.
const quickCommands = <String, String>{
  '/stats': 'Show learning stats',
  '/context': 'Show context health',
  '/compact': 'Preview context compaction',
  '/commands': 'List command registry',
  '/activity': 'Show live tool/file activity',
  '/runtime': 'Show shared local model routing',
  '/mcp': 'Audit live MCP source/tool convergence',
  '/learning': 'Inspect grounded learning and memory quality',
  '/artifactcheck': 'Validate a generated file or artifact pack',
  '/asset office-suite DOCX report, XLSX workbook, PPTX deck':
      'Generate a grounded editable Office suite',
  '/asset media-suite AVI video, animated GIF, MIDI score, SRT WebVTT captions, EDL timeline':
      'Generate a grounded editable media kit',
  '/asset rigged-character textured PBR humanoid GLB with a 17-bone rig, full morph frames, and sequenced Idle Walk Run clips':
      'Generate a grounded animated humanoid character',
  '/autopilot': 'Plan or run a persistent guarded goal',
  '/report': 'Show latest end report and exact actions',
  '/checklist': 'Show the active work checklist',
  '/inventory': 'Summarize the guarded workspace',
  '/privacy': 'Review redacted memory privacy findings',
  '/tree': 'Inspect the guarded workspace tree',
  '/programs python': 'Find the local Python runtime',
  '/dump': 'Save chat/debug dump',
  '/todo': 'Show visible task state',
  '/quality': 'Audit memory quality',
  '/emotion': 'Show or tune tone vectors',
  '/prefer': 'Show or teach preferences',
  '/improve': 'Show next improvements',
  '/agents': 'Show live agent activity',
  '/capacity': 'Show hardware-safe fleet capacity',
  '/agentretry': 'Retry interrupted persisted master work',
  '/forge': 'Build and test the in-house reference game suite',
  '/permissions': 'Show permission rules',
  '/master': 'Choose inline or delegated execution',
  '/runwindow': 'Launch last code in a Windows console',
  '/help': 'List commands',
  '/train': 'Grounded practice; does not update model weights',
  '/pass': 'Mark last answer good',
  '/accept': 'Mark last answer useful',
  '/edited': 'Mark answer used after edits',
  '/fail': 'Mark last answer bad',
};

/// Names promoted to the top of the offline palette, mirroring the shape of
/// the server's own `popular` list.
const fallbackPopular = <String>[
  '/help',
  '/commands',
  '/stats',
  '/context',
  '/todo',
  '/activity',
  '/report',
  '/dump',
];

/// [quickCommands] as a catalog so the palette has one code path whether or
/// not the server answered. Risk is left blank rather than guessed.
final CommandCatalog fallbackCatalog = CommandCatalog(
  commands: quickCommands.entries
      .map(
        (e) => SonderCommand(
          name: e.key,
          category: 'quick',
          summary: e.value,
          native: true,
          usage: e.key,
        ),
      )
      .toList(growable: false),
  categories: const {'quick': 'Built-in quick commands (offline fallback)'},
  popular: fallbackPopular,
);
