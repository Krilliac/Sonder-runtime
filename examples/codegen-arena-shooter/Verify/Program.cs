using System.Collections;
using System.Numerics;
using System.Reflection;

namespace Verify;

/// <summary>
/// Runtime verification of the Sonder-authored assembly.
///
/// This exists because a green build is NOT sufficient evidence here. The first
/// generation pass declared `private static readonly string[] MapData;` and
/// never assigned it -- which is not a compile error, so a compile-and-repair
/// loop would have reported success on a game that null-references the instant
/// it loads a map.
///
/// Everything is done by reflection, deliberately: the generated code's shape
/// changes between runs (fields vs properties, constructor arity), and a
/// verifier that only works against one exact shape would be useless the next
/// time. Every probe reports rather than throws.
/// </summary>
public static class Program
{
    private static int _pass, _fail;

    private static void Check(bool ok, string what, string detail = "")
    {
        Console.WriteLine((ok ? "  PASS  " : "  FAIL  ") + what + (detail.Length > 0 ? "  -- " + detail : ""));
        if (ok) _pass++; else _fail++;
    }

    public static int Main()
    {
        Console.WriteLine("=== Sonder-authored assembly: runtime verification ===");

        Assembly asm;
        try
        {
            asm = Assembly.Load("FpsGameSonder");
        }
        catch (Exception e)
        {
            Console.WriteLine("FATAL: could not load the assembly: " + e.Message);
            return 2;
        }

        Type[] types;
        try { types = asm.GetTypes(); }
        catch (ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray()!; }

        Console.WriteLine($"types found: {string.Join(", ", types.Select(t => t.Name).OrderBy(n => n))}\n");

        VerifyClassKit(types);
        VerifyGameMap(types);
        VerifyNetProtocol(types);
        VerifyMatchState(types);
        VerifyCombatant(types);

        Console.WriteLine($"\n=== {_pass} passed, {_fail} failed ===");
        return _fail == 0 ? 0 : 1;
    }

    private static Type? Find(Type[] types, string name) =>
        types.FirstOrDefault(t => t.Name == name);

    /// Read a field or property by name, whichever the generated code used.
    private static object? Member(object? instance, Type type, string name)
    {
        FieldInfo? f = type.GetField(name, BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static);
        if (f != null) return f.GetValue(instance);
        PropertyInfo? p = type.GetProperty(name, BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static);
        return p?.GetValue(instance);
    }

    /// <summary>Set public mutable state whether a future implementation uses
    /// a field or a property.</summary>
    private static bool SetMember(object instance, Type type, string name, object value)
    {
        FieldInfo? f = type.GetField(name, BindingFlags.Public | BindingFlags.Instance);
        if (f != null) { f.SetValue(instance, value); return true; }
        PropertyInfo? p = type.GetProperty(name, BindingFlags.Public | BindingFlags.Instance);
        if (p?.CanWrite == true) { p.SetValue(instance, value); return true; }
        return false;
    }

    private static void VerifyClassKit(Type[] types)
    {
        Console.WriteLine("--- ClassKit ---");
        Type? kit = Find(types, "ClassKit");
        Type? id = Find(types, "ClassId");
        if (kit == null || id == null) { Check(false, "ClassKit and ClassId exist"); return; }
        Check(true, "ClassKit and ClassId exist");

        MethodInfo? get = kit.GetMethod("Get", BindingFlags.Public | BindingFlags.Static);
        if (get == null) { Check(false, "ClassKit.Get(ClassId) exists"); return; }
        Check(true, "ClassKit.Get(ClassId) exists");

        // The three kits must be genuinely distinct, not three copies of the
        // default arm of a switch.
        var seen = new List<(string name, int hp, int dmg)>();
        foreach (object value in Enum.GetValues(id))
        {
            object? k;
            try { k = get.Invoke(null, new[] { value }); }
            catch (Exception e) { Check(false, $"Get({value}) returns a kit", e.InnerException?.Message ?? e.Message); continue; }
            if (k == null) { Check(false, $"Get({value}) returns a kit"); continue; }
            string name = Member(k, kit, "Name")?.ToString() ?? "";
            int hp = Convert.ToInt32(Member(k, kit, "MaxHealth") ?? 0);
            int dmg = Convert.ToInt32(Member(k, kit, "Damage") ?? 0);
            seen.Add((name, hp, dmg));
            Check(hp > 0 && name.Length > 0, $"{value} kit is populated", $"{name} HP {hp} DMG {dmg}");
        }
        Check(seen.Select(s => s.hp).Distinct().Count() == seen.Count,
              "each class has a distinct MaxHealth",
              string.Join(" / ", seen.Select(s => $"{s.name}:{s.hp}")));
    }

