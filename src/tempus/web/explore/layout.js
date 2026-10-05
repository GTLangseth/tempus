// Deterministic 3D layout for the concept tree: a "dandelion".
//
// Concepts sit on nested spherical caps: each top-level area owns a cap of the sphere, its
// children spread within that cap one shell further out, and so on. A concept's own series
// form a small seed head (a puff of points on a sphere) just beyond it. No physics: the same
// tree always lands in the same place, so the camera can fly to remembered spots and nothing
// jiggles while ~2,000 leaves render.

const GOLDEN = Math.PI * (3 - Math.sqrt(5));
export const SHELL = [0, 190, 300, 390, 465, 530]; // radius of each depth

const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const scale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a) => { const l = Math.hypot(...a) || 1; return scale(a, 1 / l); };

function basis(d) {
  const ref = Math.abs(d[1]) < 0.92 ? [0, 1, 0] : [1, 0, 0];
  const u = norm(cross(ref, d));
  return [u, cross(d, u)];
}

// The i-th of m directions spread evenly over a cap of half-angle `a` around `d`.
function inCap(d, a, i, m) {
  if (m === 1) return d;
  const theta = a * Math.sqrt((i + 0.5) / m);
  const phi = i * GOLDEN + 0.6;
  const [u, v] = basis(d);
  return norm(add(scale(d, Math.cos(theta)),
    add(scale(u, Math.sin(theta) * Math.cos(phi)), scale(v, Math.sin(theta) * Math.sin(phi)))));
}

// The i-th of m points on a sphere (Fibonacci lattice).
function onSphere(i, m) {
  const y = 1 - (2 * (i + 0.5)) / m;
  const r = Math.sqrt(1 - y * y);
  return [Math.cos(i * GOLDEN) * r, y * 0.82, Math.sin(i * GOLDEN) * r];
}

export const nodeSize = (total) => 2.2 + 0.95 * Math.sqrt(total);
const puffRadius = (n) => 3 + 2.4 * Math.cbrt(n);

/**
 * concepts: [{id, parent_id, name, total, series}] in tree order
 * series:   [{source, id, title, freq, concept, ...}]
 * Returns {nodes: Map(id → node), leaves: [leaf], root: node, children: Map(id|null → [id])}.
 */
export function layout(concepts, series) {
  const children = new Map([[null, []]]);
  for (const c of concepts) {
    if (!children.has(c.id)) children.set(c.id, []);
    const p = c.parent_id ?? null;
    if (!children.has(p)) children.set(p, []);
    children.get(p).push(c.id);
  }
  const byId = new Map(concepts.map((c) => [c.id, c]));
  const leavesOf = new Map();
  for (const s of series) {
    const key = byId.has(s.concept) ? s.concept : null;
    if (!leavesOf.has(key)) leavesOf.set(key, []);
    leavesOf.get(key).push(s);
  }

  const nodes = new Map();
  const leaves = [];
  const root = { id: null, pos: [0, 0, 0], dir: [0, 1, 0], depth: 0, size: 5, parent: undefined };

  function seedHead(node, list) {
    if (!list?.length) return;
    const rho = puffRadius(list.length);
    const stalk = node.size + rho + 10;
    const center = node.id === null ? [0, 0, 0] : add(node.pos, scale(node.dir, stalk));
    node.puff = { center, radius: rho };
    list.forEach((s, i) => {
      const p = onSphere(i, list.length);
      leaves.push({ series: s, concept: node.id, pos: add(center, scale(p, rho)), index: leaves.length });
    });
  }

  function place(id, dir, cap, depth, parent) {
    const c = byId.get(id);
    const node = {
      id, name: c.name, total: c.total, direct: c.series, depth, dir, parent,
      pos: scale(dir, SHELL[Math.min(depth, SHELL.length - 1)]), size: nodeSize(c.total),
    };
    nodes.set(id, node);
    const kids = children.get(id) ?? [];
    const childCap = Math.min(cap * 0.8, (cap * 1.05) / Math.sqrt(Math.max(kids.length, 1)));
    kids.forEach((k, i) => place(k, inCap(dir, cap * 0.92, i, kids.length), childCap, depth + 1, id));
    seedHead(node, leavesOf.get(id));
  }

  const top = children.get(null);
  const cap0 = Math.acos(1 - 2 / Math.max(top.length, 2)) * 0.9;
  top.forEach((id, i) => place(id, norm(onSphere(i, top.length)), cap0, 1, null));
  seedHead(root, leavesOf.get(null)); // series not placed anywhere yet sit at the center
  nodes.set(null, root);
  return { nodes, leaves, root, children };
}

export function subtree(children, id) {
  const out = new Set([id]);
  const stack = [id];
  while (stack.length) for (const k of children.get(stack.pop()) ?? []) { out.add(k); stack.push(k); }
  return out;
}

export function ancestors(nodes, id) {
  const out = [];
  let n = nodes.get(id);
  while (n && n.id !== null) { out.unshift(n.id); n = nodes.get(n.parent); }
  return out;
}
