export interface VsCodeApi {
    postMessage(message: unknown): void;
    getState(): unknown;
    setState(state: unknown): void;
}

declare function acquireVsCodeApi(): VsCodeApi;

// Module-level singleton — acquireVsCodeApi() throws if called twice in a webview.
export const vscode: VsCodeApi = acquireVsCodeApi();
