# ad2render

Decode Garry's Mod **Advanced Duplicator 2** dupes (`.txt`, AD2F revisions 3–5) and render them with the real models
and textures from your own Garry's Mod install. You get a command line tool, a Python library and a three.js viewer
that runs in the browser. The 3D view is drawn by the browser (on the GPU, with software WebGL as a fallback), so
nothing is rendered on the server.

```
.txt dupe ─► codec ─► scene (Scene JSON) ─► convert (MDL/VVD/VTX + VMT/VTF ─► cached .glb) ─► three.js viewer
                                                 ▲                                         (browser: GPU, else software)
                               gamefs: VPKs, addons/*.gma, workshop
```

> **Disclaimer.** This is an independent project. It was not created by, and is not affiliated with or endorsed by,
> the Wiremod team (the developers of Advanced Duplicator 2), Valve or Facepunch Studios. It only reads the dupe
> files that Advanced Duplicator 2 saves. Please report problems with this tool here, not to the AdvDupe2 project.
>
> This repository contains no game content. Models and textures are read from a Garry's Mod install that you own,
> and converted into a local cache. Never commit or redistribute that cache.

## Requirements

- Python 3.11+ and Pillow (`pip install -e .`, or `pip install -e .[test]` to get the tests as well)
- Garry's Mod, found through the Steam registry. Otherwise pass `--gmod` or set `GMOD_PATH`.
- A browser with WebGL for the viewer.

## Usage

```
ad2render info  dupe.txt                       # header + model summary
ad2render dump  dupe.txt -o dupe.json          # raw decoded tree
ad2render scene dupe.txt -o scene.json         # normalized Scene JSON (add --no-resolve to skip GMod)
ad2render render dupe.txt -o out.png --view iso --bg transparent   # quick flat-shaded PNG (Pillow)
ad2render render dupe.txt --report report.json # missing models / materials
ad2render batch  path/to/dupes -o thumbs -r    # flat-shaded thumbnails + thumbs/report.json
ad2render viewer --dir path/to/dupes --open    # the 3D viewer on http://127.0.0.1:8000/
```

`python -m ad2render ...` works without installing.

`render` and `batch` only make a quick flat-shaded preview, with no textures and no shadows. For the full render,
use the viewer.

