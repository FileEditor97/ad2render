// ad2render web viewer: wireframe / hidden-line / solid preview, and a render of the current view, all in the browser.
//
// World space: Source coordinates * INCH, Z up, metres.
// The cached .glb files are glTF Y-up, so each template geometry is rotated +90° about X once.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const INCH = 0.0254;
const VIEWS = { iso: [45, 30], iso2: [225, 30], front: [0, 0], side: [90, 0], top: [0, 89.9] };
const CONSTRAINT_COLORS = {
  Weld: 0xffc400, Rope: 0xbe783c, Elastic: 0x5ac8ff, Axis: 0xff5050, Ballsocket: 0x78ff78, NoCollide: 0xb4b4b4,
};
// keypad / button -> fading door lines (shared numpad key): access granted, access denied, button press
const LINK_COLORS = { granted: 0x3ddc84, denied: 0xff6b6b, press: 0x5ab4ff };
const $ = (s) => document.querySelector(s);
const count = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// GMod KEY_ enum, as the numpad tool names the keys
const PAD_KEYS = { 47: '/', 48: '*', 49: '−', 50: '+', 51: 'Enter', 52: '.' };
const OTHER_KEYS = ['[', ']', ';', "'", '`', ',', '.', '/', '\\', '-', '=', 'Enter', 'Space', 'Backspace', 'Tab',
  'Caps Lock', 'Num Lock', 'Esc', 'Scroll Lock', 'Insert', 'Delete', 'Home', 'End', 'Page Up', 'Page Down', 'Pause',
  'Left Shift', 'Right Shift', 'Left Alt', 'Right Alt', 'Left Ctrl', 'Right Ctrl', 'Left Win', 'Right Win',
  'Menu', '↑', '←', '↓', '→']; // KEY_LBRACKET (53) .. KEY_RIGHT (91)
function keyName(k) {
  if (k >= 1 && k <= 10) return String(k - 1);
  if (k >= 11 && k <= 36) return String.fromCharCode(54 + k); // KEY_A = 11
  if (k >= 37 && k <= 46) return `Num ${k - 37}`;
  if (PAD_KEYS[k]) return `Num ${PAD_KEYS[k]}`;
  if (k >= 53 && k <= 91) return OTHER_KEYS[k - 53];
  if (k >= 92 && k <= 103) return `F${k - 91}`;
  return `#${k}`;
}

// ---------------------------------------------------------------- three.js setup
const canvas = $('#canvas');

// All rendering happens in the browser: on the GPU when there is one, otherwise on the CPU (the browser's own
// software WebGL). The first attempt refuses a software rasteriser (`failIfMajorPerformanceCaveat`), the second
// takes whatever the browser offers. With neither there is no 3D here.
function createRenderer() {
  for (const [kind, strict] of [['gpu', true], ['software', false]]) {
    try {
      const r = new THREE.WebGLRenderer({
        canvas, antialias: kind === 'gpu', alpha: true, // alpha: transparent renders
        powerPreference: 'high-performance', failIfMajorPerformanceCaveat: strict,
      });
      // Browsers differ in what counts as a "major performance caveat": SwiftShader, llvmpipe and friends can
      // still be handed out as a normal context. Judge by the driver name as well, so the badge tells the truth.
      const gl = r.getContext();
      const info = gl.getExtension('WEBGL_debug_renderer_info');
      const driver = (info && gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) || '';
      return { renderer: r, kind: kind === 'gpu' && /swiftshader|llvmpipe|softpipe|software|basic render/i.test(driver) ? 'software' : kind };
    } catch { /* that path is not available: try the next one */ }
  }
  return null;
}

const created = createRenderer();
if (!created) {
  showNoWebGL();
  throw new Error('WebGL is not available in this browser'); // nothing else in this module can run without it
}
const { renderer, kind: renderKind } = created;
const softwareMode = renderKind === 'software';
const livePixelRatio = () => (softwareMode ? 1 : Math.min(window.devicePixelRatio, 2));
{
  const badge = $('#gpu-badge');
  badge.hidden = false;
  badge.textContent = softwareMode ? 'CPU (software)' : 'GPU';
  badge.classList.toggle('software', softwareMode);
  const info = renderer.getContext().getExtension('WEBGL_debug_renderer_info');
  badge.title = (info && renderer.getContext().getParameter(info.UNMASKED_RENDERER_WEBGL)) || '';
}
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.shadowMap.autoUpdate = false; // the sun is fixed in the world: re-render shadows only when the scene changes
const shadowsDirty = () => { renderer.shadowMap.needsUpdate = true; };
renderer.setPixelRatio(livePixelRatio());
const viewportColor = getComputedStyle(document.documentElement).getPropertyValue('--viewport').trim();
const world = new THREE.Scene();
world.background = new THREE.Color(viewportColor);

const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 1000);
camera.up.set(0, 0, 1); // must precede OrbitControls
camera.position.set(3, 3, 2);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.12;
const sceneSphere = new THREE.Sphere(new THREE.Vector3(), 1);

// Wheel and middle-drag zoom are handled here rather than by OrbitControls. three r160 divides by
// (devicePixelRatio | 0), which is 0 at <100% browser zoom or display scaling and turns one notch or one
// pixel of middle-drag into a huge jump, and it ignores Firefox's line-based wheel deltas. Each step moves
// ZOOM_STEP of the way toward the surface under the pointer (or the target depth when nothing is hit).
// Moving along the pointer ray keeps that point under the cursor, and the orbit target follows to its
// depth so rotating pivots there. (Touch pinch-zoom still goes through OrbitControls.)
const ZOOM_STEP = 0.12;
const DRAG_PX_PER_STEP = 40; // middle-drag distance equivalent to one wheel notch
const pickMeshes = [];
const raycaster = new THREE.Raycaster();
controls.mouseButtons.MIDDLE = -1; // OrbitControls ignores the middle button; handled below

canvas.parentElement.addEventListener('wheel', (e) => {
  e.preventDefault();
  e.stopPropagation(); // keep the event from OrbitControls' own wheel handler on the canvas
  // normalise to pixels; Firefox reports about 3 lines per notch
  let dy = e.deltaY * (e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 100 : 1);
  if (!dy) return;
  dy = Math.max(-100, Math.min(100, dy)); // one notch = one step; trackpads give smaller, smoother steps
  zoomAt(e.clientX, e.clientY, dy / 100);
}, { capture: true, passive: false });

