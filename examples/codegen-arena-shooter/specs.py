"""Per-file contracts for the Sonder-authored arena shooter.

Split out from the harness so the harness stays a harness. Every contract is a
*transformation* prompt -- the exact API, the exact field names, the exact
algorithm -- because that is measurably where a 7B-class local model performs,
and recall (guessing an API it was not told) is where it fails.
"""

API_BRIEF = """\
TARGET: C# 10 / .NET 10, namespace ArenaShooter, using the Raylib-cs 8.0 NuGet package.

Exact Raylib-cs API (these signatures are correct; never invent others):
  Raylib.InitWindow(int w, int h, string title) / Raylib.CloseWindow() / Raylib.WindowShouldClose()
  Raylib.SetTargetFPS(int) / Raylib.GetFrameTime() -> float
  Raylib.BeginDrawing() / Raylib.EndDrawing() / Raylib.ClearBackground(Color)
  Raylib.BeginMode3D(Camera3D) / Raylib.EndMode3D()
  Raylib.DrawCube(Vector3 pos, float w, float h, float len, Color)
  Raylib.DrawCubeWires(Vector3 pos, float w, float h, float len, Color)
  Raylib.DrawPlane(Vector3 center, Vector2 size, Color)
  Raylib.DrawLine3D(Vector3 a, Vector3 b, Color)
  Raylib.DrawText(string, int x, int y, int fontSize, Color)
  Raylib.MeasureText(string, int fontSize) -> int
  Raylib.DrawRectangle(int x, int y, int w, int h, Color)
  Raylib.DrawRectangleLines(int x, int y, int w, int h, Color)
  Raylib.DrawLine(int x1, int y1, int x2, int y2, Color)
  Raylib.DrawCircle(int centerX, int centerY, float radius, Color)
  Raylib.IsKeyDown(KeyboardKey.W) / Raylib.IsKeyPressed(KeyboardKey.Enter)
  Raylib.IsMouseButtonDown(MouseButton.Left) / Raylib.IsMouseButtonPressed(MouseButton.Left)
  Raylib.GetMousePosition() -> Vector2 / Raylib.GetMouseDelta() -> Vector2
  Raylib.DisableCursor() / Raylib.EnableCursor()
  Raylib.GetCharPressed() -> int / Raylib.GetKeyPressed() -> int
  new Color(int r, int g, int b, int a); Color.White, Color.Black, Color.Red, Color.Green, Color.Blue, Color.Yellow, Color.Gray
  new Camera3D(Vector3 position, Vector3 target, Vector3 up, float fovY, CameraProjection.Perspective)
  KeyboardKey.Backspace, KeyboardKey.Escape, KeyboardKey.Tab, KeyboardKey.Space, KeyboardKey.LeftShift, KeyboardKey.R

Required usings as needed: System, System.Collections.Generic, System.Numerics,
System.Net, System.Net.Sockets, System.Text, Raylib_cs

HARD RULES:
- Output ONE C# file. No prose, no explanation, no markdown fences, no comments block at the top.
- Begin output at the first `using` line.
- Write a Main method ONLY in the file explicitly asked for one.
- Reference only types from the Raylib API above, the .NET base library, or the
  shared contract below. Never invent a helper that is not specified.
- All math in float. Vector3/Vector2 come from System.Numerics.
- Do not use `record`, top-level statements, or file-scoped types other than what is asked.
"""