    private static void VerifyGameMap(Type[] types)
    {
        Console.WriteLine("\n--- GameMap ---");
        Type? map = Find(types, "GameMap");
        if (map == null) { Check(false, "GameMap exists"); return; }
        Check(true, "GameMap exists");

        // Construct it however it wants to be constructed.
        object? inst = null;
        ConstructorInfo? ctor = map.GetConstructor(Type.EmptyTypes);
        if (ctor != null)
        {
            try { inst = ctor.Invoke(null); }
            catch (Exception e) { Check(false, "parameterless constructor runs", e.InnerException?.Message ?? e.Message); }
        }
        else
        {
            Check(false, "GameMap has a parameterless constructor",
                  "Program.cs calls new GameMap(); generated ctors: " +
                  string.Join(" | ", map.GetConstructors().Select(c =>
                      "(" + string.Join(", ", c.GetParameters().Select(p => p.ParameterType.Name)) + ")")));
        }
        if (inst == null) return;
        Check(true, "parameterless constructor runs");

        // The bug a compiler cannot see: map data declared but never filled.
        MethodInfo? wallCells = map.GetMethod("WallCells");
        if (wallCells == null) { Check(false, "WallCells() exists"); }
        else
        {
            try
            {
                int count = 0;
                if (wallCells.Invoke(inst, null) is IEnumerable cells)
                    foreach (object _ in cells) count++;
                Check(count > 0, "map actually contains wall cells", $"{count} walls");
            }
            catch (Exception e)
            {
                Check(false, "WallCells() runs", e.InnerException?.GetType().Name + ": " + e.InnerException?.Message);
            }
        }

        object? spawns = Member(inst, map, "SpawnPoints");
        int spawnCount = spawns is ICollection c2 ? c2.Count : -1;
        Check(spawnCount > 0, "map exposes spawn points", $"{spawnCount}");

        // Collision must actually reject something, or "no walls" would pass.
        MethodInfo? hits = map.GetMethod("CircleHitsWall");
        MethodInfo? slide = map.GetMethod("MoveWithSlide");
        if (hits != null)
        {
            try
            {
                // Cell (0,0) is '#' in the specced map, so its centre is solid.
                bool solid = (bool)hits.Invoke(inst, new object[] { new Vector3(2f, 0f, 2f), 0.55f })!;
                Check(solid, "the arena border reads as solid");
            }
            catch (Exception e) { Check(false, "CircleHitsWall runs", e.InnerException?.Message ?? e.Message); }
        }
        else Check(false, "CircleHitsWall exists");

        if (slide != null && spawnCount > 0)
        {
            try
            {
                var list = (IList)spawns!;
                var start = (Vector3)list[0]!;
                // Drive hard into a wall; the result must never be inside one.
                Vector3 p = start;
                for (int i = 0; i < 300; i++)
                    p = (Vector3)slide.Invoke(inst, new object[] { p, new Vector3(-0.25f, 0f, 0f), 0.55f })!;
                bool inside = hits != null && (bool)hits.Invoke(inst, new object[] { p, 0.55f })!;
                Check(!inside, "walking into a wall never ends inside it", $"final x={p.X:0.00}");
            }
            catch (Exception e) { Check(false, "MoveWithSlide runs", e.InnerException?.Message ?? e.Message); }
        }
        else Check(slide != null, "MoveWithSlide exists");
    }

