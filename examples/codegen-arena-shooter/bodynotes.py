"""Per-body algorithm notes, keyed "File.cs:BodyName".

These REPLACE the v1 per-file contracts in specs.py rather than supplementing
them. Mixing the two was measured breaking the first run of the new harness:
specs.py describes a `MapData` string-array map that skeleton.py replaced with
a `bool[,] _walls`, so the prompt carried two contradictory contracts and the
model -- reasonably -- followed the other one. Asked for a one-line bounds
check it returned `MapData[z][x] == '#'`, which cannot compile against the
skeleton. Two sources of truth for one fact is the defect, not the model's
choice between them.

Anything a body cannot derive from its own signature and the declarations
around it belongs here, because that is by definition recall -- and recall is
where this model class was measured wrong 3 times out of 3. The class stat
table below is the clearest case: it looks mechanical and is pure recall.
"""

NOTES = {
    "ClassKit.cs:Get": """\
Return a new ClassKit with exactly these values (transcribe them, do not invent):
  Assault: Name "Assault", MaxHealth 100, MoveSpeed 6f,   Damage 18, FireDelay 0.12f, MaxAmmo 30, Tint Color.Yellow
  Scout:   Name "Scout",   MaxHealth 70,  MoveSpeed 8.5f, Damage 14, FireDelay 0.09f, MaxAmmo 24, Tint Color.Green
  Heavy:   Name "Heavy",   MaxHealth 150, MoveSpeed 4.5f, Damage 26, FireDelay 0.22f, MaxAmmo 40, Tint Color.Red
Set Id = id on each. Use a switch on id; default to the Assault values.""",

    "GameMap.cs:IsWallCell": """\
Bounds-check x against Width and z against Depth, then read the private field
_walls. Anything out of range counts as a wall.""",

    "GameMap.cs:IsWallAt": """\
Convert world coordinates to cell indices by dividing by CellSize and flooring
-- (int)MathF.Floor(p.X / CellSize) and the same for p.Z -- then return
IsWallCell of those.""",

    "GameMap.cs:CircleHitsWall": """\
Return true when IsWallAt holds at any of the four points p offset by plus or
minus radius along X and along Z. Test the four separately.""",

    "GameMap.cs:MoveWithSlide": """\
Axis-at-a-time resolution, so a blocked axis still lets the other move. Start
from pos. Try pos plus (delta.X, 0, 0) and keep it only when
!CircleHitsWall(candidate, radius). From that result try plus (0, 0, delta.Z)
under the same test. Return the final position. Never move on Y.""",

    "GameMap.cs:RayWallDistance": """\
March the ray in fixed steps of 0.25f from 0 up to maxDist, testing
IsWallAt(origin + dir * t) at each step. Return t at the first hit, or maxDist
if there is none.""",

    "GameMap.cs:WallCells": """\
Iterate x over 0..Width-1 and z over 0..Depth-1 and `yield return (x, z);` for
every cell where IsWallCell(x, z) is true.""",

    "Combatant.cs:Forward": """\
Build the look direction from Yaw and Pitch, both in radians:
  X = MathF.Cos(Pitch) * MathF.Sin(Yaw)
  Y = MathF.Sin(Pitch)
  Z = MathF.Cos(Pitch) * MathF.Cos(Yaw)
Return Vector3.Normalize(new Vector3(X, Y, Z)).""",

    "Combatant.cs:ApplyKit": """\
Look up ClassKit.Get(Kit) and copy from it: Health = the kit's MaxHealth,
Ammo = the kit's MaxAmmo. Also reset FireCooldown, RespawnTimer and HitFlash to
0f.""",

    "Combatant.cs:TakeDamage": """\
Subtract amount from Health, clamped at 0. Set HitFlash = 0.25f. Do not touch
Deaths here -- MatchState.RegisterKill owns scoring.""",

    "NetProtocol.cs:EncodeState": """\
Build a payload whose first line is "S" then a bar then matchTime, followed by
one line per player. Separate fields with the bar character and lines with a
newline. Per player, in this exact order: Id, Name, Team, (int)Kit, Position.X,
Position.Y, Position.Z, Yaw, Health, Kills, Deaths. Format every float with
CultureInfo.InvariantCulture. Use a StringBuilder.""",

    "NetProtocol.cs:ApplyState": """\
The exact reverse of EncodeState. Split the payload into lines, skip the
leading "S" line, and for each remaining line split on the bar character, parse
the Id, find the Combatant in players with that Id, and if found copy
Position, Yaw, Health, Kills and Deaths onto it. Ignore any malformed line
instead of throwing. Parse with CultureInfo.InvariantCulture.""",

    "NetProtocol.cs:EncodeInput": """\
Return "I", id, pos.X, pos.Y, pos.Z, yaw, pitch and firing joined by the bar
character, floats in CultureInfo.InvariantCulture and firing as "1" or "0".""",

    "NetProtocol.cs:DecodeInput": """\
Parse what EncodeInput produced. Return null when the payload is null or empty,
does not start with "I", has the wrong field count, or any field fails to
parse. Never throw.""",

    "MatchState.cs:Update": """\
Return immediately unless Phase is MatchPhase.Live. Subtract dt from
TimeRemaining. For every player tick FireCooldown and HitFlash down toward 0 by
dt; for a dead one (!Alive) count RespawnTimer down by dt and call
RespawnAt(player, map) when it reaches 0. Set Phase = MatchPhase.Ended when
TimeRemaining <= 0 or either TeamScore entry reaches ScoreLimit.""",

    "MatchState.cs:RegisterKill": """\
Guard a null killer and a killer on the victim's own team -- both count as a
suicide, so only victim.Deaths++. Otherwise killer.Kills++, victim.Deaths++ and
TeamScore[killer.Team]++. Then set victim.Health = 0 and
victim.RespawnTimer = 3f.""",

    "MatchState.cs:AddBot": """\
Create a Combatant with Id = _nextId++, Name = "Bot " followed by that Id, Team
= whichever team currently has fewer players (0 on a tie), and Kit picked from
ClassKit.All using _rng. Set IsBot = true, call RespawnAt(bot, map), add it to
Players and return it.""",

    "MatchState.cs:AddLocalPlayer": """\
The same shape as AddBot but using the supplied name, team and kit, with
IsBot = false and Id = _nextId++. Call RespawnAt, add to Players, return it.""",

    "MatchState.cs:RespawnAt": """\
Pick a spawn from map.SpawnPoints using _rng and assign it to c.Position, call
c.ApplyKit() to restore health and ammo, and reset c.RespawnTimer to 0f. If
SpawnPoints is empty leave Position unchanged.""",

    "LobbyNet.cs:StartHost": """\
Inside try/catch: _socket = new UdpClient(NetProtocol.Port); IsHost = true;
Connected = true; Status describes the port. On exception set Connected = false
and put the message in Status. The exception must never escape -- callers read
Status instead.""",

    "LobbyNet.cs:Join": """\
Inside try/catch: _socket = new UdpClient(); _remote = new IPEndPoint(
IPAddress.Parse(address), NetProtocol.Port); IsHost = false; Connected = true;
Status names the address; then Send("hello") so the host learns this endpoint.
On exception set Connected = false and put the message in Status.""",

    "LobbyNet.cs:Send": """\
Return immediately if !Connected or _socket is null. Encode with
Encoding.UTF8.GetBytes. When IsHost send to every endpoint in _clients,
otherwise send to _remote. Wrap the sends in try/catch and record a failure in
Status rather than throwing.""",

    "LobbyNet.cs:Poll": """\
Collect every datagram available right now into a new List<string>. Loop while
_socket is not null and _socket.Available > 0, receiving with a ref IPEndPoint
and decoding UTF-8. When IsHost and that endpoint is not already in _clients,
add it. Wrap in try/catch and return whatever was collected.""",

    "LobbyNet.cs:Shutdown": """\
Close the socket inside try/catch, then set Connected = false, IsHost = false,
_socket = null, clear _clients and set Status to "offline".""",

    "Screens.cs:Button": """\
Draw the rectangle with Raylib.DrawRectangle and DrawRectangleLines, centre the
label using Raylib.MeasureText, and return true only when the mouse position is
inside r AND Raylib.IsMouseButtonPressed(MouseButton.Left). Use a lighter fill
while hovered. r is a Raylib_cs.Rectangle with float X, Y, Width and Height.""",

    "Screens.cs:Title": """\
Draw the text horizontally centred: x = screenW / 2 - Raylib.MeasureText(text,
size) / 2, at the given y, size and colour.""",

    "Screens.cs:MainMenu": """\
Assign all four out parameters to false FIRST. Then Title("ARENA SHOOTER", w,
80, 60, Color.White) and four stacked buttons starting at y = 220, each 320
wide and 56 tall with 24 between them, centred on w / 2: "HOST GAME" sets host,
"JOIN GAME" sets join, "SOLO VS BOTS" sets solo, "QUIT" sets quit.""",

    "Screens.cs:ClassSelect": """\
Assign chosen = current and confirm = false first. Title("CHOOSE YOUR CLASS",
w, 80, 44, Color.White). Lay out one button per ClassKit.All entry
horizontally, each showing that kit's Name with its MaxHealth and Damage;
clicking one assigns chosen. A "CONFIRM" button at the bottom sets confirm.""",

    "Screens.cs:Lobby": """\
Assign start and back to false first. Title("LOBBY", w, 60, 44, Color.White).
Draw status at x = 40, y = 120. List each player as their Name, then TEAM and
their Team, then ClassKit.Get(p.Kit).Name -- at x = w / 2 - 200, y = 180 + i *
28, size 20, coloured by team. Show a "START MATCH" button only when isHost;
always show "BACK".""",

    "Screens.cs:Hud": """\
Return if me is null. Draw a health bar bottom-left with the numbers for
Health and Ammo beside it. Draw match.TeamScore top-centre and
match.TimeRemaining as minutes and seconds. Draw a small crosshair at the
screen centre with two DrawLine calls.""",

    "Screens.cs:Scoreboard": """\
Assign back = false first. Title("SCOREBOARD", w, 60, 44, Color.White). Order
match.Players by Kills descending and draw a row per player with Name, Kills
and Deaths from y = 160, size 22, team-coloured. A "BACK TO MENU" button sets
back.""",

    "Program.cs:Main": """\
Raylib.InitWindow(W, H, "Arena Shooter") then Raylib.SetTargetFPS(60).
Initialise _camera = new Camera3D(new Vector3(0, 2, 0), new Vector3(0, 2, 1),
Vector3.UnitY, 70f, CameraProjection.Perspective). Loop while
!Raylib.WindowShouldClose(): take float dt = MathF.Min(Raylib.GetFrameTime(),
0.05f), Raylib.BeginDrawing(), Raylib.ClearBackground(Color.Black), switch on
_screen to call DoMainMenu, DoClassSelect, DoLobby, DoMatch or DoScoreboard
with dt, then Raylib.EndDrawing(). After the loop call _net.Shutdown() and
Raylib.CloseWindow(), and return 0.""",

    "Program.cs:StartMatch": """\
_map = new GameMap(); _match = new MatchState(); _me =
_match.AddLocalPlayer("You", 0, _pick, _map); when withBots, add five bots with
_match.AddBot(_map). Then _match.Phase = MatchPhase.Live, _screen =
Screen.Match, and Raylib.DisableCursor().""",

    "Program.cs:DoMainMenu": """\
Call Raylib.EnableCursor(), then Ui.MainMenu(W, H, out bool host, out bool
join, out bool solo, out bool quit). solo sets _screen = Screen.ClassSelect.
host calls _net.StartHost() then goes to Screen.ClassSelect. join calls
_net.Join(_joinAddress) then goes to Screen.ClassSelect. quit calls
Raylib.CloseWindow(). Also draw _joinAddress so it is visible.""",

    "Program.cs:DoClassSelect": """\
Ui.ClassSelect(W, H, _pick, out ClassId chosen, out bool confirm); assign
_pick = chosen; on confirm go to Screen.Lobby when _net.Connected, otherwise
call StartMatch(true).""",

    "Program.cs:DoLobby": """\
Ui.Lobby(W, H, _match.Players, _net.Status, _net.IsHost, out bool start, out
bool back); on start call StartMatch(true); on back set _screen =
Screen.MainMenu.""",

    "Program.cs:DoMatch": """\
Apply Raylib.GetMouseDelta() to _me.Yaw and _me.Pitch, clamping Pitch to plus
or minus 1.5f. Build a move vector from W/A/S/D relative to _me.Forward, scale
it by ClassKit.Get(_me.Kit).MoveSpeed * dt, and assign _me.Position =
_map.MoveWithSlide(_me.Position, move, 0.4f). Call _match.Update(dt, _map).
Accumulate dt into _netTimer and every 0.05s, when _net.Connected, send
NetProtocol.EncodeState if IsHost else EncodeInput. Drain foreach (string msg
in _net.Poll()) and when !_net.IsHost call NetProtocol.ApplyState(msg,
_match.Players). Render inside Raylib.BeginMode3D(_camera) with the camera at
_me.Position plus (0, 1.6f, 0) looking along _me.Forward: DrawPlane for the
floor, a cube at GameMap.CellCenter sized CellSize and WallHeight for every
_map.WallCells() entry, and a cube tinted by their kit for each other living
player; then Raylib.EndMode3D() and Ui.Hud(W, H, _me, _match). Escape being
pressed, or _match.Phase == MatchPhase.Ended, sets _screen = Screen.Scoreboard
and calls Raylib.EnableCursor().""",

    "Program.cs:DoScoreboard": """\
Raylib.EnableCursor(); Ui.Scoreboard(W, H, _match, out bool back); on back set
_screen = Screen.MainMenu.""",
}


def note(file_name: str, body_name: str) -> str:
    return NOTES.get("%s:%s" % (file_name, body_name), "")
