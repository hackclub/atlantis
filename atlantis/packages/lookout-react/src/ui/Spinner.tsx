import React from "react";
import { colors } from "./theme.js";

export interface SpinnerProps {
  size?: "sm" | "md" | "lg";
  color?: string;
}

const sizes = { sm: 16, md: 24, lg: 40 };

export function Spinner({ size = "md", color }: SpinnerProps) {
  const s = sizes[size];
  const baseColor = colors.spinner.base;
  const trackColor = color || colors.spinner.track;
  
  return (
    <svg
      width={s}
      height={s}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      style={{
        animation: "spin 1s linear infinite",
        flexShrink: 0,
      }}
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke={baseColor}
        strokeWidth="2"
      />
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke={trackColor}
        strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray="62.83"
        strokeDashoffset="47.12"
      />
    </svg>
  );
}
