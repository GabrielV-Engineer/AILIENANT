// The "Neural Nebula" WebGL engine. Kept in its own module (statically importing
// three + d3-force-3d) so the bundler splits it into a lazily-loaded chunk that is
// only fetched when the 3D layer opens. Nodes are drawn as a single InstancedMesh
// per shape with a custom fake-glass shader (Fresnel rim + emissive core) — real
// glass (transmission) neither instances nor holds 60 FPS on an integrated GPU, so
// the look is shaded, not physically simulated. Layout is a one-shot force pass,
// then frozen; the only per-frame cost is a cheap breathing uniform and the camera.
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { forceSimulation, forceManyBody, forceLink, forceCenter, type SimNode } from 'd3-force-3d';
import type { GraphResponse, GraphNode } from '../api';

// Community palette — validated against the black surface (dataviz validator, dark,
// #000000): lightness/chroma/contrast PASS; CVD in the legal floor band, carried by
// the secondary shape + spatial-cluster encodings. External nodes are neutral gray.
const CAT = ['#3987e5', '#199e70', '#c98500', '#008300', '#9085e9', '#e66767', '#d55181', '#d95926'];
const EXTERNAL_COLOR = '#6e7681';
const GOLD = '#e3b341';

function communityColor(id: number | null | undefined): string {
    if (id === null || id === undefined) { return '#3d5a78'; }  // muted until analytics assigns one
    return CAT[((id % CAT.length) + CAT.length) % CAT.length];
}

const NODE_VERT = /* glsl */`
    attribute vec3 aColor;
    attribute float aGlow;
    uniform float uBreath;
    varying vec3 vColor;
    varying float vGlow;
    varying vec3 vNormalV;
    varying vec3 vPosV;
    void main() {
        vColor = aColor;
        vGlow = aGlow;
        vec3 p = position * uBreath;
        vec4 worldView = modelViewMatrix * instanceMatrix * vec4(p, 1.0);
        vPosV = worldView.xyz;
        vNormalV = normalize(mat3(modelViewMatrix) * mat3(instanceMatrix) * normal);
        gl_Position = projectionMatrix * worldView;
    }
`;

const NODE_FRAG = /* glsl */`
    precision highp float;
    uniform float uOpacity;
    varying vec3 vColor;
    varying float vGlow;
    varying vec3 vNormalV;
    varying vec3 vPosV;
    void main() {
        vec3 N = normalize(vNormalV);
        vec3 V = normalize(-vPosV);
        float fres = pow(1.0 - max(dot(N, V), 0.0), 2.5);   // crystal rim
        float core = (1.0 - fres) * vGlow;                  // energy toward the centre
        vec3 col = vColor * (0.30 + core) + vColor * fres * 1.5;
        float alpha = clamp(uOpacity + fres * 0.55, 0.0, 1.0);
        gl_FragColor = vec4(col, alpha);
    }
`;

export interface NebulaController {
    resize(width: number, height: number): void;
    setSearch(query: string): void;
    setReducedMotion(reduced: boolean): void;
    dispose(): void;
}

interface BuildOpts {
    onSelect: (node: GraphNode | null) => void;
    reducedMotion: boolean;
}

interface NodeRecord {
    node: GraphNode;
    shape: 'file' | 'external';
    localIndex: number;   // index within its shape's InstancedMesh
    baseGlow: number;
    pos: THREE.Vector3;
}