// Middle-drag: up zooms in, down zooms out, toward the point where the button went down.
let middleDrag = null;
canvas.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault(); }); // no Firefox autoscroll
canvas.addEventListener('pointerdown', (e) => {
  if (e.button !== 1) return;
  e.preventDefault();
  middleDrag = { id: e.pointerId, x: e.clientX, y: e.clientY, lastY: e.clientY };
  try { canvas.setPointerCapture(e.pointerId); } catch { /* keep zooming even without capture */ }
});
canvas.addEventListener('pointermove', (e) => {
  if (!middleDrag || e.pointerId !== middleDrag.id) return;
  const dy = e.clientY - middleDrag.lastY;
  middleDrag.lastY = e.clientY;
  if (dy) zoomAt(middleDrag.x, middleDrag.y, Math.max(-3, Math.min(3, dy / DRAG_PX_PER_STEP)));
});
const endMiddleDrag = (e) => { if (middleDrag && e.pointerId === middleDrag.id) middleDrag = null; };
canvas.addEventListener('pointerup', endMiddleDrag);
canvas.addEventListener('pointercancel', endMiddleDrag);

// steps < 0 zooms in, > 0 zooms out; one wheel notch is 1 step
function zoomAt(clientX, clientY, steps) {
  // several wheel/pointer events can arrive between frames; the camera matrix is otherwise only
  // refreshed at render time, and a stale one would aim the ray from the old position
  camera.updateMatrixWorld();
  const notches = Math.abs(steps);
  const rect = canvas.getBoundingClientRect();
  const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
  raycaster.setFromCamera(ndc, camera);
  const ray = raycaster.ray.direction;
  const fwd = camera.getWorldDirection(new THREE.Vector3());
  const hit = pickMeshes.length ? raycaster.intersectObjects(pickMeshes, false)[0] : null;
  // distance along the pointer ray to the point we zoom toward
  let dist = hit ? hit.distance : controls.target.clone().sub(camera.position).dot(fwd) / Math.max(ray.dot(fwd), 1e-3);
  dist = Math.max(dist, 1e-4);

  const keep = Math.pow(1 - ZOOM_STEP, notches); // fraction of the distance left after zooming in
  let move = steps < 0 ? dist * (1 - keep) : -dist * (1 / keep - 1);
  if (steps < 0) move = Math.max(move, sceneSphere.radius * 0.004 * notches); // creep through walls, never stall
  const next = camera.position.clone().addScaledVector(ray, move);
  if (steps > 0 && next.distanceTo(sceneSphere.center) > controls.maxDistance) return;
  camera.position.copy(next);
  const depth = Math.max((dist - move) * ray.dot(fwd), sceneSphere.radius * 0.01);
  controls.target.copy(camera.position).addScaledVector(fwd, depth);
  controls.update();
}

// Near/far follow the camera, so zoomed-in parts aren't clipped and distant views keep depth precision.
function updateClipping() {
  const r = sceneSphere.radius;
  const surface = camera.position.distanceTo(sceneSphere.center) - r; // < 0 when inside the build
  let near = Math.max(surface * 0.5, r * 0.0005, 0.0005);
  let far = Math.max(surface, 0) + r * 2.5 + 1;
  if (groundMesh) { // the ground reaches nearer to the camera and farther away than the build
    near = Math.max(Math.min(near, Math.abs(camera.position.z - groundMesh.position.z) * 0.5), 0.0005);
    far = Math.max(far, camera.position.distanceTo(groundMesh.position) + groundMesh.geometry.parameters.width * 0.75);
  }
  if (Math.abs(near - camera.near) > camera.near * 0.05 || Math.abs(far - camera.far) > camera.far * 0.05) {
    camera.near = near;
    camera.far = far;
    camera.updateProjectionMatrix();
  }
}

// Lighting: a sun fixed in the world (it casts the shadows, so they don't swing around while you orbit),
// image-based ambient light, a little sky/ground light, and a weak camera-following fill without shadows.
const coarsePointer = matchMedia('(pointer: coarse)').matches;
const ENV_INTENSITY = 0.5;
world.environment = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;
const hemi = new THREE.HemisphereLight(0xdfe6f0, 0x3a3f47, 0.5);
hemi.position.set(0, 0, 1);
world.add(hemi);
const SUN_DIRECTION = new THREE.Vector3(0.4, -0.2, 0.9).normalize(); // toward the sun: high, from +x, so the default iso view is lit from its upper left
const sun = new THREE.DirectionalLight(0xfff3df, 2.4);
sun.castShadow = true;
const SHADOW_SIZE = coarsePointer || softwareMode ? 1024 : 2048; // smaller maps on touch devices and on the CPU
sun.shadow.mapSize.set(SHADOW_SIZE, SHADOW_SIZE);
sun.shadow.bias = -0.0004;
world.add(sun, sun.target);
const key = new THREE.DirectionalLight(0xffffff, 0.45); // fill from the upper left of the camera
key.position.set(-0.6, 1.1, 1);
camera.add(key);
world.add(camera);

// Fit the sun's orthographic shadow camera to the build (called whenever the framing is recomputed).
function updateSun() {
  const r = Math.max(sceneSphere.radius, 0.1);
  sun.target.position.copy(sceneSphere.center);
  sun.target.updateMatrixWorld();
  sun.position.copy(sceneSphere.center).addScaledVector(SUN_DIRECTION, r * 3);
  const c = sun.shadow.camera;
  const half = r * 1.1;
  c.left = -half; c.right = half; c.top = half; c.bottom = -half;
  c.near = r * 0.5; c.far = r * 6.5;
  c.updateProjectionMatrix();
  sun.shadow.normalBias = r * 0.003; // the many flush plates would show shadow acne without it
  shadowsDirty();
}

const root = new THREE.Group();
world.add(root);
let grid = null;
let constraintLines = null;
let linkLines = null;

const loader = new GLTFLoader();
const templates = new Map();   // glb url -> Promise<[{geometry, edges, material}]>
const lineMats = new Map();
const solidMats = new Map();
const textures = new Map();
const hiddenMat = new THREE.MeshBasicMaterial({
  color: world.background, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1, side: THREE.DoubleSide,
});

const state = { sceneId: null, scene: null, mode: 'wire', fading: 'solid', ground: 'grid', entities: [], name: '', busy: false };

// ---------------------------------------------------------------- settings (per-browser convenience)
function loadSettings() {
  try { return JSON.parse(localStorage.getItem('ad2render.settings') || '{}'); } catch { return {}; }
}
function saveSettings() {
  try {
    localStorage.setItem('ad2render.settings', JSON.stringify({
      width: $('#width').value,
      bgTransparent: $('#bg-transparent').checked, bg: $('#bg-color').value,
      mode: state.mode, fov: $('#fov').value, fading: state.fading, ground: state.ground, shadows: sun.castShadow,
      groundColor: $('#ground-color').value,
    }));
  } catch { /* storage unavailable: settings just don't persist */ }
}

// ---------------------------------------------------------------- resizing
function resize() {
  const { clientWidth: w, clientHeight: h } = canvas.parentElement;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  updateHeight();
}
new ResizeObserver(resize).observe(canvas.parentElement);

