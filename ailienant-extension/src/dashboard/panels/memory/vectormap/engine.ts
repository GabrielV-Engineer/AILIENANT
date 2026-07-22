// three.js renderer for the embedding vector map. Replaces the previous
// regl-scatterplot renderer, whose underlying `regl` compiles draw commands with
// `new Function` — forbidden by the dashboard's `script-src 'self'` CSP (no
// 'unsafe-eval'). three.js compiles GLSL on the GPU driver, so it needs no eval.
// Kept in its own module so three stays in a lazily-loaded split chunk.
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

export interface VectorMapController {
    resize(width: number, height: number): void;
    /** Highlight the given point indices (search matches); [] clears. */
    setSearch(matchIndices: number[]): void;
    /** Dim points beyond `radiusFrac` (0..1 of the data diagonal) from `selectedIdx`. */
    setFilter(selectedIdx: number | null, radiusFrac: number): void;
    dispose(): void;
}

interface Pt { x: number; y: number; }

interface Opts {
    colors: string[];   // per-point base color (density ramp); length === points.length
    onHover: (idx: number | null, localX: number, localY: number) => void;
    onSelect: (idx: number | null) => void;
}

const HOVER_PX = 11;
const HIGHLIGHT = new THREE.Color('#E3B341');
const WORLD_SPAN = 180;   // data is normalised into roughly [-90, 90]

