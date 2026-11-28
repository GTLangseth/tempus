// The 3D tree, drawn in Neon Notebook style: concepts are paper beads with an ink outline,
// branches are ink pen strokes, series are small neon seeds colored by frequency, and the
// only other neon is the search trail. The renderer is transparent so the page's graph
// paper shows through. Everything heavy is instanced, so ~2,000 seeds cost four draw calls.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { ancestors, subtree } from "./layout.js";

const V = (a) => new THREE.Vector3(a[0], a[1], a[2]);
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
const OVERVIEW = { pos: [0, 330, 1180], target: [0, 0, 0] };

export class TreeScene {
  constructor(el, { nodes, leaves, children, root }, theme, handlers) {
    this.el = el;
    this.nodes = nodes;
    this.leaves = leaves;
    this.children = children;
    this.root = root;
    this.theme = theme;
    this.handlers = handlers; // {hover(item|null, x, y), click(item), labels() → [ids]}
    this.loadColors(theme);
    this.focusId = undefined;
    this.trail = [];
    this.matches = new Set();
    this.concepts = [...nodes.values()].filter((n) => n.id !== null);
    this.build();
    this.listen();
    this.setFocus(null, { fly: false });
    this.camera.position.copy(V(OVERVIEW.pos));
    this.loop = this.loop.bind(this);
    requestAnimationFrame(this.loop);
  }

  loadColors(theme) {
    this.c = Object.fromEntries(
      Object.entries(theme.color)
        .filter(([, v]) => typeof v === "string" && v.startsWith("#"))
        .map(([k, v]) => [k, new THREE.Color(v)])
    );
    this.freqColor = {
      D: new THREE.Color(theme.color["neon-cyan"]), W: new THREE.Color(theme.color["neon-violet"]),
      M: new THREE.Color(theme.color.primary), Q: new THREE.Color(theme.color["neon-orange"]),
      Y: new THREE.Color(theme.color["neon-green"]), irregular: new THREE.Color(theme.color.pencil),
    };
  }

  setTheme(theme) {
    this.theme = theme;
    this.loadColors(theme);
    this.setTrail(this.trail, [...this.matches]);
  }