// Renders are 480-1920 px in both directions and at most 2.5:1 either way. The height follows the viewport's aspect ratio; when that would leave
// the range (a very wide or tall window) the height is pinned to the limit and the width follows it.
const RENDER_MIN = 480, RENDER_MAX = 1920, RENDER_MAX_ASPECT = 2.5;
const clampRender = (v) => Math.max(RENDER_MIN, Math.min(RENDER_MAX, Math.round(v)));
function renderSize() {
  const aspect = Math.max(1 / RENDER_MAX_ASPECT, Math.min(RENDER_MAX_ASPECT, camera.aspect));
  let w = clampRender(Number($('#width').value) || 1280);
  let h = Math.round(w / aspect);
  if (h < RENDER_MIN || h > RENDER_MAX) { h = clampRender(h); w = clampRender(h * aspect); }
  return [w, h];
}
function updateHeight() { $('#height').value = renderSize()[1]; }
function normalizeWidth() { $('#width').value = renderSize()[0]; updateHeight(); }

// ---------------------------------------------------------------- models
function loadTemplate(url) {
  if (!templates.has(url)) {
    templates.set(url, loader.loadAsync(url).then((gltf) => {
      gltf.scene.updateMatrixWorld(true);
      const parts = [];
      gltf.scene.traverse((o) => {
        if (!o.isMesh) return;
        const g = o.geometry.clone();
        g.applyMatrix4(o.matrixWorld);
        g.rotateX(Math.PI / 2); // glTF Y-up -> Z-up
        g.computeBoundingSphere();
        parts.push({ geometry: g, edges: new THREE.EdgesGeometry(g, 20), material: o.material });
      });
      return parts;
    }));
  }
  return templates.get(url);
}

function srgb(c) { return new THREE.Color().setRGB(c[0] / 255, c[1] / 255, c[2] / 255, THREE.SRGBColorSpace); }

function lineMaterial(ent) {
  let c;
  if (ent.placeholder) c = new THREE.Color(0xff00ff);
  else {
    c = srgb(ent.color);
    const hsl = {}; c.getHSL(hsl);
    c.setHSL(hsl.h, hsl.s, Math.max(hsl.l, 0.38)); // keep black props visible on the dark viewport
  }
  const k = c.getHexString();
  if (!lineMats.has(k)) lineMats.set(k, new THREE.LineBasicMaterial({ color: c }));
  return lineMats.get(k);
}

const textureLoads = []; // pending override textures; Solid renders wait for them
function overrideTexture(url) {
  if (!textures.has(url)) {
    let t;
    textureLoads.push(new Promise((done) => { t = new THREE.TextureLoader().load(url, done, undefined, done); }));
    t.colorSpace = THREE.SRGBColorSpace;
    t.flipY = false; // glTF UV convention
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    textures.set(url, t);
  }
  return textures.get(url);
}

function solidMaterial(base, ent) {
  const ov = ent.override;
  const translucent = ent.placeholder || (ent.rendermode !== 0 && ent.color[3] < 255);
  const k = [base.uuid, ent.color.join(','), ent.rendermode !== 0, ent.placeholder, ov && (ov.texture || ov.avg.join(',')), ov?.wireframe].join('|');
  if (solidMats.has(k)) return solidMats.get(k);
  const m = base.clone();
  m.side = THREE.DoubleSide;
  m.envMapIntensity = ENV_INTENSITY;
  if (ent.placeholder) {
    m.map = null; m.color.set(0xff00ff);
  } else {
    if (ov) {
      m.map = ov.texture ? overrideTexture(ov.texture) : null;
      m.color.set(ov.texture ? 0xffffff : srgb(ov.avg));
      if (ov.wireframe) { m.map = null; m.color.set(0xffffff); m.wireframe = true; } // the "Wireframe" material: edges only
    }
    m.color.multiply(srgb(ent.color));
  }
  if (translucent) {
    m.transparent = true;
    m.opacity = ent.placeholder ? 0.45 : ent.color[3] / 255;
    m.depthWrite = false;
  }
  solidMats.set(k, m);
  return m;
}

function entityMatrix(ent) {
  if (ent.posed) return new THREE.Matrix4(); // ragdoll mesh is already in dupe space
  return partMatrix(ent);
}

function partMatrix(ent) { // the first physics object's transform, also for a ragdoll
  const r = ent.matrix;
  return new THREE.Matrix4().set( // row-major, translation inches -> metres
    r[0][0], r[0][1], r[0][2], r[0][3] * INCH,
    r[1][0], r[1][1], r[1][2], r[1][3] * INCH,
    r[2][0], r[2][1], r[2][2], r[2][3] * INCH,
    0, 0, 0, 1);
}

async function buildScene(scene) {
  clearScene();
  const urls = [...new Set(scene.entities.map((e) => e.glb))];
  let done = 0;
  setLoading(`Loading ${count(urls.length, 'model')}…`);
  await Promise.all(urls.map((u) => loadTemplate(u).then(() => {
    done += 1;
    setLoading(`Loading models… ${done}/${urls.length}`);
  })));
  for (const ent of scene.entities) {
    const parts = await loadTemplate(ent.glb);
    const g = new THREE.Group();
    g.matrixAutoUpdate = false;
    g.matrix.copy(entityMatrix(ent));
    g.userData.ent = ent;
    const lm = lineMaterial(ent);
    for (const p of parts) {
      const lines = new THREE.LineSegments(p.edges, lm);
      lines.userData.kind = 'line';
      const fill = new THREE.Mesh(p.geometry, hiddenMat);
      fill.userData.kind = 'fill';
      const solid = new THREE.Mesh(p.geometry, solidMaterial(p.material, ent));
      solid.userData.kind = 'solid';
      pickMeshes.push(solid); // raycast even while hidden in wireframe mode
      solid.castShadow = !ent.override?.wireframe; // a wireframe prop has no surface to shade
      solid.receiveShadow = true;
      g.add(lines, fill, solid);
    }
    root.add(g);
  }
  root.updateMatrixWorld(true);
  buildConstraints(scene);
  buildLinks(scene);
  buildGrid();
  applyMode();
  setLoading(null);
  const linkView = new URLSearchParams(location.search).get('view'); // deep link: ?view=front
  fit(VIEWS[linkView] ? linkView : 'iso');
}

function clearScene() {
  for (const g of [...root.children]) root.remove(g);
  pickMeshes.length = 0;
  for (const m of solidMats.values()) m.dispose();
  solidMats.clear();
  if (constraintLines) { world.remove(constraintLines); constraintLines.geometry.dispose(); constraintLines = null; }
  if (linkLines) { world.remove(linkLines); linkLines.geometry.dispose(); linkLines = null; }
  $('#tip').hidden = true;
}