# The single source of truth every file is told about. Kept small on purpose:
# a 7B cannot hold a large architecture, so each file only needs this much.
SHARED_CONTRACT = """\
SHARED TYPES ACROSS THIS PROJECT (already defined in the files listed; use them, never redefine them):

  // --- ClassKit.cs ---
  public enum ClassId { Assault, Scout, Heavy }
  public sealed class ClassKit
      public ClassId Id; public string Name; public int MaxHealth; public float MoveSpeed;
      public int Damage; public float FireDelay; public int MaxAmmo; public Color Tint;
      public static ClassKit Get(ClassId id)
      public static ClassId[] All

  // --- GameMap.cs ---
  public sealed class GameMap
      public const float CellSize = 4f;
      public const float WallHeight = 4f;
      public int Width; public int Depth;
      public List<Vector3> SpawnPoints;
      public bool IsWallCell(int x, int z)
      public bool IsWallAt(Vector3 p)
      public bool CircleHitsWall(Vector3 p, float radius)
      public Vector3 MoveWithSlide(Vector3 pos, Vector3 delta, float radius)
      public float RayWallDistance(Vector3 origin, Vector3 dir, float maxDist)
      public IEnumerable<(int x, int z)> WallCells()
      public static Vector3 CellCenter(int x, int z)

  // --- Combatant.cs ---
  public sealed class Combatant
      public int Id; public string Name; public int Team;      // 0 or 1
      public ClassId Kit; public Vector3 Position; public float Yaw; public float Pitch;
      public int Health; public int Ammo; public int Kills; public int Deaths;
      public bool IsBot; public float RespawnTimer; public float FireCooldown; public float HitFlash;
      public bool Alive
      public Combatant(int id, string name, int team, ClassId kit)
      public void ApplyKit()
      public void TakeDamage(int amount)
      public Vector3 Forward

  // --- NetProtocol.cs ---
  public static class NetProtocol
      public const int Port = 45123;
      public static string EncodeState(List<Combatant> players, float matchTime)
      public static void ApplyState(string payload, List<Combatant> players)
      public static string EncodeInput(int id, Vector3 pos, float yaw, float pitch, bool firing)
      public static (int id, Vector3 pos, float yaw, float pitch, bool firing)? DecodeInput(string payload)

  // --- MatchState.cs ---
  public enum MatchPhase { Warmup, Live, Ended }
  public sealed class MatchState
      public MatchPhase Phase; public float TimeRemaining; public int ScoreLimit;
      public int[] TeamScore;                                   // length 2
      public List<Combatant> Players;
      public void Update(float dt, GameMap map)
      public void RegisterKill(Combatant killer, Combatant victim)
      public Combatant AddBot(GameMap map)
      public Combatant AddLocalPlayer(string name, int team, ClassId kit, GameMap map)
      public void RespawnAt(Combatant c, GameMap map)

  // --- Screens.cs ---
  public enum Screen { MainMenu, ClassSelect, Lobby, Match, Scoreboard }
"""


def files():
    """Ordered (filename, contract) pairs. Order matters: earlier files are the
    ones later files are told about."""
    return [
        ("ClassKit.cs", CLASSKIT),
        ("GameMap.cs", GAMEMAP),
        ("Combatant.cs", COMBATANT),
        ("NetProtocol.cs", NETPROTOCOL),
        ("MatchState.cs", MATCHSTATE),
        ("LobbyNet.cs", LOBBYNET),
        ("Screens.cs", SCREENS),
        ("Program.cs", PROGRAM),
    ]


CLASSKIT = """\
Write ClassKit.cs.

public enum ClassId { Assault, Scout, Heavy }

public sealed class ClassKit. EVERY ONE of these is a `public` field -- write
the word `public` on each declaration separately, do not rely on it carrying
across a semicolon:
  public ClassId Id;
  public string Name;
  public int MaxHealth;
  public float MoveSpeed;
  public int Damage;
  public float FireDelay;
  public int MaxAmmo;
  public Color Tint;

public static ClassKit Get(ClassId id) returns a new ClassKit with these EXACT values:
  Assault: Name "ASSAULT", MaxHealth 100, MoveSpeed 8.0f, Damage 26, FireDelay 0.11f, MaxAmmo 120, Tint new Color(90,170,255,255)
  Scout:   Name "SCOUT",   MaxHealth 75,  MoveSpeed 11.5f, Damage 40, FireDelay 0.32f, MaxAmmo 60,  Tint new Color(120,230,140,255)
  Heavy:   Name "HEAVY",   MaxHealth 160, MoveSpeed 5.8f,  Damage 18, FireDelay 0.08f, MaxAmmo 200, Tint new Color(255,160,80,255)
  Use a switch expression and default to the Assault values.

public static ClassId[] All => new[] { ClassId.Assault, ClassId.Scout, ClassId.Heavy };

Also add: public static string Describe(ClassId id) returning a one-line string
like "ASSAULT  HP 100  SPD 8.0  DMG 26" built from Get(id).
"""

