// The header row of Remora's QAM panel: a title plus optional small icon buttons.
import type { ReactNode } from 'react';
import { DialogButton, Focusable } from '../../hooks/components';
import { classes } from '../../hooks/classes';

export interface TitleButton {
  key: string;
  label: string;
  icon: ReactNode;
  onClick: () => void;
}

const buttonStyle = { height: 28, width: 40, minWidth: 0, padding: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center' } as const;

export function TitleBar({ title, left, right }: { title: ReactNode; left?: TitleButton; right?: TitleButton[] }) {
  return (
    <Focusable className={classes.static.Title} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0 16px', boxShadow: 'none' }} flow-children="horizontal">
      {left && (
        <DialogButton key={left.key} style={buttonStyle} onClick={left.onClick} onOKActionDescription={left.label}>
          {left.icon}
        </DialogButton>
      )}
      <div style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{title}</div>
      {right?.map((b) => (
        <DialogButton key={b.key} style={buttonStyle} onClick={b.onClick} onOKActionDescription={b.label}>
          {b.icon}
        </DialogButton>
      ))}
    </Focusable>
  );
}
