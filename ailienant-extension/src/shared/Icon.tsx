import {
    AlertTriangle, Bot, Brain, Check, CheckCircle2, Circle, ClipboardList, Clock, Cloud,
    Columns3, Cpu, ExternalLink, Eye, EyeOff, Flame, Gauge, KeyRound, Microscope, Moon, MessageSquare,
    Network, Play, Plug, Plus, Search, Send, Settings, ShieldCheck, Sparkles, Telescope,
    Trash2, Wallet, X, XCircle, Zap, Loader2, Pencil, FileCode, Folder, Terminal, Wand2,
    PanelRightOpen, PanelRightClose, ChevronRight, ChevronDown, Square, type LucideIcon,
} from 'lucide-react';

const REGISTRY = {
    'alert': AlertTriangle,
    'bot': Bot,
    'brain': Brain,
    'check': Check,
    'check-circle': CheckCircle2,
    'circle': Circle,
    'clipboard': ClipboardList,
    'clock': Clock,
    'cloud': Cloud,
    'columns': Columns3,
    'cpu': Cpu,
    'external-link': ExternalLink,
    'eye': Eye,
    'eye-off': EyeOff,
    'flame': Flame,
    'gauge': Gauge,
    'key': KeyRound,
    'microscope': Microscope,
    'moon': Moon,
    'message': MessageSquare,
    'network': Network,
    'play': Play,
    'plug': Plug,
    'plus': Plus,
    'search': Search,
    'send': Send,
    'settings': Settings,
    'shield': ShieldCheck,
    'sparkles': Sparkles,
    'telescope': Telescope,
    'trash': Trash2,
    'wallet': Wallet,
    'x': X,
    'x-circle': XCircle,
    'zap': Zap,
    'loader': Loader2,
    'pencil': Pencil,
    'file': FileCode,
    'folder': Folder,
    'terminal': Terminal,
    'wand': Wand2,
    'panel-right-open': PanelRightOpen,
    'panel-right-close': PanelRightClose,
    'chevron-right': ChevronRight,
    'chevron-down': ChevronDown,
    'square': Square,
} as const satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof REGISTRY;

interface IconProps {
    name: IconName;
    size?: number;
    strokeWidth?: number;
    className?: string;
    color?: string;
}

export function Icon({ name, size = 16, strokeWidth = 1.5, className, color }: IconProps): JSX.Element {
    const Lucide = REGISTRY[name];
    return <Lucide size={size} strokeWidth={strokeWidth} className={className} color={color} aria-hidden />;
}