GAMEMAP = """\
Write GameMap.cs.

public sealed class GameMap holds a fixed grid arena and all collision queries.

Store this map as `private static readonly string[] MapData`, one string per row.
'#' is a wall, '.' is open floor, 'S' is a spawn point:

    "########################",
    "#S....#..........#....S#",
    "#.....#..........#.....#",
    "#.....#....##....#.....#",
    "#..........##..........#",
    "#..####............####.#",
    "#..#..#..........#..#..#",
    "#..#..#...S..S...#..#..#",
    "#..#..............#.#..#",
    "#..####..######..####..#",
    "#......................#",
    "#..####..######..####..#",
    "#..#..............#.#..#",
    "#..#..#...S..S...#..#..#",
    "#..#..#..........#..#..#",
    "#..####............####.#",
    "#..........##..........#",
    "#.....#....##....#.....#",
    "#.....#..........#.....#",
    "#S....#..........#....S#",
    "########################",

Implement exactly:
- public int Width  = the length of the longest row; public int Depth = number of rows.
- public List<Vector3> SpawnPoints, filled in the constructor with CellCenter(x, z)
  for every 'S'. If the list ends up empty, add CellCenter(1, 1).
- public static Vector3 CellCenter(int x, int z) => new Vector3((x + 0.5f) * CellSize, 0f, (z + 0.5f) * CellSize)
- public bool IsWallCell(int x, int z): return TRUE when z < 0, z >= Depth, x < 0,
  or x >= that row's length (out of bounds is solid); otherwise MapData[z][x] == '#'.
- public bool IsWallAt(Vector3 p): x = (int)MathF.Floor(p.X / CellSize),
  z = (int)MathF.Floor(p.Z / CellSize), then IsWallCell(x, z).
- public bool CircleHitsWall(Vector3 p, float radius): true if IsWallAt is true at
  any of the 8 points offset from p by +/- radius on X alone, on Z alone, and on
  both together.
- public Vector3 MoveWithSlide(Vector3 pos, Vector3 delta, float radius):
  resolve one axis at a time so the mover slides along walls. Start with
  next = pos. Try new Vector3(next.X + delta.X, next.Y, next.Z): if
  CircleHitsWall is false there, assign it to next. Then try
  new Vector3(next.X, next.Y, next.Z + delta.Z) the same way. Return next.
- public float RayWallDistance(Vector3 origin, Vector3 dir, float maxDist):
  normalize dir, step t from 0 by 0.15f while t < maxDist, return the first t
  where IsWallAt(origin + dir * t); return maxDist if never.
- public bool HasLineOfSight(Vector3 a, Vector3 b): let diff = b - a and
  dist = diff.Length(); if dist < 0.001f return true; return
  RayWallDistance(a, diff / dist, dist) >= dist.
- public IEnumerable<(int x, int z)> WallCells(): yield every '#' cell coordinate.
"""

COMBATANT = """\
Write Combatant.cs.

public sealed class Combatant is one fighter -- local player, remote player, or bot.

Public fields exactly: int Id; string Name; int Team; ClassId Kit; Vector3 Position;
float Yaw; float Pitch; int Health; int Ammo; int Kills; int Deaths; bool IsBot;
float RespawnTimer; float FireCooldown; float HitFlash;

- public bool Alive => Health > 0;
- public Combatant(int id, string name, int team, ClassId kit): assign Id, Name,
  Team, Kit, set Position to Vector3.Zero, then call ApplyKit().
- public void ApplyKit(): look up ClassKit.Get(Kit) and set Health = MaxHealth and
  Ammo = MaxAmmo.
- public void TakeDamage(int amount): Health = Math.Max(0, Health - amount); HitFlash = 1f;
- public void Tick(float dt): FireCooldown = MathF.Max(0f, FireCooldown - dt);
  HitFlash = MathF.Max(0f, HitFlash - dt * 4f);
- public Vector3 Forward => new Vector3(MathF.Cos(Pitch) * MathF.Sin(Yaw), MathF.Sin(Pitch), MathF.Cos(Pitch) * MathF.Cos(Yaw));
- public Vector3 FlatForward => new Vector3(MathF.Sin(Yaw), 0f, MathF.Cos(Yaw));
- public Vector3 FlatRight => new Vector3(MathF.Cos(Yaw), 0f, -MathF.Sin(Yaw));
- public bool CanFire => Alive && FireCooldown <= 0f && Ammo > 0;
- public void RegisterShot(): Ammo--; FireCooldown = ClassKit.Get(Kit).FireDelay;

Also add in the same file:

public static class BotBrain
- public static void Update(Combatant bot, List<Combatant> all, GameMap map, float dt, Action<Combatant, Combatant> onShoot):
  If !bot.Alive return.
  Find the nearest living Combatant in `all` whose Team differs from bot.Team and
  for which map.HasLineOfSight(bot.Position, target.Position) is true. If none, return.
  Face it: set bot.Yaw = MathF.Atan2(d.X, d.Z) where d = target.Position - bot.Position.
  If the distance is greater than 6f, move toward it:
  step = Vector3.Normalize(d) * ClassKit.Get(bot.Kit).MoveSpeed * 0.55f * dt, then
  bot.Position = map.MoveWithSlide(bot.Position, step, 0.55f) keeping the original Y.
  If bot.CanFire and the distance is under 40f, call bot.RegisterShot() and then
  onShoot(bot, target).
"""