    private static void VerifyCombatant(Type[] types)
    {
        Console.WriteLine("\n--- Combatant ---");
        Type? comb = Find(types, "Combatant");
        Type? id = Find(types, "ClassId");
        if (comb == null || id == null) { Check(false, "Combatant exists"); return; }
        Check(true, "Combatant exists");

        object? inst = null;
        foreach (ConstructorInfo ctor in comb.GetConstructors())
        {
            ParameterInfo[] ps = ctor.GetParameters();
            if (ps.Length != 4) continue;
            try
            {
                inst = ctor.Invoke(new object[] { 1, "TEST", 0, Enum.GetValues(id).GetValue(0)! });
            }
            catch (Exception e) { Check(false, "Combatant(int,string,int,ClassId) runs", e.InnerException?.Message ?? e.Message); }
            break;
        }
        if (inst == null) { Check(false, "Combatant(int,string,int,ClassId) exists"); return; }
        Check(true, "Combatant(int,string,int,ClassId) runs");

        int hp = Convert.ToInt32(Member(inst, comb, "Health") ?? 0);
        Check(hp > 0, "constructor applies the kit (Health > 0)", $"HP {hp}");

        MethodInfo? fire = comb.GetMethod("TryFire");
        MethodInfo? reload = comb.GetMethod("TryReload");
        if (fire == null || reload == null)
        {
            Check(false, "TryFire/TryReload weapon controls exist");
        }
        else
        {
            try
            {
                int startingAmmo = Convert.ToInt32(Member(inst, comb, "Ammo") ?? 0);
                object? fired = fire.Invoke(inst, null);
                int afterShot = Convert.ToInt32(Member(inst, comb, "Ammo") ?? -1);
                Check(fired is true && startingAmmo > 0 && afterShot == startingAmmo - 1,
                    "TryFire spends exactly one round", $"{startingAmmo} -> {afterShot}");

                object? blocked = fire.Invoke(inst, null);
                Check(blocked is false && Convert.ToInt32(Member(inst, comb, "Ammo") ?? -1) == afterShot,
                    "TryFire respects the fire cooldown");

                bool emptied = SetMember(inst, comb, "Ammo", 0) && SetMember(inst, comb, "FireCooldown", 0f);
                object? reloaded = emptied ? reload.Invoke(inst, null) : null;
                int afterReload = Convert.ToInt32(Member(inst, comb, "Ammo") ?? -1);
                Check(reloaded is true && afterReload == startingAmmo,
                    "TryReload refills an empty living combatant", $"ammo {afterReload}");
                Check(reload.Invoke(inst, null) is false,
                    "TryReload does not refill an already-full weapon");
            }
            catch (Exception e)
            { Check(false, "weapon controls run", e.InnerException?.Message ?? e.Message); }
        }

        MethodInfo? dmg = comb.GetMethod("TakeDamage");
        if (dmg != null)
        {
            try
            {
                dmg.Invoke(inst, new object[] { 10_000 });
                int after = Convert.ToInt32(Member(inst, comb, "Health") ?? -1);
                Check(after == 0, "lethal damage clamps at zero", $"HP {after}");
                object? alive = Member(inst, comb, "Alive");
                Check(alive is bool b && !b, "a combatant at 0 HP is not Alive");
            }
            catch (Exception e) { Check(false, "TakeDamage runs", e.InnerException?.Message ?? e.Message); }
        }
        else Check(false, "TakeDamage exists");
    }

    /// <summary>
    /// The wire protocol is the sharpest held-out check available: encode and
    /// decode are separate generated bodies, so a round trip proves they agree
    /// with each other rather than merely compiling. A malformed payload is
    /// checked too, because the contract says "never throw" and a validator
    /// that throws on bad input fails open in the caller's try/catch.
    /// </summary>
    private static void VerifyNetProtocol(Type[] types)
    {
        Console.WriteLine("\n--- NetProtocol ---");
        Type? net = types.FirstOrDefault(t => t.Name == "NetProtocol");
        if (net == null) { Check(false, "NetProtocol exists"); return; }
        Check(true, "NetProtocol exists");

        MethodInfo? encIn = net.GetMethod("EncodeInput");
        MethodInfo? decIn = net.GetMethod("DecodeInput");
        if (encIn == null || decIn == null)
        {
            Check(false, "EncodeInput/DecodeInput exist");
        }
        else
        {
            try
            {
                var pos = new Vector3(1.5f, 2.25f, -3.75f);
                object? wire = encIn.Invoke(null, new object[] { 7, pos, 0.5f, -0.25f, true });
                Check(wire is string w && w.Length > 0, "EncodeInput produces a payload",
                      Convert.ToString(wire) ?? "");
                object? back = decIn.Invoke(null, new object?[] { wire });
                Check(back != null, "DecodeInput accepts what EncodeInput produced");
                if (back != null)
                {
                    // Nullable tuple: read the fields off Value by reflection so
                    // this survives the generated shape changing.
                    object? val = back.GetType().GetProperty("Value")?.GetValue(back) ?? back;
                    FieldInfo[] fs = val.GetType().GetFields();
                    string dump = string.Join(", ", fs.Select(f => f.GetValue(val)));
                    bool idOk = fs.Length > 0 && Convert.ToInt32(fs[0].GetValue(val)) == 7;
                    Check(idOk, "the decoded id survives the round trip", dump);
                }
            }
            catch (Exception e)
            { Check(false, "input round trip runs", e.InnerException?.Message ?? e.Message); }

            foreach (string bad in new[] { "", "garbage", "I|1|2" })
            {
                try
                {
                    object? r = decIn.Invoke(null, new object?[] { bad });
                    Check(r == null, $"DecodeInput rejects malformed input rather than throwing ({(bad.Length == 0 ? "empty" : bad)})");
                }
                catch (Exception e)
                { Check(false, $"DecodeInput({bad}) does not throw", e.InnerException?.GetType().Name ?? e.Message); }
            }
        }
    }

