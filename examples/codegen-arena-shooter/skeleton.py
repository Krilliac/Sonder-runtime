"""Harness-owned C# skeletons: declarations the model is never asked to write.

Why this exists, measured rather than assumed. The previous design put the
project's public surface in the PROMPT and asked the model to reproduce it in
every file. It cannot. The final error mix of that run was, in order:

    CS1061 member-not-found      28
    CS0103 name-not-found        21
    CS0272 assign-to-readonly    16
    CS0117 no-such-definition    15
    CS1503 argument-mismatch     13

Every one of those is the same failure: two files disagreeing about an API.
Feeding each file the REAL extracted signatures of its dependencies did not fix
it -- Combatant.cs was handed GameMap's actual surface and still called four
members that exist in neither the contract nor the extraction. A 7B-class model
writes correct functions and cannot hold a system; that is a capability limit,
not a prompting one, so the harness stops asking.

Here the declarations come from ONE source (this file), are emitted verbatim,
and are identical in the code and in the prompt. Two consequences:

  * Those five error classes become structurally impossible for cross-file
    references. The members exist, with the declared accessibility, by
    construction.
  * The skeleton compiles to ZERO errors before any model output exists, so
    every later error is attributable to exactly one body, and a body that
    fails can be reverted on its own instead of poisoning the file.

The model's job shrinks to "write this one method body, given every fact it
needs" -- which is the transformation shape it was measured to be good at
(4/4 usable), rather than the recall/architecture shape it was measured to
fail (3/3 wrong, never a compiling build across 8 files).

No namespace declaration anywhere: the project convention is the global
namespace, and a single file wrapping itself in `namespace ArenaShooter` was
what hid its types from the other seven in the previous run.
"""

import os
import sys

# A method body the model has not filled yet. Chosen because it satisfies C#
# definite-assignment and all return paths -- including `out` parameters -- so
# the untouched skeleton is a clean compile rather than a pile of CS0161/CS0177.
PLACEHOLDER = "throw new NotImplementedException();"

sys.path.insert(0, os.environ.get(
    "SONDER_RUNTIME",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),
))

import codegen_loop  # noqa: E402

# The slot machinery now lives in Sonder's codegen_loop, which is where the
# reusable half of this experiment belongs: it is language-agnostic there and
# carries its own tests. These stay as thin aliases so the skeletons and the
# driver keep reading the way the C# they describe reads.
#
# Keeping a private copy here is how a real bug survived a commit. BOTH had a
# splice that added the slot's indent to every line of a ragged generated body,
# so a three-line body came back as a staircase; fixing one would have left the
# other. One source of truth.
body_names = codegen_loop.body_slots
splice = codegen_loop.splice_body
signature_of = codegen_loop.body_signature


def declarations(skeleton: str) -> str:
    """The skeleton with every body collapsed, as a dependency brief.

    This is what other files are told. It is the same text that compiles, so
    the brief cannot drift from the code the way a prose contract did. The
    `using` lines are dropped because a brief describes a surface, not a
    compilation unit.
    """
    collapsed = codegen_loop.collapse_bodies(skeleton)
    return "\n".join(
        line for line in collapsed.splitlines()
        if not line.strip().startswith("using ")
    ).strip()


CLASSKIT = """\
using System;
using System.Collections.Generic;
using System.Numerics;
using Raylib_cs;

public enum ClassId { Assault, Scout, Heavy }

public sealed class ClassKit
{
    public ClassId Id;
    public string Name = string.Empty;
    public int MaxHealth;
    public float MoveSpeed;
    public int Damage;
    public float FireDelay;
    public int MaxAmmo;
    public Color Tint;

    public static ClassId[] All = new[] { ClassId.Assault, ClassId.Scout, ClassId.Heavy };

    public static ClassKit Get(ClassId id)
    {
        // BODY:Get
        throw new NotImplementedException();
    }
}
"""