export function createNebula(canvas: HTMLCanvasElement, graph: GraphResponse, opts: BuildOpts): NebulaController {
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setClearColor(0x000000, 1);
    const initialW = canvas.clientWidth || 800;
    const initialH = canvas.clientHeight || 600;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(initialW, initialH, false);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x000000);
    scene.fog = new THREE.FogExp2(0x01030a, 0.0016);   // faint haze, no grid

    const camera = new THREE.PerspectiveCamera(55, initialW / initialH, 1, 20000);
    camera.position.set(0, 0, 900);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = 0.6;

    // ── One-shot force layout, then freeze ──────────────────────────────────
    const nodes = graph.nodes;
    const maxPpr = Math.max(1e-9, ...nodes.map(n => n.ppr_score));
    const simNodes: SimNode[] = nodes.map(n => ({ id: n.id }));
    const simLinks = graph.edges.map(e => ({ source: e.source, target: e.target }));
    const sim = forceSimulation(simNodes, 3)
        .force('charge', forceManyBody().strength(-55).distanceMax(1200))
        .force('link', forceLink(simLinks).id((d: SimNode) => d.id).distance(60).strength(0.35))
        .force('center', forceCenter(0, 0, 0).strength(0.04))
        .alpha(1)
        .alphaDecay(0.06)
        .stop();
    // Settle synchronously. Bounded iterations keep this off the render loop and
    // scale to the current node counts; a Web-Worker pass is the documented seam
    // for far larger graphs.
    const ticks = Math.min(220, Math.max(80, Math.round(900 / Math.sqrt(nodes.length + 1))));
    sim.tick(ticks);

    const posById = new Map<string, THREE.Vector3>();
    simNodes.forEach(sn => {
        posById.set(sn.id, new THREE.Vector3((sn.x ?? 0) * 3, (sn.y ?? 0) * 3, (sn.z ?? 0) * 3));
    });

    // ── Partition by shape and build one InstancedMesh each ──────────────────
    const records: NodeRecord[] = [];
    const fileNodes: GraphNode[] = [];
    const extNodes: GraphNode[] = [];
    for (const n of nodes) {
        (n.is_external ? extNodes : fileNodes).push(n);
    }

    const sphereGeo = new THREE.IcosahedronGeometry(1, 2);        // file  → crystal sphere
    const octaGeo = new THREE.OctahedronGeometry(1, 0);           // external dep → faceted
    const dummy = new THREE.Object3D();

    function buildMesh(list: GraphNode[], geo: THREE.BufferGeometry, shape: 'file' | 'external'): THREE.InstancedMesh | null {
        if (list.length === 0) { return null; }
        const material = new THREE.ShaderMaterial({
            vertexShader: NODE_VERT,
            fragmentShader: NODE_FRAG,
            transparent: true,
            depthWrite: true,
            uniforms: { uBreath: { value: 1 }, uOpacity: { value: 0.82 } },
        });
        const mesh = new THREE.InstancedMesh(geo, material, list.length);
        mesh.frustumCulled = true;
        const colorAttr = new Float32Array(list.length * 3);
        const glowAttr = new Float32Array(list.length);
        const c = new THREE.Color();

        list.forEach((n, i) => {
            const norm = Math.min(1, n.ppr_score / maxPpr);
            const radius = (6 + norm * 26) * (n.is_god_node ? 1.6 : 1);
            const pos = posById.get(n.id) ?? new THREE.Vector3();
            dummy.position.copy(pos);
            dummy.scale.setScalar(radius);
            dummy.updateMatrix();
            mesh.setMatrixAt(i, dummy.matrix);

            // Community identity on the hue channel (constant lightness); centrality
            // and recency ride size + core-glow so brightness never competes with hue.
            c.set(n.is_god_node ? GOLD : communityColor(n.leiden_community_id));
            colorAttr[i * 3] = c.r; colorAttr[i * 3 + 1] = c.g; colorAttr[i * 3 + 2] = c.b;
            const baseGlow = 0.35 + norm * 0.9 + (n.is_god_node ? 0.5 : 0);
            glowAttr[i] = baseGlow;

            records.push({ node: n, shape, localIndex: i, baseGlow, pos });
        });

        geo.setAttribute('aColor', new THREE.InstancedBufferAttribute(colorAttr, 3));
        geo.setAttribute('aGlow', new THREE.InstancedBufferAttribute(glowAttr, 1));
        mesh.instanceMatrix.needsUpdate = true;
        mesh.computeBoundingSphere();
        scene.add(mesh);
        return mesh;
    }

    const fileMesh = buildMesh(fileNodes, sphereGeo, 'file');
    const extMesh = buildMesh(extNodes, octaGeo, 'external');
    const meshes = [fileMesh, extMesh].filter((m): m is THREE.InstancedMesh => m !== null);

    // ── God-node gold rings (≤3 sprites — negligible cost) ───────────────────
    const ringSprites: THREE.Sprite[] = [];
    const ringTex = makeRingTexture();
    for (const r of records) {
        if (!r.node.is_god_node) { continue; }
        const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
            map: ringTex, color: 0xe3b341, transparent: true, depthWrite: false,
            blending: THREE.AdditiveBlending,
        }));
        const norm = Math.min(1, r.node.ppr_score / maxPpr);
        const s = (6 + norm * 26) * 1.6 * 3.4;
        sprite.scale.set(s, s, 1);
        sprite.position.copy(r.pos);
        scene.add(sprite);
        ringSprites.push(sprite);
    }

    // ── Edges: ultra-thin faint lines, brightened on search ──────────────────
    const edgePositions: number[] = [];
    const edgeColors: number[] = [];
    const edgeRecords: { aIdx: number; bIdx: number; endpoints: [string, string] }[] = [];
    const cA = new THREE.Color('#2b4a8f');   // blue
    const cB = new THREE.Color('#2fb6d6');   // cyan
    for (const e of graph.edges) {
        const a = posById.get(e.source); const b = posById.get(e.target);
        if (!a || !b) { continue; }
        const idx = edgePositions.length / 3;
        edgePositions.push(a.x, a.y, a.z, b.x, b.y, b.z);
        edgeColors.push(cA.r, cA.g, cA.b, cB.r, cB.g, cB.b);
        edgeRecords.push({ aIdx: idx, bIdx: idx + 1, endpoints: [e.source, e.target] });
    }
    const edgeGeo = new THREE.BufferGeometry();
    edgeGeo.setAttribute('position', new THREE.Float32BufferAttribute(edgePositions, 3));
    edgeGeo.setAttribute('color', new THREE.Float32BufferAttribute(edgeColors, 3));
    const edgeMat = new THREE.LineBasicMaterial({
        vertexColors: true, transparent: true, opacity: 0.13, blending: THREE.AdditiveBlending, depthWrite: false,
    });
    const edgeLines = new THREE.LineSegments(edgeGeo, edgeMat);
    edgeLines.frustumCulled = false;
    scene.add(edgeLines);

    // ── Picking ──────────────────────────────────────────────────────────────
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();

    function pick(clientX: number, clientY: number): GraphNode | null {
        const rect = renderer.domElement.getBoundingClientRect();
        pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
        pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
        raycaster.setFromCamera(pointer, camera);
        let best: { dist: number; node: GraphNode } | null = null;
        for (const mesh of meshes) {
            const hits = raycaster.intersectObject(mesh);
            if (hits.length && hits[0].instanceId !== undefined && hits[0].instanceId !== null) {
                const rec = records.find(r => r.shape === (mesh === fileMesh ? 'file' : 'external') && r.localIndex === hits[0].instanceId);
                if (rec && (!best || hits[0].distance < best.dist)) {
                    best = { dist: hits[0].distance, node: rec.node };
                }
            }
        }
        return best ? best.node : null;
    }

    function onClick(ev: MouseEvent): void {
        opts.onSelect(pick(ev.clientX, ev.clientY));
    }
    function onMove(ev: MouseEvent): void {
        renderer.domElement.style.cursor = pick(ev.clientX, ev.clientY) ? 'pointer' : 'grab';
    }
    renderer.domElement.addEventListener('click', onClick);
    renderer.domElement.addEventListener('pointermove', onMove);

    // ── Search pulse: boost matched glow + brighten incident edges + focus ────
    function setGlow(rec: NodeRecord, value: number): void {
        const mesh = rec.shape === 'file' ? fileMesh : extMesh;
        const attr = mesh?.geometry.getAttribute('aGlow') as THREE.InstancedBufferAttribute | undefined;
        if (!attr) { return; }
        attr.setX(rec.localIndex, value);
        attr.needsUpdate = true;
    }

    let matched = new Set<string>();
    const focusTarget = new THREE.Vector3();
    let hasFocus = false;

    function setSearch(query: string): void {
        const q = query.trim().toLowerCase();
        // Reset previous matches.
        for (const r of records) { setGlow(r, r.baseGlow); }
        matched = new Set();
        if (!q) {
            edgeMat.opacity = 0.13;
            hasFocus = false;
            return;
        }
        const centroid = new THREE.Vector3();
        let count = 0;
        for (const r of records) {
            if (r.node.label.toLowerCase().includes(q) || r.node.full_path.toLowerCase().includes(q)) {
                matched.add(r.node.id);
                setGlow(r, r.baseGlow + 2.4);
                centroid.add(r.pos);
                count += 1;
            }
        }
        // Illuminate incident edges of matched nodes.
        const cols = edgeGeo.getAttribute('color') as THREE.BufferAttribute;
        const litColor = new THREE.Color('#7fd8ff');
        for (const er of edgeRecords) {
            const lit = matched.has(er.endpoints[0]) || matched.has(er.endpoints[1]);
            const src = lit ? litColor : null;
            if (src) {
                cols.setXYZ(er.aIdx, src.r, src.g, src.b);
                cols.setXYZ(er.bIdx, src.r, src.g, src.b);
            } else {
                cols.setXYZ(er.aIdx, cA.r, cA.g, cA.b);
                cols.setXYZ(er.bIdx, cB.r, cB.g, cB.b);
            }
        }
        cols.needsUpdate = true;
        edgeMat.opacity = matched.size ? 0.28 : 0.13;
        if (count > 0) {
            focusTarget.copy(centroid.multiplyScalar(1 / count));
            hasFocus = true;
        }
    }

    // ── Animation loop ─────────────────────────────────────────────────────────
    let reduced = opts.reducedMotion;
    let raf = 0;
    const clock = new THREE.Clock();
    function frame(): void {
        raf = requestAnimationFrame(frame);
        const t = clock.getElapsedTime();
        const breath = reduced ? 1 : 1 + Math.sin(t * 0.7) * 0.01;   // <1% breathing
        for (const mesh of meshes) {
            (mesh.material as THREE.ShaderMaterial).uniforms.uBreath.value = breath;
        }
        if (hasFocus && !reduced) {
            controls.target.lerp(focusTarget, 0.05);
        }
        controls.update();
        renderer.render(scene, camera);
    }
    frame();

    return {
        resize(width: number, height: number): void {
            if (width <= 0 || height <= 0) { return; }
            camera.aspect = width / height;
            camera.updateProjectionMatrix();
            renderer.setSize(width, height, false);
        },
        setSearch,
        setReducedMotion(next: boolean): void { reduced = next; },
        dispose(): void {
            cancelAnimationFrame(raf);
            renderer.domElement.removeEventListener('click', onClick);
            renderer.domElement.removeEventListener('pointermove', onMove);
            controls.dispose();
            for (const mesh of meshes) {
                mesh.geometry.dispose();
                (mesh.material as THREE.Material).dispose();
            }
            for (const s of ringSprites) { (s.material as THREE.Material).dispose(); }
            ringTex.dispose();
            edgeGeo.dispose();
            edgeMat.dispose();
            sphereGeo.dispose();
            octaGeo.dispose();
            renderer.dispose();
        },
    };
}

function makeRingTexture(): THREE.Texture {
    const size = 128;
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const ctx = c.getContext('2d');
    if (ctx) {
        ctx.clearRect(0, 0, size, size);
        ctx.strokeStyle = 'rgba(227,179,65,0.95)';
        ctx.lineWidth = 6;
        ctx.beginPath();
        ctx.arc(size / 2, size / 2, size / 2 - 8, 0, Math.PI * 2);
        ctx.stroke();
    }
    const tex = new THREE.CanvasTexture(c);
    tex.needsUpdate = true;
    return tex;
}
