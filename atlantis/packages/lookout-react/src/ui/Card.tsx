import React from "react";
import { motion } from "motion/react";
import { colors, radii } from "./theme.js";

export interface CardProps {
  children: React.ReactNode;
  onClick?: () => void;
  padding?: number | string;
  style?: React.CSSProperties;
}

export function Card({ children, onClick, padding, style }: CardProps) {
  const content = (
    <div
      style={{
        background: colors.bg.surface,
        border: `1px solid ${colors.border.default}`,
        borderRadius: radii.lg,
        overflow: "hidden",
        padding,
        height: "100%",
        display: "flex",
        flexDirection: "column",
        ...(onClick ? {} : style),
      }}
    >
      {children}
    </div>
  );

  if (!onClick) {
    return content;
  }

  return (
    <motion.div
      onClick={onClick}
      role="button"
      tabIndex={0}
      whileTap={{ scale: 0.99 }}
      transition={{ type: "spring", stiffness: 1500, damping: 60 }}
      style={{
        cursor: "pointer",
        display: "block",
        height: "100%",
        ...style,
      }}
    >
      {content}
    </motion.div>
  );
}
