"""Persistent greenfield game projects backed by Trilobite procedural assets.

Reference projects are intentionally small and dependency-free. They provide a
known-good end-to-end baseline for the model-generated campaign: load the pack,
simulate bounded gameplay, software-render a PPM frame, and print ``GAME_OK``.
"""
from __future__ import annotations

import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

import assetgen
import code_runner


LANGUAGES = {"python", "javascript", "cpp", "csharp"}
DIMENSIONS = assetgen.DIMENSIONS
SOURCE_FILES = {
    "python": "game.py",
    "javascript": "game.js",
    "cpp": "game.cpp",
    "csharp": "Game.cs",
}
DEFAULT_MATRIX = (
    ("python", "2d"),
    ("javascript", "2.5d"),
    ("cpp", "3d"),
    ("csharp", "2d"),
)
FORBIDDEN = {
    "python": ("pygame", "pillow", "pil.", "numpy", "pyglet", "arcade"),
    "javascript": ("three.js", "three'", 'three"', "pixi", "phaser", "babylon"),
    "cpp": ("<sdl", "<sfml", "<gl/", "<glfw", "<vulkan", "raylib"),
    "csharp": (
        "unityengine", "monogame", "microsoft.xna", "raylib",
        "system.drawing", "image.fromfile", "bitmap(",
    ),
}


def workspace_root() -> str:
    return assetgen.workspace_root()


def forge_root() -> str:
    return os.path.join(workspace_root(), "games", "forge")


def _inside_workspace(path: str) -> bool:
    try:
        return os.path.commonpath([workspace_root(), os.path.abspath(path)]) == workspace_root()
    except ValueError:
        return False


def project_dir(name: str, language: str, dimension: str) -> str:
    name = assetgen._safe_slug(name)
    language = normalize_language(language)
    dimension = normalize_dimension(dimension)
    dim_slug = "iso" if dimension == "2.5d" else dimension
    path = os.path.abspath(os.path.join(forge_root(), name, "%s-%s" % (language, dim_slug)))
    if not _inside_workspace(path):
        raise ValueError("unsafe forge project path")
    return path


def normalize_language(language: str) -> str:
    value = (language or "").strip().lower()
    aliases = {"js": "javascript", "node": "javascript", "c++": "cpp", "cs": "csharp", "c#": "csharp"}
    value = aliases.get(value, value)
    if value not in LANGUAGES:
        raise ValueError("language must be one of: %s" % ", ".join(sorted(LANGUAGES)))
    return value


def normalize_dimension(dimension: str) -> str:
    value = (dimension or "2d").strip().lower()
    aliases = {"isometric": "2.5d", "iso": "2.5d", "2.5": "2.5d"}
    value = aliases.get(value, value)
    if value not in DIMENSIONS:
        raise ValueError("dimension must be one of: %s" % ", ".join(sorted(DIMENSIONS)))
    return value


def validate_in_house(code: str, language: str) -> list[str]:
    language = normalize_language(language)
    lowered = (code or "").lower()
    return [token for token in FORBIDDEN[language] if token in lowered]


def contract_issues(code: str, language: str) -> list[str]:
    """Return deterministic violations of the bounded artifact/game contract."""
    text = code or ""
    lowered = text.lower()
    issues = []
    for required in ("game_ok", "frame.ppm"):
        if required not in lowered:
            issues.append("missing required token: %s" % required)
    if not re.search(r"\bassets\b", lowered):
        issues.append("missing generated asset path")
    for token in ("placeholder", "not implemented", "todo:", "actual png loading logic"):
        if token in lowered:
            issues.append("unfinished implementation token: %s" % token)
    if not any(name in lowered for name in ("pickup.wav", "hit.wav", "theme.wav")):
        issues.append("must consume an existing WAV name: pickup.wav, hit.wav, or theme.wav")
    if not any(name in lowered for name in ("background.png", "tiles.png", "sprites.png", "texture.png")):
        issues.append("must consume an existing PNG asset name")
    return issues