NETPROTOCOL = """\
Write NetProtocol.cs.

public static class NetProtocol serializes match data as plain text lines so it
can travel over UDP without any external library.

- public const int Port = 45123;
- Use System.Globalization.CultureInfo.InvariantCulture for EVERY float parse and
  format, so a comma-decimal locale cannot corrupt the wire format.

public static string EncodeState(List<Combatant> players, float matchTime):
  First line: "S|" + matchTime formatted with "0.00".
  Then one line per player:
  "P|id|name|team|(int)kit|x|y|z|yaw|health|kills|deaths"
  with all floats formatted "0.00" and fields joined by '|'. Join lines with '\\n'.

public static void ApplyState(string payload, List<Combatant> players):
  Split payload on '\\n'. For each line starting with "P|", parse the fields.
  Find the Combatant in `players` whose Id matches; if none exists, create one
  with new Combatant(id, name, team, (ClassId)kit) and add it to the list.
  Then set its Position, Yaw, Health, Kills and Deaths from the message.
  Wrap the per-line parse in try/catch and skip a malformed line rather than
  throwing -- a corrupt packet must never take down the game.

public static string EncodeInput(int id, Vector3 pos, float yaw, float pitch, bool firing):
  return "I|id|x|y|z|yaw|pitch|" + (firing ? "1" : "0"), floats "0.00".

public static (int id, Vector3 pos, float yaw, float pitch, bool firing)? DecodeInput(string payload):
  Return null unless the line starts with "I|" and has the right field count.
  Parse and return the tuple. Return null on any parse failure.
"""

MATCHSTATE = """\
Write MatchState.cs.

public enum MatchPhase { Warmup, Live, Ended }

public sealed class MatchState owns the roster, the clock and the score.

Public fields: MatchPhase Phase = MatchPhase.Warmup; float TimeRemaining = 180f;
int ScoreLimit = 25; int[] TeamScore = new int[2];
List<Combatant> Players = new List<Combatant>(); int NextId = 1;
public Combatant LocalPlayer;
public List<(Vector3 a, Vector3 b, float life)> Tracers = new();

- public void RespawnAt(Combatant c, GameMap map):
  pick a spawn from map.SpawnPoints using a private Random field, set
  c.Position = spawn with Y = 2.0f, call c.ApplyKit(), and set c.RespawnTimer = 0f.
- public Combatant AddLocalPlayer(string name, int team, ClassId kit, GameMap map):
  create new Combatant(NextId++, name, team, kit), RespawnAt it, add to Players,
  assign to LocalPlayer, return it.
- public Combatant AddBot(GameMap map):
  choose the team with fewer players (count Players by Team; tie goes to team 1),
  pick a random ClassId from ClassKit.All, create
  new Combatant(NextId++, "BOT-" + NextId, team, kit) with IsBot = true,
  RespawnAt it, add to Players, return it.
- public void RegisterKill(Combatant killer, Combatant victim):
  victim.Deaths++; victim.RespawnTimer = 3f;
  if killer != null && killer != victim { killer.Kills++; TeamScore[killer.Team]++; }
  if either TeamScore reaches ScoreLimit, set Phase = MatchPhase.Ended.
- public void Update(float dt, GameMap map):
  If Phase != MatchPhase.Live return.
  TimeRemaining -= dt; if it drops to 0 or below, clamp to 0 and set Phase = Ended.
  For each player: call Tick(dt). If not Alive, decrease RespawnTimer by dt and
  when it reaches 0 or below call RespawnAt(player, map).
  Age the Tracers list: subtract dt * 5f from each life and remove any at or below 0.
- public void FireHitscan(Combatant shooter, GameMap map):
  Vector3 origin = shooter.Position; Vector3 dir = Vector3.Normalize(shooter.Forward);
  float wallDist = map.RayWallDistance(origin, dir, 90f);
  Walk every OTHER living player on a different team; treat each as a sphere of
  radius 0.7f at their Position and compute the ray/sphere distance:
  m = origin - target.Position, b = Vector3.Dot(m, dir),
  c = Vector3.Dot(m, m) - 0.49f; skip if (c > 0f && b > 0f);
  disc = b*b - c; skip if disc < 0; t = -b - MathF.Sqrt(disc); if t < 0 set t = 0.
  Keep the nearest t that is strictly less than wallDist.
  Add (origin, origin + dir * (nearest hit distance or wallDist), 1f) to Tracers.
  If a target was hit, call target.TakeDamage(ClassKit.Get(shooter.Kit).Damage)
  and if the target is no longer Alive call RegisterKill(shooter, target).
"""