GAMEMAP = """\
using System;
using System.Collections.Generic;
using System.Numerics;

public sealed class GameMap
{
    public const float CellSize = 4f;
    public const float WallHeight = 4f;

    public int Width;
    public int Depth;
    public List<Vector3> SpawnPoints = new List<Vector3>();

    private bool[,] _walls;

    public GameMap()
    {
        Width = 24;
        Depth = 24;
        _walls = new bool[Width, Depth];
        for (int x = 0; x < Width; x++)
        {
            for (int z = 0; z < Depth; z++)
            {
                bool border = x == 0 || z == 0 || x == Width - 1 || z == Depth - 1;
                bool pillar = (x % 6 == 3) && (z % 6 == 3);
                _walls[x, z] = border || pillar;
            }
        }
        SpawnPoints.Add(CellCenter(2, 2));
        SpawnPoints.Add(CellCenter(Width - 3, 2));
        SpawnPoints.Add(CellCenter(2, Depth - 3));
        SpawnPoints.Add(CellCenter(Width - 3, Depth - 3));
        SpawnPoints.Add(CellCenter(Width / 2, 2));
        SpawnPoints.Add(CellCenter(Width / 2, Depth - 3));
    }

    public static Vector3 CellCenter(int x, int z)
    {
        return new Vector3((x + 0.5f) * CellSize, 0f, (z + 0.5f) * CellSize);
    }

    public bool IsWallCell(int x, int z)
    {
        // BODY:IsWallCell
        throw new NotImplementedException();
    }

    public bool IsWallAt(Vector3 p)
    {
        // BODY:IsWallAt
        throw new NotImplementedException();
    }

    public bool CircleHitsWall(Vector3 p, float radius)
    {
        // BODY:CircleHitsWall
        throw new NotImplementedException();
    }

    public Vector3 MoveWithSlide(Vector3 pos, Vector3 delta, float radius)
    {
        // BODY:MoveWithSlide
        throw new NotImplementedException();
    }

    public float RayWallDistance(Vector3 origin, Vector3 dir, float maxDist)
    {
        // BODY:RayWallDistance
        throw new NotImplementedException();
    }

    public IEnumerable<(int x, int z)> WallCells()
    {
        // BODY:WallCells
        throw new NotImplementedException();
    }
}
"""


COMBATANT = """\
using System;
using System.Collections.Generic;
using System.Numerics;

public sealed class Combatant
{
    public int Id;
    public string Name;
    public int Team;
    public ClassId Kit;
    public Vector3 Position;
    public float Yaw;
    public float Pitch;
    public int Health;
    public int Ammo;
    public int Kills;
    public int Deaths;
    public bool IsBot;
    public float RespawnTimer;
    public float FireCooldown;
    public float HitFlash;

    public bool Alive => Health > 0;

    public Combatant(int id, string name, int team, ClassId kit)
    {
        Id = id;
        Name = name;
        Team = team;
        Kit = kit;
        Position = Vector3.Zero;
        ApplyKit();
    }

    public Vector3 Forward
    {
        get
        {
            // BODY:Forward
            throw new NotImplementedException();
        }
    }

    public void ApplyKit()
    {
        // BODY:ApplyKit
        throw new NotImplementedException();
    }

    public void TakeDamage(int amount)
    {
        // BODY:TakeDamage
        throw new NotImplementedException();
    }

    // Weapon state is host-owned rather than generated: ammo, fire cadence,
    // and reload eligibility are gameplay invariants shared by every input
    // path. A generated screen loop therefore cannot spend ammo while
    // bypassing the cooldown, or reload a dead player.
    public bool TryFire()
    {
        if (!Alive || RespawnTimer > 0f || FireCooldown > 0f || Ammo <= 0)
            return false;

        Ammo--;
        FireCooldown = MathF.Max(0f, ClassKit.Get(Kit).FireDelay);
        return true;
    }

    public bool TryReload()
    {
        if (!Alive || RespawnTimer > 0f)
            return false;

        int capacity = ClassKit.Get(Kit).MaxAmmo;
        if (Ammo >= capacity)
            return false;

        Ammo = capacity;
        return true;
    }
}
"""


NETPROTOCOL = """\
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Numerics;
using System.Text;

public static class NetProtocol
{
    public const int Port = 45123;

    public static string EncodeState(List<Combatant> players, float matchTime)
    {
        // BODY:EncodeState
        throw new NotImplementedException();
    }

    public static void ApplyState(string payload, List<Combatant> players)
    {
        // BODY:ApplyState
        throw new NotImplementedException();
    }

    public static string EncodeInput(int id, Vector3 pos, float yaw, float pitch, bool firing)
    {
        // BODY:EncodeInput
        throw new NotImplementedException();
    }

    public static (int id, Vector3 pos, float yaw, float pitch, bool firing)? DecodeInput(string payload)
    {
        // BODY:DecodeInput
        throw new NotImplementedException();
    }
}
"""


MATCHSTATE = """\
using System;
using System.Collections.Generic;
using System.Numerics;

public enum MatchPhase { Warmup, Live, Ended }

public sealed class MatchState
{
    public MatchPhase Phase = MatchPhase.Warmup;
    public float TimeRemaining = 300f;
    public int ScoreLimit = 25;
    public int[] TeamScore = new int[2];
    public List<Combatant> Players = new List<Combatant>();

    private int _nextId = 1;
    private readonly Random _rng = new Random(20260807);

    public void Update(float dt, GameMap map)
    {
        // BODY:Update
        throw new NotImplementedException();
    }

    public void RegisterKill(Combatant killer, Combatant victim)
    {
        // BODY:RegisterKill
        throw new NotImplementedException();
    }

    public Combatant AddBot(GameMap map)
    {
        // BODY:AddBot
        throw new NotImplementedException();
    }

    public Combatant AddLocalPlayer(string name, int team, ClassId kit, GameMap map)
    {
        // BODY:AddLocalPlayer
        throw new NotImplementedException();
    }

    public void RespawnAt(Combatant c, GameMap map)
    {
        // BODY:RespawnAt
        throw new NotImplementedException();
    }
}
"""