// World point of a constraint end: its saved local position, else the middle of the prop (many models have their
// origin at a corner or the bottom). A ragdoll's mesh, and so its bounds, are already in dupe space.
function anchor(g, lpos) {
  const ent = g.userData.ent;
  const v = lpos ? new THREE.Vector3(lpos[0] * INCH, lpos[1] * INCH, lpos[2] * INCH) : new THREE.Vector3();
  if (!lpos && ent.bounds) {
    const [lo, hi] = ent.bounds;
    return v.set((lo[0] + hi[0]) / 2 * INCH, (lo[1] + hi[1]) / 2 * INCH, (lo[2] + hi[2]) / 2 * INCH).applyMatrix4(g.matrixWorld);
  }
  return v.applyMatrix4(partMatrix(ent)).applyMatrix4(root.matrixWorld);
}

function lineSegments(pos, col) {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  // drawn on top, like the 2D overlay on rendered images
  const lines = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ vertexColors: true, depthTest: false }));
  lines.renderOrder = 10;
  return lines;
}

function buildConstraints(scene) {
  const byId = new Map(root.children.map((g) => [String(g.userData.ent.id), g]));
  const pos = []; const col = [];
  for (const c of scene.constraints) {
    const pts = c.ends.map((e) => {
      const g = byId.get(String(e.entity));
      return g ? anchor(g, e.lpos) : null;
    }).filter(Boolean);
    const color = new THREE.Color(CONSTRAINT_COLORS[c.type] ?? 0xff00ff);
    for (let i = 0; i + 1 < pts.length; i++) {
      pos.push(...pts[i].toArray(), ...pts[i + 1].toArray());
      col.push(...color.toArray(), ...color.toArray());
    }
  }
  if (!pos.length) return;
  constraintLines = lineSegments(pos, col);
  constraintLines.visible = $('#show-constraints').checked;
  world.add(constraintLines);
}

function buildLinks(scene) {
  const byId = new Map(root.children.map((g) => [String(g.userData.ent.id), g]));
  const pos = []; const col = [];
  for (const l of scene.links || []) {
    const a = byId.get(String(l.from)); const b = byId.get(String(l.to));
    if (!a || !b) continue;
    const color = new THREE.Color(LINK_COLORS[l.on] ?? 0xffffff);
    pos.push(...anchor(a).toArray(), ...anchor(b).toArray());
    col.push(...color.toArray(), ...color.toArray());
  }
  if (!pos.length) return;
  linkLines = lineSegments(pos, col);
  linkLines.visible = $('#show-links').checked;
  world.add(linkLines);
}

// Keypads and fading doors grouped by key. A key with doors but no keypad in the dupe is pressed by something outside
// it (the owner's numpad, a button placed later) or by nothing; a keypad key with no door opens nothing in the dupe.
function linkGroups(scene) {
  const keys = new Map();
  const at = (k) => { if (!keys.has(k)) keys.set(k, { doors: [], granted: [], denied: [], press: [] }); return keys.get(k); };
  for (const e of scene.entities) {
    if (e.numpad?.door) at(e.numpad.door.key).doors.push(e);
    if (e.numpad?.trigger?.granted) at(e.numpad.trigger.granted).granted.push(e);
    if (e.numpad?.trigger?.denied) at(e.numpad.trigger.denied).denied.push(e);
    if (e.numpad?.button) at(e.numpad.button.key).press.push(e);
  }
  return [...keys].sort((a, b) => a[0] - b[0]);
}

function showLinks(scene) {
  const box = $('#links');
  box.replaceChildren();
  const groups = linkGroups(scene);
  if (!groups.length) return;
  const d = document.createElement('details'); d.className = 'links';
  const s = document.createElement('summary');
  const pressers = (g) => g.granted.length + g.denied.length + g.press.length;
  const orphans = groups.filter(([, g]) => !g.doors.length || !pressers(g)).length;
  s.textContent = `Buttons and doors: ${count(groups.length, 'key')}` + (orphans ? `, ${orphans} unpaired` : '');
  const ul = document.createElement('ul');
  for (const [key, g] of groups) {
    const li = document.createElement('li');
    const pads = pressers(g);
    const parts = [];
    if (g.granted.length) parts.push(`keypad (granted) ×${g.granted.length}`);
    if (g.denied.length) parts.push(`keypad (denied) ×${g.denied.length}`);
    if (g.press.length) parts.push(`button ×${g.press.length}`);
    if (g.doors.length) parts.push(`fading door ×${g.doors.length}`);
    li.textContent = `${keyName(key)}: ${parts.join(', ')}`;
    if (!g.doors.length || !pads) {
      li.className = 'orphan';
      li.textContent += pads ? ' — no door on this key' : ' — no button or keypad in the dupe';
    }
    ul.append(li);
  }
  d.append(s, ul); box.append(d);
}

// Hovering a keypad, a button or a fading door names its key and what it is linked to.
function tipText(ent) {
  const n = ent.numpad;
  if (!n) return '';
  const links = state.scene?.links || [];
  const lines = [];
  if (n.door) {
    const by = links.filter((l) => String(l.to) === String(ent.id)).length;
    lines.push(`Fading door · key ${keyName(n.door.key)}`,
      [n.door.toggle ? 'toggles' : 'open while held', n.door.reversed ? 'reversed' : ''].filter(Boolean).join(', '),
      by ? `opened by ${count(by, 'button/keypad', 'buttons/keypads')}` : 'no button or keypad on this key');
  }
  if (n.trigger) {
    for (const on of ['granted', 'denied']) {
      const k = n.trigger[on];
      if (!k) continue;
      const to = links.filter((l) => String(l.from) === String(ent.id) && l.on === on).length;
      lines.push(`Keypad · ${on === 'granted' ? 'access granted' : 'access denied'}: key ${keyName(k)} → ${count(to, 'door')}`);
    }
  }
  if (n.button) {
    const to = links.filter((l) => String(l.from) === String(ent.id) && l.on === 'press').length;
    lines.push(`Button${n.button.label ? ` "${n.button.label}"` : ''} · key ${keyName(n.button.key)} → ${count(to, 'door')}`,
      n.button.toggle ? 'toggles' : '');
  }
  return lines.filter(Boolean).join('\n');
}

// a hidden fading door must not answer the hover
function isShown(mesh) {
  const ent = mesh.parent.userData.ent;
  return !((ent.fading_door || ent.transparent) && state.fading === 'hidden');
}

let tipFrame = 0;
canvas.addEventListener('pointermove', (e) => {
  const tip = $('#tip');
  if (e.buttons || !state.scene?.links) { tip.hidden = true; return; }
  if (tipFrame) return;
  const { clientX, clientY } = e;
  tipFrame = requestAnimationFrame(() => { // one raycast per frame at most
    tipFrame = 0;
    const rect = canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    camera.updateMatrixWorld();
    raycaster.setFromCamera(ndc, camera);
    const hit = raycaster.intersectObjects(pickMeshes, false).find((h) => isShown(h.object));
    const text = hit ? tipText(hit.object.parent.userData.ent) : '';
    tip.hidden = !text;
    if (!text) return;
    tip.textContent = text;
    tip.style.left = `${Math.max(4, Math.min(clientX - rect.left + 14, rect.width - tip.offsetWidth - 4))}px`;
    tip.style.top = `${Math.max(4, Math.min(clientY - rect.top + 14, rect.height - tip.offsetHeight - 4))}px`;
  });
});
canvas.addEventListener('pointerleave', () => { $('#tip').hidden = true; });