def autofix_standard_library(code: str, language: str) -> str:
    """Apply only mechanical standard-header fixes with no design judgment."""
    language = normalize_language(language)
    fixed = code or ""
    if language == "cpp":
        headers = []
        if re.search(r"\b(?:u?int(?:8|16|32|64)_t)\b", fixed) and "<cstdint>" not in fixed:
            headers.append("#include <cstdint>")
        if "std::array" in fixed and "<array>" not in fixed:
            headers.append("#include <array>")
        if "std::filesystem" in fixed and "<filesystem>" not in fixed:
            headers.append("#include <filesystem>")
        if headers:
            fixed = "\n".join(headers) + "\n" + fixed
    return fixed


def _copy_assets(pack: dict, destination: str) -> None:
    os.makedirs(destination, exist_ok=True)
    for row in pack.get("files", []):
        filename = row["path"]
        source = os.path.join(pack["root"], filename)
        target = os.path.join(destination, filename)
        shutil.copy2(source, target)
    shutil.copy2(pack["manifest"], os.path.join(destination, "manifest.json"))


def prepare_project(name: str, language: str, dimension: str, theme: str = "arcane",
                    seed: int = 1337) -> dict:
    language = normalize_language(language)
    dimension = normalize_dimension(dimension)
    root = project_dir(name, language, dimension)
    os.makedirs(root, exist_ok=True)
    pack_name = (assetgen._safe_slug(name) + "-assets")[:assetgen.MAX_NAME].rstrip("-_")
    pack = assetgen.generate_pack(pack_name, dimension, theme, seed)
    _copy_assets(pack, os.path.join(root, "assets"))
    return {
        "name": assetgen._safe_slug(name),
        "language": language,
        "dimension": dimension,
        "theme": theme,
        "seed": seed,
        "root": root,
        "source": os.path.join(root, SOURCE_FILES[language]),
        "frame": os.path.join(root, "frame.ppm"),
        "pack": pack,
    }


PYTHON_2D = r'''"""Trilobite stdlib-only 2D arena. Run with --play for Tk controls."""
import json, math, pathlib, sys

ROOT = pathlib.Path.cwd()
ASSETS = ROOT / "assets"
scene = json.loads((ASSETS / "scene.json").read_text(encoding="utf-8"))
assert (ASSETS / "tiles.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
assert (ASSETS / "pickup.wav").read_bytes()[:4] == b"RIFF"

player = [3.0, 3.0]
enemy = [10.0, 6.0]
score = 0
for tick in range(120):
    player[0] = 3.0 + math.sin(tick * 0.07) * 2.5
    player[1] = 3.0 + math.cos(tick * 0.05) * 1.5
    if abs(player[0] - 7.0) + abs(player[1] - 2.0) < 0.7:
        score = 10

W, H = 160, 90
pixels = bytearray([12, 18, 28] * W * H)
def rect(x, y, w, h, rgb):
    for py in range(max(0, y), min(H, y + h)):
        for px in range(max(0, x), min(W, x + w)):
            i = (py * W + px) * 3; pixels[i:i+3] = bytes(rgb)
for y in range(9):
    for x in range(14):
        tile = scene["map"][y][x]
        rect(10 + x * 10, 5 + y * 9, 9, 8, (35 + tile * 22, 70, 72))
rect(int(10 + player[0] * 10), int(5 + player[1] * 9), 7, 7, (87, 218, 207))
rect(int(10 + enemy[0] * 10), int(5 + enemy[1] * 9), 7, 7, (220, 70, 74))
with (ROOT / "frame.ppm").open("wb") as out:
    out.write(f"P6\n{W} {H}\n255\n".encode()); out.write(pixels)

if "--play" in sys.argv:
    import tkinter as tk
    window = tk.Tk(); window.title("Trilobite 2D Arena")
    canvas = tk.Canvas(window, width=560, height=360, bg="#0c121c"); canvas.pack()
    hero = canvas.create_oval(50, 50, 70, 70, fill="#57dacf", outline="")
    def move(event):
        dx = (-8 if event.keysym == "Left" else 8 if event.keysym == "Right" else 0)
        dy = (-8 if event.keysym == "Up" else 8 if event.keysym == "Down" else 0)
        canvas.move(hero, dx, dy)
    window.bind("<Key>", move); window.mainloop()
print(f"GAME_OK language=python dimension=2d ticks=120 score={score} assets={len(scene['entities'])}")
'''


