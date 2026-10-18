// The :name parameter of /remora/plugin/:name. @decky/ui's useParams reads Steam's router
// context; the URL is the fallback when that lookup is broken.
import { useParams } from '@decky/ui';

function fromLocation(): string | undefined {
  try {
    const m = /\/remora\/plugin\/([^/?#]+)/.exec(window.location.pathname + window.location.hash);
    return m?.[1] ? decodeURIComponent(m[1]) : undefined;
  } catch {
    return undefined;
  }
}

export function usePluginNameParam(): string | undefined {
  let name: string | undefined;
  try {
    name = useParams<{ name?: string }>()?.name;
  } catch {
    name = undefined;
  }
  if (name) {
    try {
      return decodeURIComponent(name);
    } catch {
      return name;
    }
  }
  return fromLocation();
}
