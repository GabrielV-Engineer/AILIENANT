// Memory dashboard REST client — Phase 7.9.B.1.
// The dashboard SPA is served same-origin as the API, so origin-relative
// fetches need no host or auth. Types mirror api/memory_dashboard.py models.

export interface SectionInfo {
    project_id: string;
    folder: string;
    abs_prefix: string;
    file_count: number;
    has_vectors: boolean;
}

export interface SectionsResponse {
    sections: SectionInfo[];
    project_count: number;
}

export interface GraphNode {
    id: string;
    label: string;
    ppr_score: number;
    in_degree: number;
    out_degree: number;
    is_external: boolean;
    full_path: string;
    leiden_community_id?: number | null;   // Louvain community for coloring; null until computed
    is_god_node?: boolean;                 // top-3 by degree centrality — rendered larger
}

export type EdgeConfidence = 'EXTRACTED' | 'INFERRED' | 'AMBIGUOUS';

export interface GraphEdge {
    source: string;
    target: string;
    confidence?: EdgeConfidence | null;    // null → render as solid (safe default)
    confidence_score?: number | null;
}

export interface GraphResponse {
    project_id: string;
    folder: string;
    nodes: GraphNode[];
    edges: GraphEdge[];
    total_nodes: number;
    capped: boolean;
}

export interface VectorPoint {
    file_path: string;
    label: string;
    x: number;
    y: number;
    token_count: number;
    snippet: string;
}

export interface VectorMapResponse {
    project_id: string;
    folder: string;
    points: VectorPoint[];
    point_count: number;
    degenerate: boolean;
    variance_explained: number[];
}

const BASE = '/api/v1/memory';

async function getJson<T>(path: string, params: Record<string, string | number> = {}): Promise<T> {
    const qs = new URLSearchParams(
        Object.entries(params).map(([k, v]) => [k, String(v)]),
    );
    const suffix = qs.toString() ? `?${qs}` : '';
    const res = await fetch(`${BASE}${path}${suffix}`);
    if (!res.ok) {
        throw new Error(`${path} → HTTP ${res.status}`);
    }
    return res.json() as Promise<T>;
}

export function fetchSections(): Promise<SectionsResponse> {
    return getJson<SectionsResponse>('/sections');
}

export function fetchGraph(projectId: string, folder: string): Promise<GraphResponse> {
    return getJson<GraphResponse>('/graph', { project_id: projectId, folder });
}

export function fetchVectors(projectId: string, folder: string): Promise<VectorMapResponse> {
    return getJson<VectorMapResponse>('/vectors', { project_id: projectId, folder });
}