JAVASCRIPT_ISO = r'''"use strict";
const fs = require("fs"), path = require("path");
const root = process.cwd(), assets = path.join(root, "assets");
const scene = JSON.parse(fs.readFileSync(path.join(assets, "scene.json"), "utf8"));
if (fs.readFileSync(path.join(assets, "tiles.png")).subarray(1,4).toString() !== "PNG") throw Error("bad PNG");
if (fs.readFileSync(path.join(assets, "hit.wav")).subarray(0,4).toString() !== "RIFF") throw Error("bad WAV");
const W=160,H=90,pixels=Buffer.alloc(W*H*3,14);
function pixel(x,y,c){if(x<0||y<0||x>=W||y>=H)return;const i=(y*W+x)*3;pixels[i]=c[0];pixels[i+1]=c[1];pixels[i+2]=c[2];}
function diamond(cx,cy,rx,ry,c){for(let y=-ry;y<=ry;y++){const span=Math.floor(rx*(1-Math.abs(y)/ry));for(let x=-span;x<=span;x++)pixel(cx+x,cy+y,c);}}
function iso(x,y,z=0){return [80+(x-y)*7,12+(x+y)*4-z*7];}
for(let y=0;y<scene.map.length;y++)for(let x=0;x<scene.map[y].length;x++){const p=iso(x,y);diamond(p[0],p[1],7,4,scene.map[y][x]?[72,48,75]:[40,82,78]);}
let hero={x:3,y:3,hp:100}; for(let tick=0;tick<90;tick++){hero.x=3+(tick%12)/12;hero.y=3+Math.sin(tick*.1);}
const hp=iso(hero.x,hero.y,1);diamond(Math.floor(hp[0]),Math.floor(hp[1]),5,5,[96,220,205]);
fs.writeFileSync(path.join(root,"frame.ppm"),Buffer.concat([Buffer.from(`P6\n${W} ${H}\n255\n`),pixels]));
console.log(`GAME_OK language=javascript dimension=2.5d ticks=90 hp=${hero.hp} assets=${scene.entities.length}`);
'''


CPP_3D = r'''#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>
struct Vec3 { double x,y,z; };
struct Point { int x,y; };
int main(){
  namespace fs=std::filesystem; const fs::path root=fs::current_path();
  std::ifstream png(root/"assets"/"texture.png",std::ios::binary); char sig[8]{}; png.read(sig,8);
  if(png.gcount()!=8 || (unsigned char)sig[0]!=0x89 || sig[1]!='P') return 2;
  std::ifstream wav(root/"assets"/"hit.wav",std::ios::binary); char riff[4]{}; wav.read(riff,4);
  if(wav.gcount()!=4 || riff[0]!='R' || riff[1]!='I') return 4;
  std::ifstream obj(root/"assets"/"models.obj"); int vertices=0; std::string line;
  while(std::getline(obj,line)) if(line.rfind("v ",0)==0) ++vertices; if(vertices<8) return 3;
  const int W=160,H=90; std::vector<unsigned char> pix(W*H*3,12);
  auto put=[&](int x,int y,unsigned char r,unsigned char g,unsigned char b){if(x<0||y<0||x>=W||y>=H)return;auto i=(y*W+x)*3;pix[i]=r;pix[i+1]=g;pix[i+2]=b;};
  auto lineDraw=[&](Point a,Point b){int dx=std::abs(b.x-a.x),sx=a.x<b.x?1:-1,dy=-std::abs(b.y-a.y),sy=a.y<b.y?1:-1,e=dx+dy;for(;;){put(a.x,a.y,94,218,207);if(a.x==b.x&&a.y==b.y)break;int e2=2*e;if(e2>=dy){e+=dy;a.x+=sx;}if(e2<=dx){e+=dx;a.y+=sy;}}};
  std::vector<Vec3> cube={{-1,-1,-1},{1,-1,-1},{1,1,-1},{-1,1,-1},{-1,-1,1},{1,-1,1},{1,1,1},{-1,1,1}};
  std::vector<Point> projected; double angle=.65;
  for(auto v:cube){double x=v.x*std::cos(angle)-v.z*std::sin(angle),z=v.x*std::sin(angle)+v.z*std::cos(angle)+5;projected.push_back({int(80+x/z*55),int(45-v.y/z*55)});}
  int edges[][2]={{0,1},{1,2},{2,3},{3,0},{4,5},{5,6},{6,7},{7,4},{0,4},{1,5},{2,6},{3,7}};for(auto&e:edges)lineDraw(projected[e[0]],projected[e[1]]);
  std::ofstream ppm(root/"frame.ppm",std::ios::binary);ppm<<"P6\n"<<W<<" "<<H<<"\n255\n";ppm.write((char*)pix.data(),pix.size());
  std::cout<<"GAME_OK language=cpp dimension=3d frames=120 vertices="<<vertices<<"\n"; return 0;
}
'''


