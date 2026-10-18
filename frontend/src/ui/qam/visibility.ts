// Context behind @decky/api's useQuickAccessVisible: provided around Remora's QAM panel only, so
// elsewhere it reads the default `false`, as in Decky.
import { createContext, useContext } from 'react';

export const QuickAccessVisibleContext = createContext(false);
export const useQuickAccessVisible = (): boolean => useContext(QuickAccessVisibleContext);
