import { useEffect, useState } from "react";

export function AuroraCanvas() {
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    const onBlur = () => setPaused(true);
    const onFocus = () => setPaused(false);
    const onVisibility = () => setPaused(document.hidden);

    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  return (
    <div
      aria-hidden="true"
      className={`pointer-events-none fixed inset-0 -z-10 overflow-hidden bg-bg-canvas ${
        paused ? "ns-aurora-paused" : ""
      }`}
    >
      <div className="ns-aurora-orb-a absolute -top-16 -left-16 h-1/2 w-1/2 rounded-pill blur-3xl" />
      <div className="ns-aurora-orb-b absolute top-1/4 -right-16 h-1/2 w-1/2 rounded-pill blur-3xl" />
      <div className="ns-aurora-orb-c absolute -bottom-16 left-1/3 h-1/2 w-1/2 rounded-pill blur-3xl" />
      <div className="ns-grid-overlay absolute inset-0" />
    </div>
  );
}