CSHARP_2D = r'''using System;
using System.IO;
class Game {
  static void Main(){
    string root=AppContext.BaseDirectory;
    string sourceRoot=Path.GetDirectoryName(Environment.GetCommandLineArgs()[0]) ?? root;
    string assets=Path.Combine(Directory.GetCurrentDirectory(),"assets");
    byte[] png=File.ReadAllBytes(Path.Combine(assets,"sprites.png"));
    byte[] wav=File.ReadAllBytes(Path.Combine(assets,"theme.wav"));
    if(png.Length<8 || png[1]!=(byte)'P' || wav.Length<4 || wav[0]!=(byte)'R') throw new Exception("asset validation failed");
    const int W=160,H=90; byte[] pixels=new byte[W*H*3];
    for(int i=0;i<pixels.Length;i+=3){pixels[i]=13;pixels[i+1]=22;pixels[i+2]=31;}
    Action<int,int,int,int,int,int,int> rect=(x,y,w,h,r,g,b)=>{for(int py=Math.Max(0,y);py<Math.Min(H,y+h);py++)for(int px=Math.Max(0,x);px<Math.Min(W,x+w);px++){int i=(py*W+px)*3;pixels[i]=(byte)r;pixels[i+1]=(byte)g;pixels[i+2]=(byte)b;}};
    int heroX=20,score=0;for(int tick=0;tick<100;tick++){heroX=20+(tick%80);if(heroX==70)score+=10;}
    for(int x=0;x<W;x+=16)rect(x,70,15,12,45,82,75);rect(heroX,60,8,10,90,218,205);rect(118,60,8,10,220,70,74);
    using(var file=File.Create(Path.Combine(Directory.GetCurrentDirectory(),"frame.ppm"))){byte[] head=System.Text.Encoding.ASCII.GetBytes($"P6\n{W} {H}\n255\n");file.Write(head,0,head.Length);file.Write(pixels,0,pixels.Length);}
    Console.WriteLine($"GAME_OK language=csharp dimension=2d ticks=100 score={score} assets={png.Length+wav.Length}");
  }
}
'''


REFERENCE_SOURCE = {
    ("python", "2d"): PYTHON_2D,
    ("javascript", "2.5d"): JAVASCRIPT_ISO,
    ("cpp", "3d"): CPP_3D,
    ("csharp", "2d"): CSHARP_2D,
}


def reference_source(language: str, dimension: str) -> str:
    key = (normalize_language(language), normalize_dimension(dimension))
    if key not in REFERENCE_SOURCE:
        raise ValueError("no reference project for %s/%s" % key)
    return REFERENCE_SOURCE[key]


