"""Command line interface: ad2render {info,dump,scene,render,batch,viewer}."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .codec import DecodeError, decode_file, to_jsonable
from .ground import GROUND_KINDS
from .scene import FADING_DOOR_MODES, normalize, with_fading_doors


def _log(msg):
    print(msg, file=sys.stderr)


def _parse_color(s: str):
    if s in ("transparent", "none"):
        return (0, 0, 0, 0)
    s = s.lstrip("#")
    if len(s) == 6:
        s += "ff"
    if len(s) != 8:
        raise argparse.ArgumentTypeError("background must be 'transparent' or #rrggbb[aa]")
    return tuple(int(s[i:i + 2], 16) for i in range(0, 8, 2))


def load_scene(path: Path, fs, conv) -> dict:
    from .convert import resolve_scene
    if path.suffix.lower() == ".json":  # a Scene JSON written by `ad2render scene`
        scene = json.loads(path.read_text(encoding="utf-8"))
        if scene.get("version") != 1 or "entities" not in scene:
            raise DecodeError(f"{path.name} is not an ad2render Scene JSON")
    else:
        scene = normalize(decode_file(path), name=path.stem)
    return resolve_scene(scene, conv)


def _make_fs(args):
    from .convert import Converter
    from .gamefs import GameFS
    fs = GameFS(args.gmod, workshop_dir=args.workshop)
    for w in fs.warnings:
        _log(f"warning: {w}")
    return fs, Converter(fs, args.cache, force=args.no_cache)


def _render_one(scene: dict, out: Path, args, glb_cache=None, conv=None):
    """The command-line renderer: flat-shaded, drawn with Pillow. (The textured, shadowed render is made by the
    viewer in a browser.)"""
    from .render_pil import render
    scene = with_fading_doors(scene, args.fading_doors)
    if args.ground != "none":
        from .ground import ground_spec
        scene = dict(scene, ground=ground_spec(scene, conv, args.ground, args.ground_color))
    render(scene, out, args.width, args.height, args.view, args.yaw, args.pitch,
           args.background, glb_cache=glb_cache, constraints=args.constraints)


def _report(scene, out_png: Path | None, args):
    rep = scene.get("report", {})
    for m in rep.get("missing_models", []):
        _log(f"  missing model: {m}")
    for m in rep.get("missing_materials", []):
        _log(f"  missing material: {m}")
    if args.report:
        data = {"name": scene["name"], "info": scene["info"], "entities": len(scene["entities"]),
                "output": str(out_png) if out_png else None, **rep}
        Path(args.report).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def cmd_info(args):
    d = decode_file(args.file)
    ents = d.data["Entities"]
    print(f"revision: {d.revision}")
    for k, v in d.info.items():
        print(f"{k}: {v}")
    print(f"entities: {len(ents)}")
    cons = d.data.get("Constraints") or {}
    print(f"constraints: {len(cons)}")
    from collections import Counter
    for (cls, mdl), n in Counter((e.get("Class"), e.get("Model")) for e in ents.values()).most_common(args.top):
        print(f"  {n:4d}  {cls}  {mdl}")


def cmd_dump(args):
    d = decode_file(args.file)
    out = {"revision": d.revision, "info": d.info, "dupe": to_jsonable(d.data)}
    text = json.dumps(out, indent=None if args.compact else 1, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)


def cmd_scene(args):
    if args.no_resolve:
        scene = normalize(decode_file(args.file), name=Path(args.file).stem)
    else:
        fs, conv = _make_fs(args)
        scene = load_scene(Path(args.file), fs, conv)
    text = json.dumps(scene, indent=1, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)


def cmd_render(args):
    src = Path(args.file)
    out = Path(args.output) if args.output else src.with_suffix(".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    t = time.time()
    fs, conv = _make_fs(args)
    scene = load_scene(src, fs, conv)
    _log(f"{src.name}: {len(scene['entities'])} entities resolved in {time.time() - t:.1f}s")
    _render_one(scene, out, args, conv=conv)
    _report(scene, out, args)
    _log(f"wrote {out} ({time.time() - t:.1f}s total)")


def cmd_viewer(args):
    """The local viewer: open any dupe file or a folder of them. Loopback use only."""
    from .server import App, serve
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("the viewer has no access control; bind it to localhost")
    fs, conv = _make_fs(args)
    app = App(fs, conv, dupe_dir=Path(args.dir) if args.dir else None, log=_log)
    serve(app, args.host, args.port, open_browser=args.open)


def cmd_batch(args):
    src = Path(args.folder)
    outdir = Path(args.output) if args.output else src / "thumbnails"
    outdir.mkdir(parents=True, exist_ok=True)
    files = sorted(src.rglob("*.txt") if args.recursive else src.glob("*.txt"))
    fs, conv = _make_fs(args)
    summary, glb_cache, failed = [], {}, 0
    for f in files:
        rel = f.relative_to(src).with_suffix(".png")
        out = outdir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        t = time.time()
        try:
            scene = load_scene(f, fs, conv)
            _render_one(scene, out, args, glb_cache, conv)
            rep = scene["report"]
            summary.append({"file": str(f), "output": str(out), "entities": len(scene["entities"]),
                            "missing_models": rep["missing_models"],
                            "missing_materials": rep["missing_materials"]})
            _log(f"ok    {f.name} -> {out} ({time.time() - t:.1f}s, "
                 f"{len(rep['missing_models'])} missing models)")
        except Exception as e:  # one bad dupe must not stop the batch
            failed += 1
            summary.append({"file": str(f), "error": str(e)})
            _log(f"FAIL  {f.name}: {e}")
    (outdir / "report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(f"{len(files) - failed}/{len(files)} rendered; report in {outdir / 'report.json'}")
    return 1 if failed else 0


def _add_render_opts(p):
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=960)
    p.add_argument("--view", default="iso", choices=("iso", "iso2", "front", "back", "side", "top"))
    p.add_argument("--yaw", type=float, help="camera yaw around the build in degrees (overrides --view)")
    p.add_argument("--pitch", type=float, help="camera elevation in degrees (overrides --view)")
    p.add_argument("--background", "--bg", type=_parse_color, default=(0, 0, 0, 0),
                   help="'transparent' (default) or #rrggbb[aa]")
    p.add_argument("--constraints", action="store_true", help="overlay constraints as lines")
    p.add_argument("--ground", choices=GROUND_KINDS, default="none",
                   help="ground under the build, at the level stored in the dupe (default none)")
    p.add_argument("--ground-color", default="#6e6e6e", help="colour for --ground solid (#rrggbb)")
    p.add_argument("--fading-doors", choices=FADING_DOOR_MODES, default="solid",
                   help="draw fading doors and transparent props solid (as saved, default), as see-through ghosts, or hide them")
    p.add_argument("--report", help="write a JSON report (missing assets etc.) to this path")


def _add_fs_opts(p):
    p.add_argument("--gmod", help="Garry's Mod install folder (auto-detected from Steam, or $GMOD_PATH)")
    p.add_argument("--workshop", help="workshop content/4000 folder (default: next to the Steam install, "
                                      "or $AD2RENDER_WORKSHOP)")
    p.add_argument("--cache", help="model cache directory (default %%LOCALAPPDATA%%/ad2render/cache)")
    p.add_argument("--no-cache", action="store_true", help="reconvert models even if cached")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ad2render", description="Render Advanced Duplicator 2 dupes")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="print header metadata and a model summary")
    p.add_argument("file")
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("dump", help="decode a dupe to raw JSON")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    p.add_argument("--compact", action="store_true")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("scene", help="write the normalized Scene JSON")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    p.add_argument("--no-resolve", action="store_true", help="skip asset resolution (no GMod needed)")
    _add_fs_opts(p)
    p.set_defaults(func=cmd_scene)

    p = sub.add_parser("render", help="quick flat-shaded PNG of a dupe (the textured render is made in the browser)")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    _add_fs_opts(p)
    _add_render_opts(p)
    p.set_defaults(func=cmd_render)

    def server_opts(p):
        p.add_argument("--host", default="127.0.0.1", help="bind address (localhost only)")
        p.add_argument("--port", type=int, default=8000)
        p.add_argument("--open", action="store_true", help="open the viewer in a browser")
        _add_fs_opts(p)

    p = sub.add_parser("viewer", help="3D viewer for dupe files and folders (localhost only)")
    server_opts(p)
    p.add_argument("--dir", help="folder of dupes to offer in the viewer's file list")
    p.set_defaults(func=cmd_viewer)

    p = sub.add_parser("batch", help="render every dupe in a folder to flat-shaded PNGs")
    p.add_argument("folder")
    p.add_argument("-o", "--output", help="output folder (default <folder>/thumbnails)")
    p.add_argument("-r", "--recursive", action="store_true")
    _add_fs_opts(p)
    _add_render_opts(p)
    p.set_defaults(func=cmd_batch, width=512, height=384)
    return ap


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except DecodeError as e:
        _log(f"error: {getattr(args, 'file', '')}{': ' if getattr(args, 'file', '') else ''}{e}")
        return 2
    except OSError as e:  # missing install, unreadable input, unwritable cache/output
        _log(f"error: {e}")
        return 2
    except RuntimeError as e:
        _log(f"error: {e}")
        return 3
