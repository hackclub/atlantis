import type { ClientInfo } from "./types.js";
/**
 * Pieces of client telemetry. `type` and `version` are required; the rest are
 * optional and surface-specific (browser fields are web/SDK only).
 */
export interface ClientInfoParts {
    /** "desktop" | "web" | "sdk" (extensible). */
    type: string;
    /** Lookout client version, e.g. "0.2.6". */
    version: string;
    /** Web/SDK only: host program embedding Lookout, e.g. "Fallout". */
    embeddedApp?: string;
    osType?: string;
    osVersion?: string;
    browserType?: string;
    browserVersion?: string;
}
/**
 * Format the parts into a User-Agent-like `clientInfo` string, e.g.
 *   "Lookout Desktop/0.2.6 (macOS 14.3)"
 *   "Lookout Web (Fallout)/0.2.6 (macOS 14.3; Chrome 120.0)"
 * The server stores this opaquely; the format is a convention, not enforced.
 */
export declare function formatClientInfo(parts: ClientInfoParts): ClientInfo;
/** Best-effort browser family + version from a User-Agent string. Heuristic;
 *  order matters (Edge/Opera masquerade as Chrome). Returns {} if unknown. */
export declare function detectBrowser(ua: string): {
    browserType?: string;
    browserVersion?: string;
};
/** Best-effort OS family + version from a User-Agent string. Returns {} if
 *  unknown. Version is coarse (and unavailable for recent macOS, which freezes
 *  the UA at 10_15_7). */
export declare function detectOS(ua: string): {
    osType?: string;
    osVersion?: string;
};
/**
 * Build a `clientInfo` string for a browser-based client (web app / SDK),
 * auto-detecting browser and OS from `navigator.userAgent`. Pass the Lookout
 * `type`, `version`, and optional `embeddedApp` (the host program).
 */
export declare function buildBrowserClientInfo(opts: {
    type: string;
    version: string;
    embeddedApp?: string;
}): ClientInfo;
//# sourceMappingURL=clientInfo.d.ts.map