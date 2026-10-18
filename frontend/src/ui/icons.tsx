// Small inline icons (no icon library, to keep the bundle self-contained).
import type { CSSProperties } from 'react';

interface IconProps {
  size?: number;
  style?: CSSProperties;
}

function Svg({ size = 20, style, d, viewBox = '0 0 24 24' }: IconProps & { d: string; viewBox?: string }) {
  return (
    <svg width={size} height={size} viewBox={viewBox} fill="currentColor" style={{ display: 'block', ...style }} aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

// A remora (the suckerfish): body, fin and the oval sucker disc on its head.
export const RemoraIcon = (p: IconProps) => (
  <Svg {...p} d="M2 13c2.5-3.2 6.3-5 10.6-5 2.6 0 4.9.7 6.7 1.9L22 8v8l-2.7-1.9c-1.8 1.2-4.1 1.9-6.7 1.9-4.3 0-8.1-1.8-10.6-3zm5.3-2.6a1.1 1.1 0 1 0 0 2.2 1.1 1.1 0 0 0 0-2.2zM9 6.3c1.6-.8 3.6-1 5.4-.6l-1.1 1.3c-1.5-.2-2.9 0-4.3.4z" />
);
export const StoreIcon = (p: IconProps) => (
  <Svg {...p} d="M4 4h16l1 5a3 3 0 0 1-2 2.8V20H5v-8.2A3 3 0 0 1 3 9zm3 9v5h4v-5zm6 0v5h4v-5zM5.6 6l-.6 3a1 1 0 0 0 2 0V6zm3.4 0v3a1.5 1.5 0 0 0 3 0V6zm5 0v3a1.5 1.5 0 0 0 3 0V6zm4.4 0H17v3a1 1 0 0 0 2 0z" />
);
export const GearIcon = (p: IconProps) => (
  <Svg {...p} d="M19.4 13a7.6 7.6 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4l-.3 2.9a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.6 7.6 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.4 7.4 0 0 0 1.7 1L11 21h4l.3-2.9a7.4 7.4 0 0 0 1.7-1l2.5 1 2-3.5zM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z" />
);
export const WarningIcon = (p: IconProps) => <Svg {...p} d="M12 2 1 21h22zm1 15h-2v-2h2zm0-4h-2V9h2z" />;
export const BackIcon = (p: IconProps) => <Svg {...p} d="M20 11H7.8l5.6-5.6L12 4l-8 8 8 8 1.4-1.4L7.8 13H20z" />;
export const FolderIcon = (p: IconProps) => <Svg {...p} d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8z" />;
export const FileIcon = (p: IconProps) => <Svg {...p} d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zm0 7V3.5L19.5 9z" />;
export const PlugIcon = (p: IconProps) => <Svg {...p} d="M16 7V3h-2v4h-4V3H8v4H6v5a6 6 0 0 0 5 5.9V22h2v-4.1a6 6 0 0 0 5-5.9V7z" />;
export const UpIcon = (p: IconProps) => <Svg {...p} d="M4 12l1.4 1.4L11 7.8V20h2V7.8l5.6 5.6L20 12l-8-8z" />;