  build() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h);
    this.renderer.setClearColor(0x000000, 0);
    this.el.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(42, w / h, 1, 6000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    Object.assign(this.controls, { enableDamping: true, dampingFactor: 0.08, rotateSpeed: 0.6,
      zoomSpeed: 0.9, minDistance: 25, maxDistance: 2600, autoRotate: true, autoRotateSpeed: 0.35 });
    const dummy = new THREE.Object3D();

    // Concept beads: paper fill plus a slightly larger back-face shell in ink (the outline).
    const sphere = new THREE.SphereGeometry(1, 28, 18);
    const n = this.concepts.length;
    this.beads = new THREE.InstancedMesh(sphere, new THREE.MeshBasicMaterial(), n);
    this.beadEdge = new THREE.InstancedMesh(sphere, new THREE.MeshBasicMaterial({ side: THREE.BackSide }), n);
    this.concepts.forEach((node, i) => {
      node.index = i;
      dummy.position.copy(V(node.pos));
      dummy.scale.setScalar(node.size);
      dummy.updateMatrix();
      this.beads.setMatrixAt(i, dummy.matrix);
      dummy.scale.setScalar(node.size + 0.9 + node.size * 0.04);
      dummy.updateMatrix();
      this.beadEdge.setMatrixAt(i, dummy.matrix);
    });
    this.scene.add(this.beadEdge, this.beads);

    // Seeds: one per series.
    const small = new THREE.SphereGeometry(1, 10, 8);
    const m = this.leaves.length;
    this.seeds = new THREE.InstancedMesh(small, new THREE.MeshBasicMaterial(), Math.max(m, 1));
    this.seedEdge = new THREE.InstancedMesh(small, new THREE.MeshBasicMaterial({ side: THREE.BackSide }), Math.max(m, 1));
    this.seeds.count = this.seedEdge.count = m;
    this.scene.add(this.seedEdge, this.seeds);
    this.placeSeeds();

    // Branches: curved pen strokes between concepts; hairlines from each concept to its seeds.
    const branchPts = [];
    this.branchOf = []; // per segment: the child concept id
    for (const node of this.nodes.values()) {
      if (node.id === null) continue;
      const parent = this.nodes.get(node.parent);
      const a = V(parent.pos), b = V(node.pos);
      const mid = a.clone().add(b).multiplyScalar(0.5);
      const ctrl = mid.clone().normalize().multiplyScalar(mid.length() * 1.12 + 6);
      const curve = new THREE.QuadraticBezierCurve3(a, parent.id === null ? mid : ctrl, b);
      const pts = curve.getPoints(14);
      for (let i = 0; i < pts.length - 1; i++) {
        branchPts.push(pts[i], pts[i + 1]);
        this.branchOf.push(node.id);
      }
      node.curve = curve;
    }
    this.branches = this.lines(branchPts);
    const hairPts = [];
    for (const leaf of this.leaves) {
      const owner = this.nodes.get(leaf.concept);
      hairPts.push(V(owner.puff ? owner.pos : [0, 0, 0]), V(leaf.pos));
    }
    this.hairs = this.lines(hairPts);
    this.scene.add(this.branches, this.hairs);

    this.trailGroup = new THREE.Group();
    this.scene.add(this.trailGroup);
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
  }

  lines(points) {
    const geo = new THREE.BufferGeometry().setFromPoints(points.length ? points : [new THREE.Vector3()]);
    geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(Math.max(points.length, 1) * 3), 3));
    return new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ vertexColors: true }));
  }

  placeSeeds() {
    const dummy = new THREE.Object3D();
    for (const leaf of this.leaves) {
      const big = this.matches.has(`${leaf.series.source}:${leaf.series.id}`);
      const r = big ? 2.5 : leaf.series.synced ? 1.9 : 1.45;
      dummy.position.copy(V(leaf.pos));
      dummy.scale.setScalar(r);
      dummy.updateMatrix();
      this.seeds.setMatrixAt(leaf.index, dummy.matrix);
      dummy.scale.setScalar(r + (big ? 0.9 : 0.55));
      dummy.updateMatrix();
      this.seedEdge.setMatrixAt(leaf.index, dummy.matrix);
    }
    this.seeds.instanceMatrix.needsUpdate = this.seedEdge.instanceMatrix.needsUpdate = true;
    this.seeds.computeBoundingSphere();
  }

  // --- state ---------------------------------------------------------------------------

  setFocus(id, { fly = true } = {}) {
    this.focusId = id;
    const lit = id ? subtree(this.children, id) : null;
    const path = new Set(id ? ancestors(this.nodes, id) : []);
    const { c } = this;
    const faded = (col, amt = 0.78) => col.clone().lerp(c.canvas, amt);
    const trailSet = new Set(this.trail);
    for (const node of this.concepts) {
      const on = !lit || lit.has(node.id) || path.has(node.id);
      const hot = trailSet.has(node.id) || node.id === id;
      this.beads.setColorAt(node.index, on || hot ? c.paper : faded(c.paper, 0.5));
      this.beadEdge.setColorAt(node.index, trailSet.has(node.id) ? c.primary : on ? c.ink : faded(c.ink));
    }
    for (const leaf of this.leaves) {
      const on = !lit || lit.has(leaf.concept);
      const col = this.freqColor[leaf.series.freq] ?? this.freqColor.irregular;
      this.seeds.setColorAt(leaf.index, on ? col : faded(col, 0.85));
      this.seedEdge.setColorAt(leaf.index, on ? (leaf.series.synced ? c.ink : col.clone().lerp(c.ink, 0.35)) : faded(c.ink, 0.88));
    }
    for (const mesh of [this.beads, this.beadEdge, this.seeds, this.seedEdge])
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    this.paintLines(this.branches, (i) => {
      const k = this.branchOf[Math.floor(i / 2)];
      if (trailSet.has(k)) return c.ink;
      return !lit || lit.has(k) ? c.graphite : faded(c.graphite, 0.8);
    });
    this.paintLines(this.hairs, (i) => {
      const leaf = this.leaves[Math.floor(i / 2)];
      return !lit || lit.has(leaf?.concept) ? c.hairline : faded(c.hairline, 0.7);
    });
    this.controls.autoRotate = !id && !this.trail.length;
    if (fly) this.flyTo(id);
  }

  paintLines(lines, colorAt) {
    const attr = lines.geometry.getAttribute("color");
    for (let i = 0; i < attr.count; i++) {
      const col = colorAt(i);
      attr.setXYZ(i, col.r, col.g, col.b);
    }
    attr.needsUpdate = true;
  }

  // The search trail: thick pink tubes along each branch on the route, and big seeds for
  // the ranked series. Pink is the page's one signal color.
  setTrail(conceptIds, matchRefs) {
    this.trailGroup.clear();
    this.trail = conceptIds;
    this.matches = new Set(matchRefs);
    const mat = new THREE.MeshBasicMaterial({ color: this.c.primary });
    for (const id of conceptIds) {
      const node = this.nodes.get(id);
      if (!node?.curve) continue;
      this.trailGroup.add(new THREE.Mesh(new THREE.TubeGeometry(node.curve, 20, 1.15, 6), mat));
    }
    this.placeSeeds();
    this.setFocus(this.focusId, { fly: false });
  }

  // The smallest sphere around a set of concepts' subtrees (and, for a route, its nodes).
  bounds(ids, { whole = true } = {}) {
    const keep = new Set();
    for (const id of ids) for (const k of whole ? subtree(this.children, id) : [id]) keep.add(k);
    const pts = [...keep].map((k) => V(this.nodes.get(k).pos));
    for (const leaf of this.leaves) if (keep.has(leaf.concept)) pts.push(V(leaf.pos));
    const sphere = new THREE.Box3().setFromPoints(pts).getBoundingSphere(new THREE.Sphere());
    sphere.radius = Math.max(sphere.radius, 70);
    return sphere;
  }

  view(id) {
    if (id === null || id === undefined) return { pos: V(OVERVIEW.pos), target: V(OVERVIEW.target) };
    return this.frame(this.bounds([id]));
  }

  frame(s) {
    if (!s) return { pos: V(OVERVIEW.pos), target: V(OVERVIEW.target) };
    const out = s.center.clone().normalize();
    const up = new THREE.Vector3(0, 1, 0).multiplyScalar(0.35);
    const dist = s.radius / Math.sin((this.camera.fov * Math.PI) / 360) * 1.02;
    return { pos: s.center.clone().add(out.add(up).normalize().multiplyScalar(dist)), target: s.center };
  }

  flyTo(id, ms = 1100) {
    const { pos, target } = this.view(id);
    return this.tween([{ pos, target }], ms);
  }

  // Fly through a route one stop at a time (the search trail), then pull back to frame the
  // whole route with what it reached.
  flyAlong(ids, msPerStop = 650) {
    if (!ids.length) return this.tween([this.view(null)], msPerStop);
    const stops = ids.slice(0, -1).map((id) => this.view(id));
    const route = this.bounds(ids, { whole: false });
    const end = this.bounds([ids.at(-1)]);
    const both = new THREE.Sphere().copy(route).union(end);
    stops.push(this.frame(both.radius < 260 ? both : end));
    return this.tween(stops, msPerStop);
  }

  tween(stops, msPerStop) {
    this.controls.autoRotate = false;
    const token = (this.flight = Symbol("flight"));
    return new Promise((resolve) => {
      const run = (k) => {
        if (k >= stops.length || this.flight !== token) return resolve();
        const p0 = this.camera.position.clone(), t0 = this.controls.target.clone();
        const { pos, target } = stops[k];
        const start = performance.now();
        const step = (now) => {
          if (this.flight !== token) return resolve();
          const t = Math.min(1, (now - start) / msPerStop), e = ease(t);
          this.camera.position.lerpVectors(p0, pos, e);
          this.controls.target.lerpVectors(t0, target, e);
          if (t < 1) requestAnimationFrame(step); else run(k + 1);
        };
        requestAnimationFrame(step);
      };
      run(0);
    });
  }

  // --- input ---------------------------------------------------------------------------

  listen() {
    const dom = this.renderer.domElement;
    let down = null;
    dom.addEventListener("pointerdown", (e) => {
      down = [e.clientX, e.clientY];
      this.flight = null; // grabbing the scene cancels a flight
    });
    dom.addEventListener("pointerup", (e) => {
      if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 5) {
        const item = this.pick(e);
        this.handlers.click(item);
      }
      down = null;
    });
    dom.addEventListener("pointermove", (e) => {
      if (down) return;
      this.hoverEvent = e;
    });
    dom.addEventListener("pointerleave", () => { this.hoverEvent = null; this.handlers.hover(null); });
    this.resizer = new ResizeObserver(() => this.resize());
    this.resizer.observe(this.el);
  }

  pick(e) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects([this.beads, this.seeds], false);
    const hit = hits[0];
    if (!hit) return null;
    if (hit.object === this.beads) return { kind: "concept", node: this.concepts[hit.instanceId] };
    return { kind: "series", leaf: this.leaves[hit.instanceId] };
  }

  // Shift the picture right by `px` so the tree centers in the space beside the side panel.
  setShift(px) {
    this.shift = px;
    this.resize();
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    if (this.shift) this.camera.setViewOffset(w, h, -this.shift, 0, w, h);
    else this.camera.clearViewOffset();
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  project(pos) {
    const v = V(pos).project(this.camera);
    const w = this.el.clientWidth, h = this.el.clientHeight;
    return { x: (v.x + 1) / 2 * w, y: (1 - v.y) / 2 * h, visible: v.z < 1 && v.z > -1, depth: v.z };
  }

  distance(pos) {
    return this.camera.position.distanceTo(V(pos));
  }

  loop() {
    if (this.disposed) return;
    if (this.hoverEvent) {
      const e = this.hoverEvent;
      this.hoverEvent = null;
      this.handlers.hover(this.pick(e), e.clientX, e.clientY);
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.handlers.frame?.();
    requestAnimationFrame(this.loop);
  }

  dispose() {
    this.disposed = true;
    this.resizer?.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

export function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (canvas.getContext("webgl2") || canvas.getContext("webgl")));
  } catch {
    return false;
  }
}