function sceneBox() { return new THREE.Box3().setFromObject(root); }

function groundZ() { return (state.scene?.ground?.z ?? 0) * INCH; } // ground level stored in the dupe

function buildGrid() {
  if (grid) { world.remove(grid); grid.geometry.dispose(); }
  const box = sceneBox();
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const extent = Math.max(size.x, size.y) * 2.2;
  const step = 47.45 * INCH; // one PHX 1x1 plate
  const divisions = Math.max(4, Math.min(200, Math.round(extent / step)));
  grid = new THREE.GridHelper(divisions * step, divisions, 0x3a414c, 0x2a2f37);
  grid.rotateX(Math.PI / 2);
  const c = box.getCenter(new THREE.Vector3());
  grid.position.set(c.x, c.y, groundZ() - 0.002);
  world.add(grid);
  state.groundReady = applyGround(); // Solid renders wait for this
}

// Ground: 'grid' (viewer helper only), 'grass' / 'concrete' / 'dirt' (game textures), 'solid' colour or 'none'.
// Everything except the grid is also used for renders.
let groundMesh = null;
const groundInfo = new Map(); // kind -> Promise<{texture, tile, color}>
async function applyGround() {
  if (grid) grid.visible = state.ground === 'grid';
  if (groundMesh) { world.remove(groundMesh); groundMesh.geometry.dispose(); groundMesh.material.dispose(); groundMesh = null; }
  const g = state.scene?.ground;
  if (!g || !['grass', 'concrete', 'dirt', 'solid'].includes(state.ground)) return;
  const kind = state.ground;
  const size = g.size * INCH;
  const mat = new THREE.MeshStandardMaterial({ roughness: 1, envMapIntensity: ENV_INTENSITY, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 4 });
  if (kind === 'solid') {
    mat.color.set($('#ground-color').value);
  } else {
    if (!groundInfo.has(kind)) groundInfo.set(kind, api(`/api/ground/${kind}`));
    let info;
    try { info = await groundInfo.get(kind); } catch (e) { status(`Ground: ${e.message}`, 'error'); return; }
    if (state.ground !== kind) return; // changed while loading
    const tex = overrideTexture(info.texture);
    const t = tex.clone(); // own repeat per ground size; shares the image
    t.repeat.set(g.size / info.tile, g.size / info.tile);
    t.needsUpdate = true;
    mat.map = t;
  }
  const geo = new THREE.PlaneGeometry(size, size);
  groundMesh = new THREE.Mesh(geo, mat);
  groundMesh.receiveShadow = true; // the ground only receives
  groundMesh.position.set(g.center[0] * INCH, g.center[1] * INCH, groundZ() - 0.001);
  world.add(groundMesh); // not in root: framing and picking stay on the build
}

// Fading doors: 'solid' (closed, as in game), 'ghost' (see-through) or 'hidden'
const ghostMats = new Map();
function ghostMaterial(base) {
  if (!ghostMats.has(base.uuid)) {
    const m = base.clone();
    m.transparent = true;
    m.opacity = Math.min(base.opacity, base.isLineBasicMaterial ? 0.35 : 0.3); // never more visible than saved
    m.depthWrite = false;
    ghostMats.set(base.uuid, m);
  }
  return ghostMats.get(base.uuid);
}

function applyMode() {
  root.traverse((o) => {
    const k = o.userData.kind;
    if (!k) return;
    const pe = o.parent?.userData.ent;
    const door = pe && (pe.fading_door || pe.transparent) ? state.fading : 'solid';
    o.visible = door !== 'hidden' && ((k === 'line' && state.mode !== 'solid')
      || (k === 'fill' && state.mode === 'hidden' && door !== 'ghost') // a ghost must not hide what's behind it
      || (k === 'solid' && state.mode === 'solid'));
    if (k !== 'fill') {
      o.userData.baseMat ??= o.material;
      o.material = door === 'ghost' ? ghostMaterial(o.userData.baseMat) : o.userData.baseMat;
    }
    if (k === 'solid') o.castShadow = door !== 'ghost' && !o.userData.baseMat.transparent && !o.userData.baseMat.wireframe;
  });
  shadowsDirty();
  document.querySelectorAll('#mode button').forEach((b) => {
    const on = b.dataset.mode === state.mode;
    b.classList.toggle('active', on);
    b.setAttribute('aria-checked', on);
  });
}

// ---------------------------------------------------------------- camera
// Fit the build tightly: for the view direction, solve for the camera position at which every corner
// of every prop's box lies inside the frustum with FIT_MARGIN to spare (exact, not a bounding sphere).
const FIT_MARGIN = 0.03;
function fitPoints() {
  const pts = [];
  root.updateMatrixWorld(true);
  root.traverse((o) => {
    if (o.userData.kind !== 'solid') return;
    const g = o.geometry;
    if (!g.boundingBox) g.computeBoundingBox();
    const { min, max } = g.boundingBox;
    for (const x of [min.x, max.x]) for (const y of [min.y, max.y]) for (const z of [min.z, max.z]) {
      pts.push(new THREE.Vector3(x, y, z).applyMatrix4(o.matrixWorld));
    }
  });
  return pts;
}

function fit(view) {
  const box = sceneBox();
  if (box.isEmpty()) return;
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  sceneSphere.copy(sphere);
  updateSun();
  let back; // unit vector from the build towards the camera
  if (view && VIEWS[view]) {
    const [yaw, pitch] = VIEWS[view].map(THREE.MathUtils.degToRad);
    back = new THREE.Vector3(Math.cos(pitch) * Math.cos(yaw), Math.cos(pitch) * Math.sin(yaw), Math.sin(pitch));
  } else {
    back = camera.position.clone().sub(controls.target).normalize();
  }
  const right = new THREE.Vector3().crossVectors(camera.up, back);
  if (right.lengthSq() < 1e-8) right.set(1, 0, 0); // looking straight down
  right.normalize();
  const up = new THREE.Vector3().crossVectors(back, right).normalize();
  const ty = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * (1 - FIT_MARGIN);
  const tx = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * camera.aspect * (1 - FIT_MARGIN);
  // camera at (cx, cy, cz) in (right, up, back) coordinates sees p when |x - cx| <= tx (cz - z), same for y
  let ax = -Infinity, bx = -Infinity, ay = -Infinity, by = -Infinity, zmin = Infinity, zmax = -Infinity;
  for (const p of fitPoints()) {
    const x = p.dot(right), y = p.dot(up), z = p.dot(back);
    ax = Math.max(ax, z + x / tx); bx = Math.max(bx, z - x / tx);
    ay = Math.max(ay, z + y / ty); by = Math.max(by, z - y / ty);
    zmin = Math.min(zmin, z); zmax = Math.max(zmax, z);
  }
  const cx = (tx * (ax - bx)) / 2, cy = (ty * (ay - by)) / 2;
  const cz = Math.max((ax + bx) / 2, (ay + by) / 2);
  const zc = (zmin + zmax) / 2; // orbit around the middle of the build's depth
  camera.position.copy(right).multiplyScalar(cx).addScaledVector(up, cy).addScaledVector(back, cz);
  controls.target.copy(right).multiplyScalar(cx).addScaledVector(up, cy).addScaledVector(back, zc);
  const dist = cz - zc;
  controls.minDistance = sphere.radius * 0.005; // below the wheel handler's minimum target depth
  controls.maxDistance = Math.max(dist, sphere.radius) * 8;
  camera.updateProjectionMatrix();
  controls.update();
  updateClipping();
}