Views: `iso`, `iso2`, `front`, `back`, `side`, `top`, or set your own angle with `--yaw/--pitch`.
`--ground none|grass|concrete|dirt|solid` puts a ground under the build; use `--ground-color #rrggbb` with `solid`.
The ground sits at the level stored in the dupe (AdvDupe2's `HeadEnt.Z`), so props sunk into the floor don't make
the build float. `--constraints` draws constraints as lines. `--fading-doors solid|ghost|hidden` draws fading doors
closed (the default), see-through, or not at all.

## What is rendered

- Model, skin, bodygroups, `ModelScale`, colour and alpha (when `RenderMode` ≠ 0), and the material tool's override.
- Models are posed with their default sequence, as the game draws them. Without that, models exported with a rotated
  root (keypads, `breenchair`, some cabinets and doors) would come out turned 90°.
- Ragdolls: the mesh is skinned so that each physics bone sits at its saved physics-object transform.
- Buttons (`gmod_button`) and keypads that open fading doors: the viewer draws a line from each one to the doors on
  the same numpad key. It also lists the keys, flags doors that no button in the dupe presses, and names the key on
  hover. Keys belong to the player, not the entity, so this shows what one player gets after pasting. The keypad
  password is never sent to the browser.
- A missing model is drawn as a translucent magenta box and listed in the report. The box is sized from the `.mdl`
  hull when that can be read.
- Some props have faces that coincide with a prop of a different colour or texture, such as a panel flush with a
  wall. These would flicker between the two materials ("z-fighting"). To prevent this, the prop with the deliberate
  look is grown by 0.5 mm per stacked layer so that it always shows. That is the prop with a custom colour or a
  material override, or otherwise the one placed later.

Known limitations:

- E2 holograms and other runtime-spawned entities are not drawn.
- Lua-drawn entities show only their base model.
- Materials with proxies or animation render static.
- Only LOD0 of the first VTX layout is used.

## Viewer

```
ad2render viewer --dir path/to/dupes --open        # no access control: binds to localhost only
```

You can open a dupe in three ways: from the server-side list (`--dir`), with the file picker, or by dropping a `.txt`
file on the viewport. A link such as `/?file=sub/dupe.txt&mode=hidden&view=front&fading=ghost` opens a dupe directly.

- **Wireframe**, **hidden-line** or **solid** preview, with Iso/Front/Side/Top presets, FOV, constraints and a
  ground. The solid view has a sun with soft shadows and image-based ambient light. Turn the shadows off with the
  **Shadows** checkbox (or `?shadows=0`) on weak devices.
- A badge shows whether the GPU or software WebGL ("CPU") is drawing. The software path starts with shadows off and
  a lower pixel ratio.
- **Fading doors** can be shown solid, as see-through ghosts, or hidden. Props saved with transparency follow the
  same setting.
- **Fit** (<kbd>F</kbd>) frames the whole build. **Reset view** (<kbd>Home</kbd>) returns to the default iso view
  and 40° FOV.
- Navigation:
  - Move with the arrow keys and <kbd>PgUp</kbd>/<kbd>PgDn</kbd>. Hold <kbd>Shift</kbd> to go faster.
  - The scroll wheel zooms toward the point under the cursor.
  - Dragging with the middle button held zooms as well.
- **Render this view** (<kbd>R</kbd>) saves the solid view at the size you choose, from 480 to 1920 px. Renders are
  listed so you can download them, restore the camera they were taken from, or lay them over the viewport.
- three.js 0.160.0 is bundled (`ad2render/viewer/vendor/three`, MIT), so the page works offline.

The server uses only the standard library. Scenes stay on the server: the browser gets unguessable asset URLs and
never sees file paths. Uploads are capped at 16 MB.

## Using it as a library

```python
from ad2render.codec import decode
from ad2render.scene import normalize
from ad2render.gamefs import GameFS
from ad2render.convert import Converter, resolve_scene

fs = GameFS(None)                         # None: find Garry's Mod through Steam / $GMOD_PATH
conv = Converter(fs, None)                # None: default cache directory
scene = normalize(decode(open("dupe.txt", "rb").read()), name="dupe")
resolve_scene(scene, conv)                # adds a .glb path, bounds and texture overrides to each entity
```

| Module                    | Role                                                                                      |
|---------------------------|-------------------------------------------------------------------------------------------|
| `codec`                   | AD2F header, LZMA payload, serializer (revisions 3–5), with caps against hostile files    |
| `scene`                   | the Scene JSON: entities with 4×4 matrices in Source units, constraints, numpad links     |
| `gamefs`                  | read-only virtual filesystem over VPKs, `.gma` addons, loose folders and workshop content |
| `mdl`, `pose`, `material` | Source model (MDL/VVD/VTX/PHY) and material (VMT/VTF) readers                             |
| `convert`, `gltf`         | model → cached `.glb`, scene resolution, missing-asset report                             |
| `zfight`, `ground`        | coplanar-face fix, ground level and textures                                              |
| `render_pil`              | flat-shaded Pillow renderer for thumbnails                                                |
| `server`, `viewer/`       | the HTTP server and the three.js viewer                                                   |

## Cache

Converted models are kept in `%LOCALAPPDATA%\ad2render\cache`. Override the location with `--cache` or
`AD2RENDER_CACHE`, and force reconversion with `--no-cache`. `--workshop` / `AD2RENDER_WORKSHOP` points at a SteamCMD
`workshop/content/4000` folder, and `AD2RENDER_ADDONS` mounts extra addons behind the base game.

On Linux, asset lookups are case-insensitive. Asset names from a dupe that contain `..` or a drive letter are refused.

## Tests

```
python -m pytest
```

The tests build their dupes in code (`tests/dupes.py`), so no sample files are needed. The asset and server tests run
only when a Garry's Mod install is found.

## License

MIT, see [LICENSE.md](LICENSE.md). three.js is MIT, see `ad2render/viewer/vendor/three/LICENSE`.
