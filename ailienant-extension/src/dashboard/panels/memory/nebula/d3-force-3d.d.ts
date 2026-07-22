// Minimal ambient declarations for d3-force-3d (no published @types package).
// Covers only the surface the Nebula layout uses: a 3D force simulation ticked to
// a settled state, then frozen. Extend if more of the API is adopted.
declare module 'd3-force-3d' {
    export interface SimNode {
        id: string;
        x?: number;
        y?: number;
        z?: number;
        vx?: number;
        vy?: number;
        vz?: number;
        index?: number;
    }

    export interface SimLink {
        source: string | SimNode;
        target: string | SimNode;
    }

    interface ManyBodyForce {
        strength(v: number): ManyBodyForce;
        theta(v: number): ManyBodyForce;
        distanceMax(v: number): ManyBodyForce;
    }

    interface LinkForce {
        id(accessor: (n: SimNode) => string): LinkForce;
        distance(v: number): LinkForce;
        strength(v: number): LinkForce;
    }

    interface CenterForce {
        strength(v: number): CenterForce;
    }

    export interface Simulation {
        nodes(nodes: SimNode[]): Simulation;
        force(name: string, force: ManyBodyForce | LinkForce | CenterForce): Simulation;
        alpha(a: number): Simulation;
        alphaDecay(a: number): Simulation;
        alphaMin(a: number): Simulation;
        tick(n?: number): Simulation;
        stop(): Simulation;
    }

    export function forceSimulation(nodes?: SimNode[], numDimensions?: number): Simulation;
    export function forceManyBody(): ManyBodyForce;
    export function forceLink(links?: SimLink[]): LinkForce;
    export function forceCenter(x?: number, y?: number, z?: number): CenterForce;
}