const DEFAULT_FOV = 40;
function resetView() {
  camera.fov = DEFAULT_FOV;
  $('#fov').value = DEFAULT_FOV;
  $('#fov-out').value = `${DEFAULT_FOV}°`;
  camera.updateProjectionMatrix();
  saveSettings();
  fit('iso');
}

// ---------------------------------------------------------------- loop
// Keyboard movement: held keys move camera and orbit target together (the view direction doesn't change).
// Speed scales with the build so it feels the same for a single prop and a whole base.
const MOVE_KEYS = {
  ArrowUp: [0, 0, 1], ArrowDown: [0, 0, -1], ArrowLeft: [-1, 0, 0], ArrowRight: [1, 0, 0],
  PageUp: [0, 1, 0], PageDown: [0, -1, 0],
}; // [right, world up, forward]
const heldKeys = new Set();
let fastMove = false;
let lastFrame = performance.now();

function moveCamera(dt) {
  if (!heldKeys.size) return;
  const v = [0, 0, 0];
  for (const k of heldKeys) MOVE_KEYS[k].forEach((x, i) => { v[i] += x; });
  if (!v[0] && !v[1] && !v[2]) return;
  const fwd = camera.getWorldDirection(new THREE.Vector3());
  const right = new THREE.Vector3().crossVectors(fwd, camera.up).normalize();
  const step = new THREE.Vector3()
    .addScaledVector(right, v[0]).addScaledVector(camera.up, v[1]).addScaledVector(fwd, v[2])
    .normalize().multiplyScalar(sceneSphere.radius * 0.5 * (fastMove ? 3 : 1) * dt);
  camera.position.add(step);
  controls.target.add(step);
}

// Orbit center marker: a blue, semi-transparent sphere at the point the view rotates around, shown while
// the view moves (orbit, pan, zoom, arrow keys, fit/reset) and faded out shortly after. Drawn on top of
// the build (the center is usually inside it) at a constant on-screen size; never part of renders.
const PIVOT_OPACITY = 0.45;
const PIVOT_LINGER_MS = 700;
const pivot = new THREE.Mesh(
  new THREE.SphereGeometry(1, 24, 16),
  new THREE.MeshBasicMaterial({ color: 0x3b82f6, transparent: true, opacity: 0, depthTest: false, depthWrite: false }));
pivot.renderOrder = 20;
pivot.visible = false;
world.add(pivot);
let pivotUntil = 0;
let dragging = false;
controls.addEventListener('change', () => { pivotUntil = performance.now() + PIVOT_LINGER_MS; });
controls.addEventListener('start', () => { dragging = true; });
controls.addEventListener('end', () => { dragging = false; pivotUntil = performance.now() + PIVOT_LINGER_MS; });

function updatePivot(now, dt) {
  const want = state.scene && (dragging || now < pivotUntil) ? PIVOT_OPACITY : 0;
  const m = pivot.material;
  m.opacity += (want - m.opacity) * Math.min(1, dt * (want ? 20 : 6)); // quick fade in, gentler fade out
  pivot.visible = m.opacity > 0.01;
  if (!pivot.visible) return;
  pivot.position.copy(controls.target);
  const dist = camera.position.distanceTo(controls.target);
  pivot.scale.setScalar(dist * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * 0.025); // ~1.25% of view height
}

function frame() {
  const now = performance.now();
  const dt = Math.min((now - lastFrame) / 1000, 0.1); // cap dt so a background tab doesn't teleport
  moveCamera(dt);
  lastFrame = now;
  controls.update();
  updatePivot(now, dt);
  updateClipping();
  renderer.render(world, camera);
  requestAnimationFrame(frame);
}
resize();
requestAnimationFrame(frame);

// ---------------------------------------------------------------- server calls
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const isJson = (res.headers.get('content-type') || '').includes('json');
  const body = isJson ? await res.json() : null;
  if (!res.ok) throw new Error((body && body.error) || `${res.status} ${res.statusText}`);
  return body;
}

function setLoading(text) {
  $('#loading').hidden = !text;
  if (text) { $('#loading-text').textContent = text; $('#empty').hidden = true; }
}

function status(text, kind = '') {
  const el = $('#render-status');
  el.textContent = text;
  el.className = `status small ${kind || 'muted'}`;
}

async function openDupe(promise, name) {
  try {
    setLoading('Decoding and converting models…');
    const { id, scene } = await promise;
    state.sceneId = id; state.scene = scene; state.name = scene.name || name;
    showInfo(scene);
    await buildScene(scene);
    updateRenderButton();
    status(softwareMode ? 'Rendering on the CPU (no GPU found): large builds will be slower.' : '');
  } catch (e) {
    setLoading(null);
    if (!state.scene) $('#empty').hidden = false;
    status(`Could not load ${name}: ${e.message}`, 'error');
  }
}

function openFile(file) {
  if (!file) return;
  const p = file.arrayBuffer().then((buf) => api(`/api/scene?name=${encodeURIComponent(file.name)}`, {
    method: 'POST', body: buf, headers: { 'Content-Type': 'application/octet-stream' },
  }));
  openDupe(p, file.name);
}

function showInfo(scene) {
  const doors = scene.entities.filter((e) => e.fading_door).length;
  const clear = scene.entities.filter((e) => e.transparent && !e.fading_door).length;
  $('#fading-count').value = doors + clear ? `${doors + clear}` : '';
  $('#fading-count').title = `In this dupe: ${count(doors, 'fading door')}, ${count(clear, 'other transparent prop')}`;
  const i = scene.info || {};
  const rows = [
    ['Name', scene.name], ['Author', i.name], ['Saved', [i.date, i.time].filter(Boolean).join(' ')],
    ['Entities', scene.entities.length], ['Constraints', scene.constraints.length],
    ['Format', i.revision === undefined ? '' : `AD2 rev ${i.revision}`],
  ];
  const dl = $('#info');
  dl.replaceChildren(...rows.filter((r) => r[1] !== undefined && r[1] !== '').flatMap(([k, v]) => {
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd'); dd.textContent = v;
    return [dt, dd];
  }));
  dl.hidden = false;
  const w = $('#warnings');
  w.replaceChildren();
  const rep = scene.report || {};
  for (const [label, items] of [['missing model', rep.missing_models], ['missing material', rep.missing_materials]]) {
    if (!items || !items.length) continue;
    const d = document.createElement('details'); d.className = 'warn';
    const s = document.createElement('summary'); s.textContent = count(items.length, label);
    const ul = document.createElement('ul');
    for (const it of items) { const li = document.createElement('li'); li.textContent = it; ul.append(li); }
    d.append(s, ul); w.append(d);
  }
  showLinks(scene);
}