export function createVectorMap(canvas: HTMLCanvasElement, points: Pt[], opts: Opts): VectorMapController {
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setClearColor(0x0d1117, 1);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    let width = canvas.clientWidth || 800;
    let height = canvas.clientHeight || 600;
    renderer.setSize(width, height, false);

    const scene = new THREE.Scene();

    // Normalise the projected coordinates into a stable world box.
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const p of points) {
        if (p.x < minX) { minX = p.x; } if (p.x > maxX) { maxX = p.x; }
        if (p.y < minY) { minY = p.y; } if (p.y > maxY) { maxY = p.y; }
    }
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const span = Math.max(maxX - minX, maxY - minY) || 1;
    const scale = WORLD_SPAN / span;
    const world: Pt[] = points.map(p => ({ x: (p.x - cx) * scale, y: (p.y - cy) * scale }));

    const aspect = width / height;
    const F = 110;
    const camera = new THREE.OrthographicCamera(-F * aspect, F * aspect, F, -F, 0.1, 1000);
    camera.position.set(0, 0, 100);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableRotate = false;
    controls.enableDamping = true;
    controls.dampingFactor = 0.12;
    controls.screenSpacePanning = true;
    controls.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };

    // Geometry: one vertex per point, per-vertex color.
    const positions = new Float32Array(world.length * 3);
    const baseColors: THREE.Color[] = [];
    const colorAttr = new Float32Array(world.length * 3);
    const tmp = new THREE.Color();
    world.forEach((p, i) => {
        positions[i * 3] = p.x; positions[i * 3 + 1] = p.y; positions[i * 3 + 2] = 0;
        tmp.set(opts.colors[i] ?? '#3987e5');
        baseColors.push(tmp.clone());
        colorAttr[i * 3] = tmp.r; colorAttr[i * 3 + 1] = tmp.g; colorAttr[i * 3 + 2] = tmp.b;
    });
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geo.setAttribute('color', new THREE.BufferAttribute(colorAttr, 3));
    const material = new THREE.PointsMaterial({
        size: 7, vertexColors: true, sizeAttenuation: false,
        transparent: true, opacity: 0.92, depthWrite: false, blending: THREE.AdditiveBlending,
    });
    const cloud = new THREE.Points(geo, material);
    cloud.frustumCulled = false;
    scene.add(cloud);

    // ── State: search matches + similarity filter recompute the color buffer ──
    let matches = new Set<number>();
    let filterSel: number | null = null;
    let filterRadius = 1;

    function recolor(): void {
        const attr = geo.getAttribute('color') as THREE.BufferAttribute;
        const dim = new THREE.Color();
        const radiusWorld = filterRadius * WORLD_SPAN * Math.SQRT2;
        const origin = filterSel !== null ? world[filterSel] : null;
        for (let i = 0; i < world.length; i++) {
            let c = baseColors[i];
            if (matches.has(i)) {
                c = HIGHLIGHT;
            } else if (origin && filterRadius < 1) {
                const dx = world[i].x - origin.x, dy = world[i].y - origin.y;
                if (Math.sqrt(dx * dx + dy * dy) > radiusWorld) {
                    dim.copy(baseColors[i]).multiplyScalar(0.12);   // fade toward background
                    c = dim;
                }
            }
            attr.setXYZ(i, c.r, c.g, c.b);
        }
        attr.needsUpdate = true;
    }

    // ── Screen-space nearest-point picking (robust for Points) ───────────────
    const ndc = new THREE.Vector3();
    function nearest(localX: number, localY: number): number {
        let best = -1, bestD = HOVER_PX * HOVER_PX;
        for (let i = 0; i < world.length; i++) {
            ndc.set(world[i].x, world[i].y, 0).project(camera);
            const sx = (ndc.x * 0.5 + 0.5) * width;
            const sy = (-ndc.y * 0.5 + 0.5) * height;
            const dx = sx - localX, dy = sy - localY;
            const d = dx * dx + dy * dy;
            if (d < bestD) { bestD = d; best = i; }
        }
        return best;
    }

    function localXY(ev: MouseEvent): [number, number] {
        const rect = renderer.domElement.getBoundingClientRect();
        return [ev.clientX - rect.left, ev.clientY - rect.top];
    }
    function onMove(ev: MouseEvent): void {
        const [lx, ly] = localXY(ev);
        const idx = nearest(lx, ly);
        opts.onHover(idx >= 0 ? idx : null, lx, ly);
        renderer.domElement.style.cursor = idx >= 0 ? 'pointer' : 'grab';
    }
    let downXY: [number, number] | null = null;
    function onDown(ev: MouseEvent): void { downXY = localXY(ev); }
    function onUp(ev: MouseEvent): void {
        const [lx, ly] = localXY(ev);
        // Treat as a click only if the pointer barely moved (else it was a pan).
        if (downXY && Math.hypot(lx - downXY[0], ly - downXY[1]) < 4) {
            const idx = nearest(lx, ly);
            opts.onSelect(idx >= 0 ? idx : null);
        }
        downXY = null;
    }
    renderer.domElement.addEventListener('pointermove', onMove);
    renderer.domElement.addEventListener('pointerdown', onDown);
    renderer.domElement.addEventListener('pointerup', onUp);

    let raf = 0;
    function frame(): void {
        raf = requestAnimationFrame(frame);
        controls.update();
        renderer.render(scene, camera);
    }
    frame();

    return {
        resize(w: number, h: number): void {
            if (w <= 0 || h <= 0) { return; }
            width = w; height = h;
            const a = w / h;
            camera.left = -F * a; camera.right = F * a; camera.top = F; camera.bottom = -F;
            camera.updateProjectionMatrix();
            renderer.setSize(w, h, false);
        },
        setSearch(matchIndices: number[]): void {
            matches = new Set(matchIndices);
            recolor();
        },
        setFilter(selectedIdx: number | null, radiusFrac: number): void {
            filterSel = selectedIdx;
            filterRadius = radiusFrac;
            recolor();
        },
        dispose(): void {
            cancelAnimationFrame(raf);
            renderer.domElement.removeEventListener('pointermove', onMove);
            renderer.domElement.removeEventListener('pointerdown', onDown);
            renderer.domElement.removeEventListener('pointerup', onUp);
            controls.dispose();
            geo.dispose();
            material.dispose();
            renderer.dispose();
        },
    };
}
