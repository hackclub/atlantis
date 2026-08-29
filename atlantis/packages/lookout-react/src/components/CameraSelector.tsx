import React from "react";
import { colors, spacing, fontSize, radii } from "../ui/theme.js";

export interface CameraSelectorProps {
  devices: MediaDeviceInfo[];
  selectedDeviceId: string | null;
  onSelect: (deviceId: string) => void;
  disabled?: boolean;
}

export function CameraSelector({
  devices,
  selectedDeviceId,
  onSelect,
  disabled,
}: CameraSelectorProps) {
  if (devices.length === 0) return null;

  return (
    <div style={{ marginBottom: spacing.md }}>
      <label
        style={{
          display: "block",
          fontSize: fontSize.sm,
          color: colors.text.secondary,
          marginBottom: spacing.xs,
        }}
      >
        Camera
      </label>
      <select
        value={selectedDeviceId ?? ""}
        onChange={(e) => onSelect(e.target.value)}
        disabled={disabled}
        style={{
          width: "100%",
          padding: `${spacing.sm}px ${spacing.md}px`,
          fontSize: fontSize.md,
          color: colors.text.primary,
          background: colors.bg.sunken,
          border: `1px solid ${colors.border.default}`,
          borderRadius: radii.md,
          outline: "none",
          cursor: disabled ? "not-allowed" : "pointer",
          opacity: disabled ? 0.5 : 1,
        }}
      >
        {devices.map((device, i) => (
          <option key={device.deviceId} value={device.deviceId}>
            {device.label || `Camera ${i + 1}`}
          </option>
        ))}
      </select>
    </div>
  );
}
