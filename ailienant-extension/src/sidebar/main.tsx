import './sidebar.css';
import { createRoot } from 'react-dom/client';
import { SessionBrowser } from './SessionBrowser';

document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('root');
    if (!root) { return; }
    createRoot(root).render(<SessionBrowser />);
});