LOBBYNET = """\
Write LobbyNet.cs.

public sealed class LobbyNet is a minimal UDP host/join layer. It must never
throw into the game loop: every socket operation is wrapped in try/catch and
failures are recorded in a public string field instead.

Public fields: public bool IsHost; public bool Connected; public string Status = "offline";
private UdpClient _socket; private IPEndPoint _remote; private readonly List<IPEndPoint> _clients = new();

- public void StartHost(): create new UdpClient(NetProtocol.Port), set
  _socket.Client.ReceiveTimeout to 1, IsHost = true, Connected = true,
  Status = "hosting on port " + NetProtocol.Port. On exception set
  Status = "host failed: " + ex.Message and Connected = false.
- public void Join(string address): create new UdpClient(), resolve
  _remote = new IPEndPoint(IPAddress.Parse(address), NetProtocol.Port),
  set ReceiveTimeout to 1, IsHost = false, Connected = true,
  Status = "joined " + address. On exception set Status = "join failed: " + ex.Message
  and Connected = false.
- public void Send(string payload): if !Connected return. Convert with
  Encoding.UTF8.GetBytes. If IsHost, send to every endpoint in _clients;
  otherwise send to _remote. Swallow exceptions.
- public List<string> Poll(): return every datagram available right now, as UTF-8
  strings. Loop while _socket.Available > 0, call _socket.Receive(ref endpoint),
  and when IsHost is true and that endpoint is not already in _clients, add it.
  Catch SocketException and break out of the loop. Return the list (never null).
- public void Shutdown(): close the socket inside try/catch, set Connected = false,
  Status = "offline".
"""