// ---------------------------------------------------------------- rendering
function cameraState() {
  camera.updateMatrixWorld(true);
  const e = camera.matrixWorld.elements; // column-major
  const rows = [0, 1, 2, 3].map((i) => [e[i], e[4 + i], e[8 + i], e[12 + i]]);
  return { matrix: rows, fov: camera.fov, position: camera.position.toArray(), target: controls.target.toArray() };
}

function updateRenderButton() {
  $('#render-btn').disabled = !state.sceneId || state.busy;
}

// Draw the current scene at an exact size and return it as a data URL. This is the viewer's own Solid view, drawn
// by the GPU (or the CPU fallback) and read back immediately: no animation frame or timer is involved, so it also
// works in a hidden frame or a background tab. Shadows are on for renders even when the live view has them off.
async function captureView(width, height, { transparent = false, background = '#2b2f36', constraints = false,
  mime = 'image/png', quality = 0.92 } = {}) {
  const keep = {
    mode: state.mode, background: world.background, grid: grid?.visible, constraints: constraintLines?.visible,
    links: linkLines?.visible, shadows: sun.castShadow,
  };
  await state.groundReady; // ground texture info, then all textures
  await Promise.all(textureLoads); // otherwise props whose texture is still downloading come out black
  try {
    state.mode = 'solid';
    applyMode();
    sun.castShadow = true;
    shadowsDirty();
    if (grid) grid.visible = false;
    pivot.visible = false; // the next frame decides again
    if (constraintLines) constraintLines.visible = constraints;
    if (linkLines) linkLines.visible = constraints; // keypad -> door lines go with the constraint overlay
    world.background = transparent ? null : new THREE.Color(background);
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(1);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    updateClipping();
    renderer.render(world, camera);
    return canvas.toDataURL(mime, quality); // same task as render(), so the buffer is still intact
  } finally {
    state.mode = keep.mode;
    applyMode();
    sun.castShadow = keep.shadows;
    shadowsDirty();
    world.background = keep.background;
    if (grid) grid.visible = keep.grid;
    if (constraintLines) constraintLines.visible = keep.constraints;
    if (linkLines) linkLines.visible = keep.links;
    renderer.setPixelRatio(livePixelRatio());
    resize(); // restores the canvas size and camera aspect
  }
}

async function renderSolid(width, height) {
  const dataUrl = await captureView(width, height, {
    transparent: $('#bg-transparent').checked, background: $('#bg-color').value,
    constraints: $('#render-constraints').checked,
  });
  return URL.createObjectURL(await (await fetch(dataUrl)).blob());
}

async function doRender() {
  if (!state.sceneId || state.busy) return;
  const [width, height] = renderSize();
  const cam = cameraState();
  const t = performance.now();
  state.busy = true;
  updateRenderButton();
  try {
    const url = await renderSolid(width, height);
    const seconds = ((performance.now() - t) / 1000).toFixed(2);
    status(`Done: ${width}×${height} in ${seconds} s`, 'ok');
    addResult({ id: null, url, width, height, seconds, camera: cam, engine: 'solid', name: state.name });
  } catch (e) {
    status(`Render failed: ${e.message}`, 'error');
  } finally {
    state.busy = false;
    updateRenderButton();
  }
}

const results = [];
function addResult(r) {
  results.unshift(r);
  $('#results-section').hidden = false;
  const li = document.createElement('li');
  const b = document.createElement('button');
  b.className = 'checker';
  b.title = 'Open render';
  const img = document.createElement('img'); img.src = r.url; img.alt = `Render: ${r.name}`;
  const meta = document.createElement('span'); meta.className = 'meta';
  meta.textContent = `${r.width}×${r.height} · ${r.seconds} s`;
  b.append(img, meta);
  b.addEventListener('click', () => openLightbox(r));
  li.append(b);
  $('#results').prepend(li);
  openLightbox(r);
}

function clearResults() {
  if (!results.length) return;
  if (!window.confirm(`Remove ${count(results.length, 'render')}? Files you already downloaded are not affected.`)) return;
  for (const r of results) URL.revokeObjectURL(r.url); // renders live only in this browser
  results.length = 0;
  $('#results').replaceChildren();
  $('#results-section').hidden = true;
  $('#overlay').hidden = true; // the overlay may show one of the deleted images
  $('#overlay-bar').hidden = true;
}

let lightboxResult = null;
function openLightbox(r) {
  lightboxResult = r;
  $('#lb-img').src = r.url;
  $('#lb-title').textContent = `${r.name} · ${r.width}×${r.height}`;
  const a = $('#lb-download');
  a.href = r.url;
  a.download = `${r.name}_${r.width}x${r.height}.png`;
  $('#lightbox').showModal();
}

function restoreView(r) {
  camera.fov = r.camera.fov;
  $('#fov').value = Math.round(r.camera.fov);
  $('#fov-out').value = `${Math.round(r.camera.fov)}°`;
  camera.position.fromArray(r.camera.position);
  controls.target.fromArray(r.camera.target);
  camera.updateProjectionMatrix();
  controls.update();
}

function showOverlay(r) {
  restoreView(r);
  const o = $('#overlay');
  o.src = r.url;
  o.hidden = false;
  o.style.opacity = $('#overlay-opacity').value / 100;
  $('#overlay-bar').hidden = false;
}

// ---------------------------------------------------------------- UI wiring
$('#file-input').addEventListener('change', (e) => { openFile(e.target.files[0]); e.target.value = ''; });

const vp = $('#viewport');
let dragDepth = 0;
vp.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth += 1; $('#drop-zone').hidden = false; });
vp.addEventListener('dragover', (e) => e.preventDefault());
vp.addEventListener('dragleave', () => { dragDepth -= 1; if (dragDepth <= 0) { dragDepth = 0; $('#drop-zone').hidden = true; } });
vp.addEventListener('drop', (e) => {
  e.preventDefault(); dragDepth = 0; $('#drop-zone').hidden = true;
  openFile(e.dataTransfer.files[0]);
});