def save_source(project: dict, code: str) -> str:
    forbidden = validate_in_house(code, project["language"])
    if forbidden:
        raise ValueError("third-party dependency token(s): %s" % ", ".join(forbidden))
    with open(project["source"], "w", encoding="utf-8") as handle:
        handle.write(code.rstrip() + "\n")
    return project["source"]


def _valid_frame(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            header = handle.read(32)
        return header.startswith(b"P6\n") and os.path.getsize(path) > 1024
    except OSError:
        return False


def run_project(project: dict, code: str, timeout: int = 20) -> dict:
    save_source(project, code)
    result = code_runner.run_code(
        code,
        language=project["language"],
        timeout=max(2, min(int(timeout), 60)),
        cwd=project["root"],
    )
    output = ((result.get("stdout") or "") + "\n" + (result.get("stderr") or "")).strip()
    frame_ok = _valid_frame(project["frame"])
    ok = bool(result.get("ok") and "GAME_OK" in output and frame_ok)
    return {
        "ok": ok,
        "language": project["language"],
        "dimension": project["dimension"],
        "root": project["root"],
        "source": project["source"],
        "frame": project["frame"],
        "frame_ok": frame_ok,
        "output": output,
        "runner": result,
    }


def run_reference(name: str, language: str, dimension: str, theme: str = "arcane",
                  seed: int = 1337, timeout: int = 20) -> dict:
    project = prepare_project(name, language, dimension, theme, seed)
    return run_project(project, reference_source(language, dimension), timeout)


def run_reference_suite(name: str, theme: str = "arcane", seed: int = 1337,
                        max_workers: int = 2, timeout: int = 20) -> dict:
    workers = max(1, min(int(max_workers or 1), 4, len(DEFAULT_MATRIX)))
    results = [None] * len(DEFAULT_MATRIX)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_reference, name, lang, dim, theme, seed + index, timeout): index
            for index, (lang, dim) in enumerate(DEFAULT_MATRIX)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                lang, dim = DEFAULT_MATRIX[index]
                results[index] = {"ok": False, "language": lang, "dimension": dim, "output": "ERROR: %s" % exc}
    return {
        "ok": all(row.get("ok") for row in results),
        "name": assetgen._safe_slug(name),
        "workers": workers,
        "results": results,
    }


def generation_prompt(project: dict, concept: str = "") -> str:
    language, dimension = project["language"], project["dimension"]
    return (
        "Create a compact greenfield %s game in %s. %s\n"
        "Use only the language standard library and OS-native APIs: no third-party "
        "engines, packages, downloads, or external services. The working directory "
        "contains these exact generated files: assets/manifest.json, assets/scene.json, "
        "assets/background.png, tiles.png, sprites.png, texture.png, pickup.wav, hit.wav, "
        "theme.wav, and models.obj. Use those names exactly. Validate PNG/WAV signatures "
        "and inspect metadata; do not write a full image/audio decoder. For 3D also parse "
        "vertex lines from models.obj. Run a bounded 60-180 tick gameplay "
        "simulation, software-render frame.ppm (P6, at least 64x48), print a final line "
        "starting exactly GAME_OK with useful metrics, and exit within 20 seconds. "
        "Write frame.ppm as a file in the working directory, never to stdout. Avoid "
        "interactive input in smoke mode. No placeholders, TODOs, omitted logic, or fake "
        "loaders are allowed. Return exactly one complete fenced %s "
        "code block and no prose. Do not claim execution or test success."
        % (dimension, language, concept.strip(), language)
    )


def format_suite(suite: dict) -> str:
    rows = suite.get("results") or []
    passed = sum(1 for row in rows if row.get("ok"))
    lines = [
        "greenfield game forge: %d/%d passed (workers=%d)" %
        (passed, len(rows), suite.get("workers", 0)),
    ]
    for row in rows:
        lines.append("[%s] %s/%s" % (
            "PASS" if row.get("ok") else "FAIL",
            row.get("language"), row.get("dimension"),
        ))
        if row.get("output"):
            lines.append(str(row["output"])[:1000])
        if row.get("root"):
            lines.append("  root: %s" % row["root"])
    return "\n".join(lines)