    /// <summary>
    /// Scoring is the one place a plausible-looking body silently corrupts a
    /// match: a kill credited to the wrong team, or a suicide that still scores,
    /// compiles perfectly and only shows up as a wrong number on a scoreboard.
    /// </summary>
    private static void VerifyMatchState(Type[] types)
    {
        Console.WriteLine("\n--- MatchState ---");
        Type? ms = types.FirstOrDefault(t => t.Name == "MatchState");
        Type? map = types.FirstOrDefault(t => t.Name == "GameMap");
        Type? cid = types.FirstOrDefault(t => t.Name == "ClassId");
        if (ms == null || map == null || cid == null) { Check(false, "MatchState/GameMap/ClassId exist"); return; }
        Check(true, "MatchState exists");

        object state, world;
        try { state = Activator.CreateInstance(ms)!; world = Activator.CreateInstance(map)!; }
        catch (Exception e) { Check(false, "MatchState constructs", e.InnerException?.Message ?? e.Message); return; }
        Check(true, "MatchState constructs");

        MethodInfo? addLocal = ms.GetMethod("AddLocalPlayer");
        MethodInfo? kill = ms.GetMethod("RegisterKill");
        if (addLocal == null || kill == null) { Check(false, "AddLocalPlayer/RegisterKill exist"); return; }

        object? a, b;
        try
        {
            object first = Enum.GetValues(cid).GetValue(0)!;
            a = addLocal.Invoke(state, new object[] { "A", 0, first, world });
            b = addLocal.Invoke(state, new object[] { "B", 1, first, world });
            Check(a != null && b != null, "two players join the match");
        }
        catch (Exception e) { Check(false, "AddLocalPlayer runs", e.InnerException?.Message ?? e.Message); return; }

        var players = Member(state, ms, "Players") as IEnumerable;
        int count = players?.Cast<object>().Count() ?? 0;
        Check(count == 2, "both players are in Players", $"{count}");

        try
        {
            kill.Invoke(state, new object?[] { a, b });
            var scores = Member(state, ms, "TeamScore") as Array;
            int t0 = scores != null && scores.Length > 0 ? Convert.ToInt32(scores.GetValue(0)) : -1;
            int t1 = scores != null && scores.Length > 1 ? Convert.ToInt32(scores.GetValue(1)) : -1;
            Check(t0 == 1 && t1 == 0, "a kill scores for the killer's team only", $"team0={t0} team1={t1}");
            Check(Convert.ToInt32(Member(a!, a!.GetType(), "Kills") ?? -1) == 1, "the killer is credited");
            Check(Convert.ToInt32(Member(b!, b!.GetType(), "Deaths") ?? -1) == 1, "the victim is debited");
            Check(Convert.ToInt32(Member(b!, b!.GetType(), "Health") ?? -1) == 0, "the victim is dead after a kill");
        }
        catch (Exception e) { Check(false, "RegisterKill runs", e.InnerException?.Message ?? e.Message); }

        try
        {
            kill.Invoke(state, new object?[] { b, b });
            var scores = Member(state, ms, "TeamScore") as Array;
            int t1 = scores != null && scores.Length > 1 ? Convert.ToInt32(scores.GetValue(1)) : -1;
            Check(t1 == 0, "a suicide does not score for its own team", $"team1={t1}");
        }
        catch (Exception e) { Check(false, "self-kill is handled", e.InnerException?.Message ?? e.Message); }
    }

}
