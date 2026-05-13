declare function acquireVsCodeApi(): {
    postMessage(message: unknown): void;
    getState(): unknown;
    setState(state: unknown): void;
};

const vscode = acquireVsCodeApi();

function mount(container: HTMLElement): void {
    let active = false;

    const btn = document.createElement('button');
    btn.id = 'planner-mode-toggle';
    btn.setAttribute('role', 'switch');
    btn.setAttribute('aria-checked', 'false');
    btn.textContent = 'Planner Mode: OFF';
    btn.style.cssText = [
        'display:block',
        'width:100%',
        'padding:8px 14px',
        'margin:8px 0',
        'border:2px solid var(--vscode-button-border,#888)',
        'background:var(--vscode-button-secondaryBackground)',
        'color:var(--vscode-button-secondaryForeground)',
        'cursor:pointer',
        'border-radius:4px',
        'font-weight:bold',
        'font-size:13px',
        'transition:background .15s,border-color .15s',
    ].join(';');

    btn.addEventListener('click', () => {
        active = !active;
        btn.textContent = `Planner Mode: ${active ? 'ON 🧠' : 'OFF'}`;
        btn.setAttribute('aria-checked', String(active));
        btn.style.background = active
            ? 'var(--vscode-button-background)'
            : 'var(--vscode-button-secondaryBackground)';
        btn.style.color = active
            ? 'var(--vscode-button-foreground)'
            : 'var(--vscode-button-secondaryForeground)';
        btn.style.borderColor = active
            ? 'var(--vscode-focusBorder,#007fd4)'
            : 'var(--vscode-button-border,#888)';
        vscode.postMessage({ type: 'togglePlannerMode', value: active });
    });

    container.appendChild(btn);
}

document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('root');
    if (root) { mount(root); }
});