LOBBYNET = """\
using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Text;

public sealed class LobbyNet
{
    public bool IsHost;
    public bool Connected;
    public string Status = "offline";

    // These are populated by the generated lifecycle bodies.  The null-forgiving
    // initializers preserve that runtime contract while keeping the deterministic
    // baseline warning-free before any model body is installed.
    private UdpClient _socket = null!;
    private IPEndPoint _remote = null!;
    private readonly List<IPEndPoint> _clients = new List<IPEndPoint>();

    public void StartHost()
    {
        // BODY:StartHost
        throw new NotImplementedException();
    }

    public void Join(string address)
    {
        // BODY:Join
        throw new NotImplementedException();
    }

    public void Send(string payload)
    {
        // BODY:Send
        throw new NotImplementedException();
    }

    public List<string> Poll()
    {
        // BODY:Poll
        throw new NotImplementedException();
    }

    public void Shutdown()
    {
        // BODY:Shutdown
        throw new NotImplementedException();
    }
}
"""


SCREENS = """\
using System;
using System.Collections.Generic;
using System.Numerics;
using Raylib_cs;

public enum Screen { MainMenu, ClassSelect, Lobby, Match, Scoreboard }

public static class Ui
{
    public static bool Button(Rectangle r, string label, int fontSize)
    {
        // BODY:Button
        throw new NotImplementedException();
    }

    public static void Title(string text, int screenW, int y, int size, Color c)
    {
        // BODY:Title
        throw new NotImplementedException();
    }

    public static void MainMenu(int w, int h, out bool host, out bool join, out bool solo, out bool quit)
    {
        // BODY:MainMenu
        throw new NotImplementedException();
    }

    public static void ClassSelect(int w, int h, ClassId current, out ClassId chosen, out bool confirm)
    {
        // BODY:ClassSelect
        throw new NotImplementedException();
    }

    public static void Lobby(int w, int h, List<Combatant> players, string status, bool isHost, out bool start, out bool back)
    {
        // BODY:Lobby
        throw new NotImplementedException();
    }

    public static void Hud(int w, int h, Combatant me, MatchState match)
    {
        // BODY:Hud
        throw new NotImplementedException();
    }

    public static void Scoreboard(int w, int h, MatchState match, out bool back)
    {
        // BODY:Scoreboard
        throw new NotImplementedException();
    }
}
"""


PROGRAM = """\
using System;
using System.Collections.Generic;
using System.Numerics;
using Raylib_cs;

public static class Program
{
    private const int W = 1280;
    private const int H = 720;

    private static GameMap _map = new GameMap();
    private static MatchState _match = new MatchState();
    private static LobbyNet _net = new LobbyNet();
    private static Combatant _me = null!;
    private static Screen _screen = Screen.MainMenu;
    private static ClassId _pick = ClassId.Assault;
    private static string _joinAddress = "127.0.0.1";
    private static float _netTimer;
    private static Camera3D _camera;

    public static int Main(string[] args)
    {
        // BODY:Main
        throw new NotImplementedException();
    }

    private static void StartMatch(bool withBots)
    {
        // BODY:StartMatch
        throw new NotImplementedException();
    }

    private static void DoMainMenu(float dt)
    {
        // BODY:DoMainMenu
        throw new NotImplementedException();
    }

    private static void DoClassSelect(float dt)
    {
        // BODY:DoClassSelect
        throw new NotImplementedException();
    }

    private static void DoLobby(float dt)
    {
        // BODY:DoLobby
        throw new NotImplementedException();
    }

    private static void DoMatch(float dt)
    {
        // BODY:DoMatch
        throw new NotImplementedException();
    }

    private static void DoScoreboard(float dt)
    {
        // BODY:DoScoreboard
        throw new NotImplementedException();
    }
}
"""


# Order matters: a file is shown the declarations of everything before it.
FILES = [
    ("ClassKit.cs", CLASSKIT),
    ("GameMap.cs", GAMEMAP),
    ("Combatant.cs", COMBATANT),
    ("NetProtocol.cs", NETPROTOCOL),
    ("MatchState.cs", MATCHSTATE),
    ("LobbyNet.cs", LOBBYNET),
    ("Screens.cs", SCREENS),
    ("Program.cs", PROGRAM),
]


def by_name(name: str) -> str:
    """The ORIGINAL skeleton for a file.

    Signatures and declarations are read from this rather than from the file on
    disk: bodies get spliced in as the run proceeds, and a prompt that quoted
    the partially-filled file would drift from the contract it is supposed to
    be pinning.
    """
    for candidate, text in FILES:
        if candidate == name:
            return text
    raise KeyError(name)
