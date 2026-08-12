// Reads the fixture project ids/names/roots that main.py's lifespan seeded
// in-process and wrote to `.fixture-data/ids.json` (as part of the Playwright
// `webServer` boot, orchestrated by `run-backend.mjs`), so the spec never
// hardcodes a project id it didn't itself request.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export interface FixtureIds {
    proj_a: string;
    proj_a_name: string;
    proj_a_root: string;
    proj_b: string;
    proj_b_name: string;
    proj_b_root: string;
}

export function readFixtureIds(): FixtureIds {
    const path = join(__dirname, '.fixture-data', 'ids.json');
    return JSON.parse(readFileSync(path, 'utf8')) as FixtureIds;
}
