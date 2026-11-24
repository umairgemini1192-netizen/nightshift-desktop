import {
  Activity,
  AlertCircle,
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Command,
  Cpu,
  FolderGit2,
  History,
  ListChecks,
  Loader2,
  Minus,
  Moon,
  PauseCircle,
  Play,
  Radar,
  Search,
  Settings,
  Sparkles,
  Square,
  Sun,
  Monitor,
  X,
  type LucideIcon,
} from "lucide-react";

const ICON_MAP: Record<string, LucideIcon> = {
  Activity,
  AlertCircle,
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Command,
  Cpu,
  FolderGit2,
  History,
  ListChecks,
  Loader2,
  Minus,
  Moon,
  PauseCircle,
  Play,
  Radar,
  Search,
  Settings,
  Sparkles,
  Square,
  Sun,
  Monitor,
  X,
};

export type IconName = keyof typeof ICON_MAP;

interface IconProps {
  name: string;
  size?: 16 | 20 | 24;
  className?: string;
}

export function Icon({ name, size = 20, className }: IconProps) {
  const Component = ICON_MAP[name] ?? Sparkles;
  return <Component size={size} strokeWidth={1.75} className={className} aria-hidden="true" />;
}