SCREENS = """\
Write Screens.cs.

public enum Screen { MainMenu, ClassSelect, Lobby, Match, Scoreboard }

public static class Ui draws every 2D screen. Every method takes the values it
needs as parameters; none of them hold state.

- public static bool Button(Rectangle r, string label, int fontSize):
  Draw a filled rectangle in new Color(40,44,58,255), an outline in
  new Color(120,130,160,255), and the label centred using Raylib.MeasureText.
  Let m = Raylib.GetMousePosition(); if m is inside r, redraw the fill in
  new Color(70,80,110,255). Return true when the mouse is inside r AND
  Raylib.IsMouseButtonPressed(MouseButton.Left).
  Use Raylib_cs.Rectangle with fields X, Y, Width, Height (floats).

- public static void Title(string text, int screenW, int y, int size, Color c):
  centre `text` horizontally using MeasureText and draw it.

- public static void MainMenu(int w, int h, out bool host, out bool join, out bool solo, out bool quit):
  Set all four out params false first. Draw the background as a full-screen
  rectangle in new Color(14,16,22,255). Title "ARENA SHOOTER" at y=90, size 64,
  Color.White. Then four buttons, each 320 wide and 54 tall, centred horizontally
  at (w/2 - 160), starting at y=240 with a 70px gap:
  "HOST MATCH" -> host, "JOIN MATCH" -> join, "SOLO vs BOTS" -> solo, "QUIT" -> quit.

- public static void ClassSelect(int w, int h, ClassId current, out ClassId chosen, out bool confirm):
  chosen = current; confirm = false. Background as above. Title "SELECT CLASS" at
  y=80, size 48. For each ClassId in ClassKit.All draw a button 420x70 at
  (w/2 - 210, 190 + index * 90) labelled ClassKit.Describe(id); clicking it sets
  chosen = id. Draw a highlight rectangle outline in ClassKit.Get(current).Tint
  around the currently selected one. A final button 320x54 at (w/2-160, 520)
  labelled "READY" sets confirm = true.

- public static void Lobby(int w, int h, List<Combatant> players, string status, bool isHost, out bool start, out bool back):
  start = false; back = false. Background as above. Title "LOBBY" at y=70, size 48.
  Draw `status` centred at y=130, size 20, Color.Gray. List each player as
  "<name>  TEAM <team>  <class name>" at x=w/2-200, y=180+i*28, size 20, coloured
  by ClassKit.Get(p.Kit).Tint. A button "START MATCH" 320x54 at (w/2-160, h-160)
  sets start = true, but only draw it when isHost is true. A button "BACK"
  320x54 at (w/2-160, h-90) sets back = true.

- public static void Hud(int w, int h, Combatant me, MatchState match):
  Crosshair: four 10px lines with a 6px gap around (w/2, h/2) in Color.White.
  Bottom-left "HP <health>" size 26 coloured green above 50, yellow above 25,
  else red. Bottom-right "AMMO <ammo>" size 26. Top-centre the score as
  "<TeamScore[0]>  -  <TeamScore[1]>" size 32. Top-right the remaining time as
  match.TimeRemaining formatted "0" followed by "s". If me is not Alive, draw
  "RESPAWNING" centred, size 40, Color.Red.

- public static void Scoreboard(int w, int h, MatchState match, out bool back):
  back = false. Full-screen rectangle new Color(0,0,0,200). Title "SCOREBOARD"
  at y=70, size 48. For each player sorted by Kills descending draw
  "<name>  T<team>  K <kills>  D <deaths>" at x=w/2-220, y=160+i*30, size 22.
  A button "BACK TO MENU" 320x54 at (w/2-160, h-110) sets back = true.
"""

