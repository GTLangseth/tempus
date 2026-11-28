// Dual-theme controller (Neon Notebook light + Catppuccin Macchiato dark).
// Keeps <meta name="color-scheme">, localStorage ("tempus.theme"), and the JS theme object
// in sync without writing semantic color tokens directly onto :root inline styles.
const STORAGE_KEY = "tempus.theme";

function readStored() {
  try {
    const val = localStorage.getItem(STORAGE_KEY);
    return val === "light" || val === "dark" ? val : null;
  } catch {
    return null;
  }
}

export async function initTheme(onChange) {
  const darkMedia = window.matchMedia("(prefers-color-scheme: dark)");
  let override = readStored();

  const systemScheme = () => (darkMedia.matches ? "dark" : "light");
  const resolvedScheme = () => override ?? systemScheme();
  const syncMeta = () => {
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = override ?? "light dark";
  };

  syncMeta();

  const raw = await (await fetch("/theme.json")).json();
  const root = document.documentElement.style;
  for (const [k, v] of Object.entries(raw.color ?? {})) {
    if (typeof v === "string") root.setProperty(`--${k}-light`, v);
  }
  for (const [k, v] of Object.entries(raw.dark ?? {})) {
    if (typeof v === "string") root.setProperty(`--${k}-dark`, v);
  }
  for (const [k, v] of Object.entries(raw.font ?? {})) {
    root.setProperty(`--font-${k}`, v);
  }
  for (const [k, v] of Object.entries(raw.rounded ?? {})) {
    root.setProperty(`--rounded-${k}`, `${v}px`);
  }
  if (raw.grid != null) root.setProperty("--grid-cell", `${raw.grid}px`);

  const initial = resolvedScheme();
  const theme = {
    ...raw,
    scheme: initial,
    color: { ...(initial === "dark" ? raw.dark : raw.color) },
  };

  const applyResolved = () => {
    const next = resolvedScheme();
    if (next === theme.scheme) return;
    theme.scheme = next;
    Object.assign(theme.color, next === "dark" ? raw.dark : raw.color);
    onChange?.(theme);
  };

  document.getElementById("theme-toggle")?.addEventListener("click", () => {
    const sys = systemScheme();
    const target = resolvedScheme() === "dark" ? "light" : "dark";
    if (target === sys) {
      override = null;
      try { localStorage.removeItem(STORAGE_KEY); } catch { /* storage may be blocked */ }
    } else {
      override = target;
      try { localStorage.setItem(STORAGE_KEY, target); } catch { /* storage may be blocked */ }
    }
    syncMeta();
    applyResolved();
  });

  // Never erase a stored override when the OS preference shifts to match it.
  darkMedia.addEventListener("change", () => applyResolved());

  window.addEventListener("storage", (e) => {
    if (e.key !== STORAGE_KEY) return;
    override = readStored();
    syncMeta();
    applyResolved();
  });

  return theme;
}