$('#mode').addEventListener('click', (e) => {
  const m = e.target.closest('button')?.dataset.mode;
  if (m) { state.mode = m; applyMode(); saveSettings(); }
});
document.querySelector('.presets').addEventListener('click', (e) => {
  const v = e.target.closest('button')?.dataset.view;
  if (v === 'reset') resetView();
  else if (v) fit(v === 'fit' ? null : v);
});
$('#fov').addEventListener('input', (e) => {
  camera.fov = Number(e.target.value);
  $('#fov-out').value = `${e.target.value}°`;
  camera.updateProjectionMatrix();
  saveSettings();
});
$('#show-constraints').addEventListener('change', (e) => { if (constraintLines) constraintLines.visible = e.target.checked; });
$('#show-links').addEventListener('change', (e) => { if (linkLines) linkLines.visible = e.target.checked; });
function setGround(kind) {
  state.ground = kind;
  $('#ground').value = kind;
  $('#ground-color').hidden = kind !== 'solid';
  state.groundReady = applyGround();
}
$('#ground').addEventListener('change', (e) => { setGround(e.target.value); saveSettings(); });
$('#ground-color').addEventListener('input', () => { if (state.ground === 'solid') state.groundReady = applyGround(); saveSettings(); });
function setShadows(on) {
  sun.castShadow = on;
  $('#shadows').checked = on;
  shadowsDirty();
}
$('#shadows').addEventListener('change', (e) => { setShadows(e.target.checked); saveSettings(); });
$('#fading-doors').addEventListener('change', (e) => { state.fading = e.target.value; applyMode(); saveSettings(); });
for (const id of ['#bg-transparent', '#bg-color']) $(id).addEventListener('change', saveSettings);
$('#width').addEventListener('input', () => { updateHeight(); saveSettings(); });
$('#width').addEventListener('change', () => { normalizeWidth(); saveSettings(); }); // typed 100? show the 480 that is used
$('#bg-transparent').addEventListener('change', (e) => { $('#bg-color').disabled = e.target.checked; });
$('#render-btn').addEventListener('click', doRender);
$('#clear-results').addEventListener('click', clearResults);

$('#lb-restore').addEventListener('click', () => { restoreView(lightboxResult); $('#lightbox').close(); });
$('#lb-overlay').addEventListener('click', () => { showOverlay(lightboxResult); $('#lightbox').close(); });
$('#overlay-opacity').addEventListener('input', (e) => { $('#overlay').style.opacity = e.target.value / 100; });
$('#overlay-close').addEventListener('click', () => { $('#overlay').hidden = true; $('#overlay-bar').hidden = true; });

window.addEventListener('keydown', (e) => {
  fastMove = e.shiftKey;
  const typing = e.target instanceof Element && e.target.closest('input, select, textarea');
  if (e.ctrlKey || e.metaKey || e.altKey || $('#lightbox').open) return;
  // A focused slider would take Home itself (jumping FOV to its minimum), so Home resets the view even there.
  if (e.key === 'Home' && e.target instanceof HTMLInputElement && e.target.type === 'range') {
    e.preventDefault(); resetView(); return;
  }
  if (typing) return;
  if (MOVE_KEYS[e.key]) {
    e.preventDefault(); // don't scroll the sidebar
    if (state.scene) heldKeys.add(e.key);
  } else if (e.key === 'f' || e.key === 'F') fit(null);
  else if (e.key === 'Home') { e.preventDefault(); resetView(); }
  else if ((e.key === 'r' || e.key === 'R') && !$('#render-btn').disabled) doRender();
});
window.addEventListener('keyup', (e) => {
  fastMove = e.shiftKey;
  heldKeys.delete(e.key);
});
window.addEventListener('blur', () => heldKeys.clear()); // a key released in another window must not stick

// ---------------------------------------------------------------- startup
(async function init() {
  const s = loadSettings();
  try {
    const cfg = await api('/api/config');
    if (cfg.files) {
      const { files } = await api('/api/files');
      const list = $('#file-list');
      const render = (q) => list.replaceChildren(...files.filter((f) => f.toLowerCase().includes(q)).slice(0, 500).map((f) => {
        const li = document.createElement('li');
        li.textContent = f; li.title = f; li.tabIndex = 0;
        const open = () => {
          list.querySelectorAll('.active').forEach((x) => x.classList.remove('active'));
          li.classList.add('active');
          openDupe(api(`/api/scene?file=${encodeURIComponent(f)}`, { method: 'POST' }), f);
        };
        li.addEventListener('click', open);
        li.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
        return li;
      }));
      render('');
      $('#file-filter').addEventListener('input', (e) => render(e.target.value.toLowerCase()));
      $('#file-list-wrap').hidden = files.length === 0;
      // deep link: /?file=sub/dir/dupe.txt opens a server dupe directly
      const want = new URLSearchParams(location.search).get('file');
      if (want && files.includes(want)) {
        [...list.children].find((li) => li.textContent === want)?.click();
      }
    }
  } catch (e) {
    status(`Server unavailable: ${e.message}`, 'error');
  }
  if (s.width) $('#width').value = clampRender(Number(s.width) || 1280);
  if (s.bg) $('#bg-color').value = s.bg;
  $('#bg-transparent').checked = !!s.bgTransparent;
  $('#bg-color').disabled = !!s.bgTransparent;
  const linkMode = new URLSearchParams(location.search).get('mode');
  const mode = ['wire', 'hidden', 'solid'].includes(linkMode) ? linkMode : s.mode;
  if (/^#[0-9a-f]{6}$/i.test(s.groundColor || '')) $('#ground-color').value = s.groundColor;
  const ground = [new URLSearchParams(location.search).get('ground'), s.ground]
    .find((g) => ['grid', 'grass', 'concrete', 'dirt', 'solid', 'none'].includes(g));
  if (ground) setGround(ground);
  const wantShadows = new URLSearchParams(location.search).get('shadows');
  // ?shadows=0 wins over the saved setting; with no saved choice the CPU path starts with live shadows off
  setShadows(wantShadows !== null ? wantShadows !== '0' : (s.shadows ?? !softwareMode));
  const fading = [new URLSearchParams(location.search).get('fading'), s.fading]
    .find((f) => ['solid', 'ghost', 'hidden'].includes(f)); // deep link ?fading=ghost wins over the saved setting
  if (fading) { state.fading = fading; $('#fading-doors').value = fading; }
  if (mode) { state.mode = mode; applyMode(); }
  if (s.fov) { $('#fov').value = s.fov; $('#fov-out').value = `${s.fov}°`; camera.fov = Number(s.fov); camera.updateProjectionMatrix(); }
  updateHeight();
  updateRenderButton();
})();

// ---------------------------------------------------------------- no WebGL at all
// Function declarations are hoisted, so this runs even though the module stops right after the first attempt.
function showNoWebGL() {
  document.body.classList.add('nowebgl');
  $('#loading').hidden = true;
  $('#empty').hidden = false;
  $('#empty strong').textContent = '3D is not available in this browser (no WebGL, neither on the GPU nor in software).';
  $('#empty .muted').textContent = '';
}
