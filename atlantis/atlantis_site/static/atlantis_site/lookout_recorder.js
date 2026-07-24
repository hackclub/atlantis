/*
 * Lookout browser recorder for Atlantis.
 *
 * Implements the client responsibilities from the Lookout integration guide:
 *   - serial per-capture pipeline (upload-url -> R2 PUT -> confirm), each leg
 *     retried 3x with exponential backoff (2s/4s/8s); 409 is terminal.
 *   - credit mode: capturedAt stamped at frame-grab time, sent on every
 *     upload-url request.
 *   - cadence driven by the server's nextExpectedAt, never a fixed setInterval.
 *   - honors 429 Retry-After.
 *   - clock-skew detection against the server's serverTime.
 *   - server-authoritative trackedSeconds, displayed with a 60s-capped local
 *     interpolation so the timer can never overshoot the next credit.
 *   - session recovery after refresh via GET /api/sessions/:token.
 *   - clientInfo telemetry on every upload-url request.
 *   - never fails silently; never logs the session token in full.
 *
 * Config is provided by the template via window.LOOKOUT_CONFIG.
 */
(function () {
	"use strict";

	const cfg = window.LOOKOUT_CONFIG || {};
	const BASE = (cfg.baseUrl || "").replace(/\/+$/, "");
	const TOKEN = cfg.token;
	const SESSION_ID = cfg.sessionId;
	const APP_NAME = cfg.appName || "Atlantis";
	const SYNC_URL = cfg.syncUrl;
	const CSRF = cfg.csrfToken;

	const INTERVAL_S = 60; // capture interval / interpolation cap
	const BACKOFF_MS = [2000, 4000, 8000];
	const MAX_JPEG_BYTES = 2 * 1024 * 1024;
	const CLOCK_SKEW_WARN_MS = 4 * 60 * 1000; // warn before the server's ±5min hard reject

	// --- DOM ----------------------------------------------------------------
	const el = (id) => document.getElementById(id);
	const ui = {
		status: el("lookout-status"),
		timer: el("lookout-timer"),
		mode: el("lookout-mode"),
		log: el("lookout-log"),
		start: el("lookout-start"),
		pause: el("lookout-pause"),
		resume: el("lookout-resume"),
		stop: el("lookout-stop"),
		video: el("lookout-video"),
		reshare: el("lookout-reshare"),
	};

	// --- logging (visible + console; NEVER the token) -----------------------
	function logLine(level, msg) {
		const line = document.createElement("div");
		line.className = "lookout-log-" + level;
		const ts = new Date().toLocaleTimeString();
		line.textContent = `[${ts}] ${msg}`;
		if (ui.log) {
			ui.log.appendChild(line);
			ui.log.scrollTop = ui.log.scrollHeight;
		}
		return line;
	}
	function info(msg) { logLine("info", msg); console.info("[lookout]", msg); }
	function warn(msg) { logLine("warn", msg); console.warn("[lookout]", msg); }
	function error(msg) { logLine("error", msg); console.error("[lookout]", msg); }

	// --- small helpers ------------------------------------------------------
	const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
	const nowMs = () => Date.now();

	class TerminalError extends Error {}

	function buildClientInfo() {
		// Lookout <Type> [(<EmbeddedApp>)]/<version> (<OS>[; <Browser> <version>])
		const ua = navigator.userAgent || "";
		let os = "Unknown OS";
		if (/Windows NT 10/.test(ua)) os = "Windows 10";
		else if (/Windows/.test(ua)) os = "Windows";
		else if (/Mac OS X ([0-9_]+)/.test(ua)) os = "macOS " + RegExp.$1.replace(/_/g, ".");
		else if (/Mac/.test(ua)) os = "macOS";
		else if (/Android/.test(ua)) os = "Android";
		else if (/Linux/.test(ua)) os = "Linux";
		let browser = "";
		let m;
		if ((m = ua.match(/Firefox\/([\d.]+)/))) browser = "Firefox " + m[1];
		else if ((m = ua.match(/Edg\/([\d.]+)/))) browser = "Edge " + m[1];
		else if ((m = ua.match(/Chrome\/([\d.]+)/))) browser = "Chrome " + m[1];
		else if ((m = ua.match(/Version\/([\d.]+).*Safari/))) browser = "Safari " + m[1];
		const osPart = browser ? `${os}; ${browser}` : os;
		return `Lookout Web (${APP_NAME})/1.0 (${osPart})`;
	}
	const CLIENT_INFO = buildClientInfo();

	// --- recording state ----------------------------------------------------
	let stream = null;
	let video = null;
	let recording = false;
	let loopTimer = null;
	let lastCapturedAtMs = 0; // enforce strictly-monotonic capturedAt

	// --- timer (server-authoritative + capped interpolation) ----------------
	let baseSeconds = 0;
	let lastSyncMs = nowMs();
	let tickId = null;

	function formatTime(total) {
		total = Math.max(0, Math.floor(total));
		const h = Math.floor(total / 3600);
		const m = Math.floor((total % 3600) / 60);
		const s = total % 60;
		return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
	}
	function onServerTrackedSeconds(serverTracked) {
		// Ratchet forward — never let a stale/idempotent read drag the timer back.
		if (typeof serverTracked === "number" && serverTracked > baseSeconds) {
			baseSeconds = serverTracked;
			lastSyncMs = nowMs();
		}
	}
	function getDisplaySeconds() {
		const elapsedS = Math.floor((nowMs() - lastSyncMs) / 1000);
		return baseSeconds + Math.min(INTERVAL_S, elapsedS);
	}
	function renderTimer() {
		if (ui.timer) ui.timer.textContent = formatTime(getDisplaySeconds());
	}
	function startTicking() {
		if (tickId) return;
		tickId = setInterval(renderTimer, 1000);
	}
	function stopTicking(snapToBase) {
		if (tickId) { clearInterval(tickId); tickId = null; }
		if (snapToBase && ui.timer) ui.timer.textContent = formatTime(baseSeconds);
	}

	// --- clock skew ---------------------------------------------------------
	function checkClockSkew(serverTimeIso) {
		if (!serverTimeIso) return;
		const serverMs = Date.parse(serverTimeIso);
		if (isNaN(serverMs)) return;
		const drift = Math.abs(serverMs - nowMs());
		if (drift > CLOCK_SKEW_WARN_MS) {
			warn(`Your device clock is off by ~${Math.round(drift / 1000)}s vs the server. `
				+ `Captures may be rejected — please fix your system clock.`);
		}
	}

	// --- retrying fetch -----------------------------------------------------
	// doFetch() must return a Promise<Response>. Retries transient failures with
	// exponential backoff; 409 is terminal; 429 waits Retry-After.
	async function requestWithRetry(label, doFetch) {
		let attempt = 0;
		let rateLimitHits = 0;
		while (true) {
			let res;
			try {
				res = await doFetch();
			} catch (netErr) {
				if (attempt >= BACKOFF_MS.length) {
					throw new Error(`${label}: network error after retries: ${netErr.message}`);
				}
				warn(`${label}: network error, retry in ${BACKOFF_MS[attempt] / 1000}s`);
				await sleep(BACKOFF_MS[attempt++]);
				continue;
			}

			if (res.ok) return res;

			if (res.status === 409) {
				// session paused/stopped — terminal, not retriable.
				throw new TerminalError(`${label}: session no longer accepting uploads (409)`);
			}

			if (res.status === 429) {
				const retryAfter = parseInt(res.headers.get("Retry-After") || "0", 10);
				const waitMs = (retryAfter > 0 ? retryAfter : 5) * 1000;
				warn(`${label}: rate limited (429), backing off ${waitMs / 1000}s`);
				await sleep(waitMs);
				if (++rateLimitHits > 5) throw new Error(`${label}: persistently rate limited (429)`);
				continue;
			}

			// other error — log status + endpoint + body, then retry/give up.
			let body = "";
			try { body = (await res.text()).slice(0, 300); } catch (_) {}
			if (attempt >= BACKOFF_MS.length) {
				throw new Error(`${label} failed: HTTP ${res.status} ${body}`);
			}
			warn(`${label}: HTTP ${res.status} ${body} — retry in ${BACKOFF_MS[attempt] / 1000}s`);
			await sleep(BACKOFF_MS[attempt++]);
		}
	}

	// --- screen capture -----------------------------------------------------
	async function startShare() {
		stream = await navigator.mediaDevices.getDisplayMedia({
			video: { width: { max: 1920 }, height: { max: 1080 }, frameRate: { ideal: 1 } },
			audio: false,
		});
		stream.getVideoTracks()[0].addEventListener("ended", onShareStopped);

		video = document.createElement("video");
		video.srcObject = stream;
		video.muted = true;
		await video.play();
	}

	function stopShare() {
		if (stream) {
			stream.getTracks().forEach((t) => t.stop());
			stream = null;
		}
		video = null;
	}

	function captureScreenshot() {
		const canvas = document.createElement("canvas");
		const scale = Math.min(1920 / video.videoWidth, 1080 / video.videoHeight, 1);
		canvas.width = Math.round(video.videoWidth * scale);
		canvas.height = Math.round(video.videoHeight * scale);
		canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
		return new Promise((resolve) => {
			canvas.toBlob(
				(blob) => resolve({ blob, width: canvas.width, height: canvas.height }),
				"image/jpeg",
				0.85
			);
		});
	}

	// --- one capture pipeline (serial, awaited end-to-end) ------------------
	// Returns nextExpectedAt (ISO string) from the confirm response.
	async function captureOnce() {
		// Stamp capturedAt at the moment we grab the frame; keep strictly monotonic.
		let capturedMs = nowMs();
		if (capturedMs <= lastCapturedAtMs) capturedMs = lastCapturedAtMs + 1;
		lastCapturedAtMs = capturedMs;
		const capturedAt = new Date(capturedMs).toISOString();

		const { blob, width, height } = await captureScreenshot();
		if (blob.size > MAX_JPEG_BYTES) {
			warn(`Screenshot ${(blob.size / 1024 / 1024).toFixed(1)}MB exceeds 2MB limit; skipping.`);
			return null;
		}

		// 1. upload-url (credit mode via capturedAt; clientInfo telemetry)
		const uploadUrlEndpoint =
			`${BASE}/api/sessions/${TOKEN}/upload-url`
			+ `?capturedAt=${encodeURIComponent(capturedAt)}`
			+ `&clientInfo=${encodeURIComponent(CLIENT_INFO)}`;
		const uploadUrlRes = await requestWithRetry("upload-url", () => fetch(uploadUrlEndpoint));
		const { uploadUrl, screenshotId, nextExpectedAt, trackingMode, serverTime } =
			await uploadUrlRes.json();
		checkClockSkew(serverTime);
		if (trackingMode && ui.mode) ui.mode.textContent = `tracking: ${trackingMode}`;

		// 2. PUT the blob to the presigned R2 URL
		await requestWithRetry("r2-put", () => fetch(uploadUrl, {
			method: "PUT",
			headers: { "Content-Type": "image/jpeg" },
			body: blob,
		}));

		// 3. confirm (idempotent — safe to retry)
		const confirmRes = await requestWithRetry("confirm", () =>
			fetch(`${BASE}/api/sessions/${TOKEN}/screenshots`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ screenshotId, width, height, fileSize: blob.size }),
			}));
		const confirm = await confirmRes.json();
		checkClockSkew(confirm.serverTime);
		onServerTrackedSeconds(confirm.trackedSeconds);

		return confirm.nextExpectedAt;
	}

	// --- capture loop -------------------------------------------------------
	async function loop() {
		if (!recording) return;
		let nextExpectedAt = null;
		try {
			nextExpectedAt = await captureOnce();
		} catch (e) {
			if (e instanceof TerminalError) {
				error(e.message + " — reconciling with server.");
				recording = false;
				await syncBackend();
				await recoverFromServer();
				return;
			}
			// Loud, visible failure — but keep trying on the next interval.
			error(`Capture failed: ${e.message}`);
		}
		if (!recording) return;
		let delay = INTERVAL_S * 1000;
		if (nextExpectedAt) {
			const parsed = Date.parse(nextExpectedAt);
			if (!isNaN(parsed)) delay = Math.max(0, parsed - nowMs());
		}
		loopTimer = setTimeout(loop, delay);
	}

	function startLoop() {
		recording = true;
		startTicking();
		loop();
	}
	function stopLoop() {
		recording = false;
		if (loopTimer) { clearTimeout(loopTimer); loopTimer = null; }
	}

	// --- backend sync -------------------------------------------------------
	async function syncBackend() {
		if (!SYNC_URL) return null;
		try {
			const res = await fetch(SYNC_URL, {
				method: "POST",
				headers: { "X-CSRFToken": CSRF },
			});
			if (!res.ok) { warn(`backend sync returned ${res.status}`); return null; }
			return await res.json();
		} catch (e) {
			warn(`backend sync failed: ${e.message}`);
			return null;
		}
	}

	// --- session control (pause/resume/stop) --------------------------------
	async function postControl(action) {
		return requestWithRetry(action, () =>
			fetch(`${BASE}/api/sessions/${TOKEN}/${action}`, { method: "POST" }));
	}

	// --- UI state application -----------------------------------------------
	function setButtons({ start, pause, resume, stop }) {
		const set = (btn, on) => { if (btn) btn.style.display = on ? "" : "none"; };
		set(ui.start, start);
		set(ui.pause, pause);
		set(ui.resume, resume);
		set(ui.stop, stop);
	}
	function setStatus(text) { if (ui.status) ui.status.textContent = text; }

	function showVideo() {
		if (!ui.video) return;
		// Permanent media redirect — safe to embed (presigned URLs expire).
		ui.video.innerHTML = "";
		const v = document.createElement("video");
		v.controls = true;
		v.src = `${BASE}/api/media/${SESSION_ID}/video.mp4`;
		v.poster = `${BASE}/api/media/${SESSION_ID}/thumbnail.jpg`;
		v.style.maxWidth = "100%";
		ui.video.appendChild(v);
	}

	// Poll compilation status until it resolves.
	let statusPollTimer = null;
	async function pollCompilation() {
		if (statusPollTimer) clearTimeout(statusPollTimer);
		try {
			const res = await fetch(`${BASE}/api/sessions/${TOKEN}/status`);
			if (res.ok) {
				const data = await res.json();
				const st = data.status;
				setStatus(`Status: ${st}`);
				if (st === "complete") {
					await syncBackend();
					setStatus("Timelapse ready!");
					showVideo();
					return;
				}
				if (st === "failed") {
					error("Compilation failed. An organizer can trigger a recompile.");
					await syncBackend();
					return;
				}
			} else {
				warn(`status poll returned ${res.status}`);
			}
		} catch (e) {
			warn(`status poll failed: ${e.message}`);
		}
		statusPollTimer = setTimeout(pollCompilation, 5000);
	}

	// Apply a status string to the whole UI (used on load + after transitions).
	function applyStatus(status, tracked, totalActive) {
		if (typeof tracked === "number") { baseSeconds = tracked; lastSyncMs = nowMs(); renderTimer(); }
		setStatus(`Status: ${status}`);
		switch (status) {
			case "pending":
				setButtons({ start: true, pause: false, resume: false, stop: false });
				break;
			case "active":
				setButtons({ start: false, pause: true, resume: false, stop: true });
				// The screen-share MediaStream doesn't survive a refresh — prompt to re-share.
				if (!stream) {
					setButtons({ start: false, pause: true, resume: false, stop: true });
					if (ui.reshare) ui.reshare.style.display = "";
					setStatus("Recording is active — re-share your screen to continue.");
				}
				break;
			case "paused":
				setButtons({ start: false, pause: false, resume: true, stop: true });
				break;
			case "stopped":
			case "compiling":
				setButtons({ start: false, pause: false, resume: false, stop: false });
				pollCompilation();
				break;
			case "complete":
				setButtons({ start: false, pause: false, resume: false, stop: false });
				stopTicking(true);
				showVideo();
				break;
			case "failed":
				setButtons({ start: false, pause: false, resume: false, stop: false });
				error("This session failed to compile.");
				break;
			default:
				setButtons({ start: true, pause: false, resume: false, stop: false });
		}
	}

	async function recoverFromServer() {
		try {
			const res = await fetch(`${BASE}/api/sessions/${TOKEN}`);
			if (!res.ok) { warn(`session recovery returned ${res.status}`); return; }
			const data = await res.json();
			applyStatus(data.status, data.trackedSeconds, data.totalActiveSeconds);
		} catch (e) {
			error(`Could not load session status: ${e.message}`);
		}
	}

	// --- event handlers -----------------------------------------------------
	function onShareStopped() {
		if (!recording) return;
		warn("Screen sharing stopped. Pausing capture — resume and re-share to continue.");
		stopLoop();
		stopShare();
		if (ui.reshare) ui.reshare.style.display = "";
		setButtons({ start: false, pause: false, resume: true, stop: true });
		stopTicking(false);
	}

	async function onStart() {
		try {
			await startShare();
		} catch (e) {
			error(`Screen share was not granted: ${e.message}`);
			return;
		}
		info("Screen shared. Recording started.");
		if (ui.reshare) ui.reshare.style.display = "none";
		setButtons({ start: false, pause: true, resume: false, stop: true });
		setStatus("Status: recording");
		startLoop();
	}

	async function onReshare() {
		try {
			await startShare();
		} catch (e) {
			error(`Screen share was not granted: ${e.message}`);
			return;
		}
		info("Screen re-shared. Resuming capture.");
		if (ui.reshare) ui.reshare.style.display = "none";
		setButtons({ start: false, pause: true, resume: false, stop: true });
		try { await postControl("resume"); } catch (e) { warn(`resume failed: ${e.message}`); }
		startLoop();
	}

	async function onPause() {
		stopLoop();
		stopTicking(false);
		try {
			await postControl("pause");
			info("Paused.");
		} catch (e) {
			error(`Pause failed: ${e.message}`);
		}
		stopShare();
		if (ui.reshare) ui.reshare.style.display = "none";
		setButtons({ start: false, pause: false, resume: true, stop: true });
		setStatus("Status: paused");
		await syncBackend();
	}

	async function onResume() {
		// Resuming requires a fresh screen share (the old stream is gone).
		if (!stream) { await onReshare(); return; }
		try {
			await postControl("resume");
			info("Resumed.");
		} catch (e) {
			error(`Resume failed: ${e.message}`);
			return;
		}
		setButtons({ start: false, pause: true, resume: false, stop: true });
		setStatus("Status: recording");
		startLoop();
	}

	async function onStop() {
		stopLoop();
		try {
			const res = await postControl("stop");
			const data = await res.json().catch(() => ({}));
			// The /stop response carries the final committed trackedSeconds.
			if (typeof data.trackedSeconds === "number") {
				baseSeconds = data.trackedSeconds;
				lastSyncMs = nowMs();
			}
			info("Stopped. Compiling your timelapse…");
		} catch (e) {
			error(`Stop failed: ${e.message}`);
		}
		stopShare();
		stopTicking(true);
		if (ui.reshare) ui.reshare.style.display = "none";
		setButtons({ start: false, pause: false, resume: false, stop: false });
		setStatus("Status: compiling");
		await syncBackend();
		pollCompilation();
	}

	// --- init ---------------------------------------------------------------
	function init() {
		if (!BASE || !TOKEN || !SESSION_ID) {
			error("Recorder is misconfigured (missing Lookout config).");
			return;
		}
		if (ui.start) ui.start.addEventListener("click", onStart);
		if (ui.pause) ui.pause.addEventListener("click", onPause);
		if (ui.resume) ui.resume.addEventListener("click", onResume);
		if (ui.stop) ui.stop.addEventListener("click", onStop);
		if (ui.reshare) ui.reshare.addEventListener("click", onReshare);
		info(`Client: ${CLIENT_INFO}`);
		// Recover current session state (handles page refresh).
		recoverFromServer();
	}

	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", init);
	} else {
		init();
	}
})();
