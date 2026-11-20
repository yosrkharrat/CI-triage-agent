"use client";

import { useEffect, useRef, useState } from "react";

type Point = { x: number; y: number };

/**
 * The run itself, riding the line: a marker that travels the track from the
 * station it last reached to the next, along the drawn path (round the bend
 * at Route too), easing out as it pulls in. It takes the ink of its category
 * once the verdict names one. With reduced motion it simply stands at the
 * station it has reached.
 */
export function Train({ route, at, color }: { route: Point[]; at: number; color: string }) {
  // The segment to animate: from where the train was to where it is now.
  // Adjusted during render when `at` moves, so re-renders mid-trip keep it.
  const [seg, setSeg] = useState({ from: at, to: at });
  if (seg.to !== at) setSeg({ from: at > seg.to ? seg.to : at, to: at });

  const motion = useRef<SVGAnimateMotionElement>(null);
  const body = useRef<SVGGElement>(null);
  const path =
    seg.to > seg.from
      ? "M" + route.slice(seg.from, seg.to + 1).map((p) => `${p.x} ${p.y}`).join(" L")
      : null;

  useEffect(() => {
    if (!path) return;
    motion.current?.beginElement();
    if (body.current) body.current.style.visibility = "visible";
  }, [path]);

  const end = route[at];
  const marker = (
    <>
      <circle r={11} fill="var(--bg)" opacity={0.85} />
      <circle r={7} fill={color} stroke="var(--bg)" strokeWidth={2.5} style={{ transition: "fill 300ms ease-out" }} />
    </>
  );
  const ms = Math.min(1400, 520 * (seg.to - seg.from));

  return (
    <g aria-hidden>
      {/* Reduced motion: standing at the station reached. */}
      <g transform={`translate(${end.x} ${end.y})`} className="motion-safe:hidden">
        {marker}
      </g>
      {/* Otherwise: riding there. */}
      {path ? (
        <g key={`${seg.from}-${seg.to}`} ref={body} className="motion-reduce:hidden" style={{ visibility: "hidden" }}>
          {marker}
          <animateMotion
            ref={motion}
            path={path}
            dur={`${ms}ms`}
            begin="indefinite"
            fill="freeze"
            calcMode="spline"
            keyTimes="0;1"
            keySplines="0.16 1 0.3 1"
          />
        </g>
      ) : (
        <g transform={`translate(${end.x} ${end.y})`} className="motion-reduce:hidden">
          {marker}
        </g>
      )}
    </g>
  );
}
