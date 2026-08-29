import React from "react";

import { colors, radii, spacing, fontSize } from "../ui/theme.js";

export interface ScreenPreviewProps {
  imageUrl: string | null;
}

export function ScreenPreview({ imageUrl }: ScreenPreviewProps) {
  if (!imageUrl) return null;

  return (
    <div style={styles.container}>
      <img src={imageUrl} alt="Last captured screenshot" style={styles.image} />
      <span style={styles.label}>Latest screenshot</span>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    position: "relative",
    marginBottom: spacing.lg,
    borderRadius: radii.md,
    overflow: "hidden",
    background: colors.bg.sunken,
    border: `1px solid ${colors.border.default}`,
  },
  image: { width: "100%", display: "block" },
  label: {
    position: "absolute",
    bottom: spacing.sm,
    right: spacing.sm,
    fontSize: fontSize.sm,
    color: colors.text.secondary,
    background: "rgba(0,0,0,0.5)",
    padding: "2px 8px",
    borderRadius: radii.sm,
  },
};