PROGRAM = """\
Write Program.cs. This file owns Main and the screen state machine.

public static class Program.

Private static fields: GameMap _map; MatchState _match; LobbyNet _net;
Screen _screen = Screen.MainMenu; ClassId _pick = ClassId.Assault;
string _joinAddress = "127.0.0.1"; bool _typingAddress; float _netTimer;
const int W = 1280; const int H = 720;

public static int Main(string[] args):
- Raylib.InitWindow(W, H, "Arena Shooter"); Raylib.SetTargetFPS(60);
- _map = new GameMap(); _match = new MatchState(); _net = new LobbyNet();
- Loop while !Raylib.WindowShouldClose():
    float dt = MathF.Min(Raylib.GetFrameTime(), 0.05f);
    Raylib.BeginDrawing(); Raylib.ClearBackground(new Color(14,16,22,255));
    switch on _screen and call DoMainMenu(dt), DoClassSelect(dt), DoLobby(dt),
    DoMatch(dt) or DoScoreboard(dt);
    Raylib.EndDrawing();
- _net.Shutdown(); Raylib.CloseWindow(); return 0;

private static void StartMatch(bool withBots):
- _match = new MatchState();
- _match.AddLocalPlayer("YOU", 0, _pick, _map);
- if (withBots) add 5 bots by calling _match.AddBot(_map) five times.
- _match.Phase = MatchPhase.Live;
- _screen = Screen.Match; Raylib.DisableCursor();

private static void DoMainMenu(float dt):
- Raylib.EnableCursor();
- Call Ui.MainMenu(W, H, out bool host, out bool join, out bool solo, out bool quit).
- host -> _net.StartHost(), _screen = Screen.ClassSelect.
- join -> _net.Join(_joinAddress), _screen = Screen.ClassSelect.
- solo -> _screen = Screen.ClassSelect.
- quit -> Raylib.CloseWindow().
- Also draw "Join address: " + _joinAddress at (40, H-40), size 20, Color.Gray.

private static void DoClassSelect(float dt):
- Ui.ClassSelect(W, H, _pick, out ClassId chosen, out bool confirm);
- _pick = chosen;
- if (confirm) { if (_net.Connected) _screen = Screen.Lobby; else StartMatch(true); }

private static void DoLobby(float dt):
- If the roster is empty, add the local player once:
  if (_match.LocalPlayer == null) _match.AddLocalPlayer("YOU", _net.IsHost ? 0 : 1, _pick, _map);
- Ui.Lobby(W, H, _match.Players, _net.Status, _net.IsHost, out bool start, out bool back);
- start -> add 3 bots then StartMatch(false) but keep the existing roster: instead
  of calling StartMatch, set _match.Phase = MatchPhase.Live, _screen = Screen.Match,
  and call Raylib.DisableCursor().
- back -> _net.Shutdown(); _screen = Screen.MainMenu.

private static void DoMatch(float dt):
- if (Raylib.IsKeyPressed(KeyboardKey.Escape)) { Raylib.EnableCursor(); _screen = Screen.Scoreboard; return; }
- Combatant me = _match.LocalPlayer; if (me == null) { _screen = Screen.MainMenu; return; }
- If me.Alive: read the mouse with Raylib.GetMouseDelta(), subtract delta.X * 0.0032f
  from me.Yaw and delta.Y * 0.0032f from me.Pitch, clamp Pitch to [-1.45f, 1.45f].
  Build a wish vector from W/S/A/D using me.FlatForward and me.FlatRight; if its
  LengthSquared() > 0.0001f normalize it and set
  me.Position = _map.MoveWithSlide(me.Position, wish * ClassKit.Get(me.Kit).MoveSpeed * dt, 0.55f).
  If Raylib.IsMouseButtonDown(MouseButton.Left) && me.CanFire { me.RegisterShot(); _match.FireHitscan(me, _map); }
- Drive the bots: foreach player where IsBot is true call
  BotBrain.Update(bot, _match.Players, _map, dt, (shooter, target) => _match.FireHitscan(shooter, _map));
- _match.Update(dt, _map);
- Networking: _netTimer -= dt; if (_netTimer <= 0f && _net.Connected) {
    _netTimer = 0.05f;
    if (_net.IsHost) _net.Send(NetProtocol.EncodeState(_match.Players, _match.TimeRemaining));
    else _net.Send(NetProtocol.EncodeInput(me.Id, me.Position, me.Yaw, me.Pitch, false));
    foreach (string msg in _net.Poll()) if (!_net.IsHost) NetProtocol.ApplyState(msg, _match.Players);
  }
- Render 3D: build Camera3D cam = new Camera3D(me.Position, me.Position + me.Forward, Vector3.UnitY, 72f, CameraProjection.Perspective);
  Raylib.BeginMode3D(cam);
  Draw the floor with Raylib.DrawPlane at the arena centre sized
  (Width*CellSize, Depth*CellSize) in new Color(55,58,68,255).
  foreach (var (x, z) in _map.WallCells()) draw a cube at GameMap.CellCenter(x, z)
  with Y = GameMap.WallHeight/2f, size (CellSize, WallHeight, CellSize),
  colour new Color(105,95,85,255).
  foreach other living player draw a cube at their Position sized (1.4f, 2.4f, 1.4f)
  tinted ClassKit.Get(p.Kit).Tint, or white when p.HitFlash > 0f.
  foreach tracer in _match.Tracers draw Raylib.DrawLine3D(a, b, Color.Yellow).
  Raylib.EndMode3D();
- Ui.Hud(W, H, me, _match);
- if (_match.Phase == MatchPhase.Ended) { Raylib.EnableCursor(); _screen = Screen.Scoreboard; }

private static void DoScoreboard(float dt):
- Raylib.EnableCursor();
- Ui.Scoreboard(W, H, _match, out bool back);
- if (back) { _net.Shutdown(); _screen = Screen.MainMenu; }
"""


# Which already-generated files each file must be told the ACTUAL API of.
# Ordered so a dependency is always regenerated before its dependents, which is
# the whole point: the first run failed because files disagreed about each
# other, and a hand-written contract could not prevent that.
DEPS = {
    "ClassKit.cs": [],
    "GameMap.cs": [],
    "Combatant.cs": ["ClassKit.cs", "GameMap.cs"],
    "NetProtocol.cs": ["ClassKit.cs", "Combatant.cs"],
    "MatchState.cs": ["ClassKit.cs", "GameMap.cs", "Combatant.cs"],
    "LobbyNet.cs": ["NetProtocol.cs"],
    "Screens.cs": ["ClassKit.cs", "Combatant.cs", "MatchState.cs"],
    "Program.cs": ["ClassKit.cs", "GameMap.cs", "Combatant.cs", "MatchState.cs",
                   "LobbyNet.cs", "NetProtocol.cs", "Screens.cs"],
}
