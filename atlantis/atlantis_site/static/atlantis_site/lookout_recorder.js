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
 * The recorder is a popup on the project page, so it is mounted on demand:
 * window.AtlantisLookout.boot(config) points it at a session and takes over
 * the markup, and can be called again for the next session in the same page
 * load. Config comes from the server (see the record_timelapse view).
 */
(function () {
	"use strict";

	// All set by boot(); nothing here talks to Lookout until it is called.
	let BASE = "";
	let TOKEN = null;
	let SESSION_ID = null;
	let APP_NAME = "Atlantis";
	let SYNC_URL = null;
	let CSRF = null;
	let SESSION_PK = null;

	const INTERVAL_S = 60; // capture interval / interpolation cap
	const BACKOFF_MS = [2000, 4000, 8000];
	const MAX_JPEG_BYTES = 2 * 1024 * 1024;
	const CLOCK_SKEW_WARN_MS = 4 * 60 * 1000; // warn before the server's ±5min hard reject
	const RECOVERY_TIMEOUT_MS = 10000; // cap on the initial session-status lookup

	// --- DOM ----------------------------------------------------------------
	// Looked up at boot rather than at load: the markup lives in a popup on the
	// project page, so the script can be anywhere on it.
	const el = (id) => document.getElementById(id);
	const ui = {};
	let DEFAULT_STAGE_TEXT = "";

	function mountUi() {
		[
			["pill", "lookout-pill"], ["hint", "lookout-hint"], ["timer", "lookout-timer"],
			["shots", "lookout-shots"], ["next", "lookout-next"], ["mode", "lookout-mode"],
			["log", "lookout-log"], ["start", "lookout-start"], ["pause", "lookout-pause"],
			["resume", "lookout-resume"], ["stop", "lookout-stop"], ["reshare", "lookout-reshare"],
			["preview", "lookout-preview"], ["stageEmpty", "lookout-stage-empty"],
			["badge", "lookout-badge"], ["flash", "lookout-flash"], ["result", "lookout-result"],
			["video", "lookout-video"], ["alert", "lookout-alert"],
			["alertText", "lookout-alert-text"], ["alertDismiss", "lookout-alert-dismiss"],
		].forEach(([key, id]) => { ui[key] = el(id); });
		DEFAULT_STAGE_TEXT = ui.stageEmpty ? ui.stageEmpty.textContent : "";
	}

	const PAGE_TITLE = document.title;

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
	function error(msg) {
		logLine("error", msg);
		console.error("[lookout]", msg);
		// Failures cost the user tracked time — say so where they'll actually see it.
		showAlert(msg);
	}

	// --- prominent, dismissible error banner ---------------------------------
	function showAlert(msg) {
		if (!ui.alert || !ui.alertText) return;
		ui.alertText.textContent = msg;
		ui.alert.hidden = false;
	}
	function hideAlert() {
		if (ui.alert) ui.alert.hidden = true;
	}

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
	// Built per boot — the app name only arrives with the session config.
	let CLIENT_INFO = "";

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
	let shotCount = 0;
	let nextCaptureAtMs = 0;

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
	function renderCountdown() {
		if (!ui.next) return;
		if (!recording || !nextCaptureAtMs) {
			ui.next.textContent = "Next one: —";
			return;
		}
		const left = Math.max(0, Math.round((nextCaptureAtMs - nowMs()) / 1000));
		ui.next.textContent = left > 0 ? `Next one in ${left}s` : "Taking one now…";
	}
	function renderTick() {
		renderTimer();
		renderCountdown();
	}
	function startTicking() {
		if (tickId) return;
		tickId = setInterval(renderTick, 1000);
	}
	function stopTicking(snapToBase) {
		if (tickId) { clearInterval(tickId); tickId = null; }
		if (snapToBase && ui.timer) ui.timer.textContent = formatTime(baseSeconds);
		nextCaptureAtMs = 0;
		renderCountdown();
	}

	// --- screenshot counter (ratcheted like the timer) ----------------------
	function setShotCount(n) {
		if (typeof n !== "number" || n < shotCount) return;
		shotCount = n;
		if (ui.shots) ui.shots.textContent = String(shotCount);
	}
	function bumpShotCount() {
		setShotCount(shotCount + 1);
	}
	function flashStage() {
		if (!ui.flash) return;
		ui.flash.classList.remove("is-flashing");
		void ui.flash.offsetWidth; // restart the animation
		ui.flash.classList.add("is-flashing");
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
	// The capture source doubles as the on-page preview, so the user can see
	// exactly what's being recorded and catch a wrong window immediately.
	function showPreview(on) {
		if (ui.preview) ui.preview.hidden = !on;
		if (ui.stageEmpty) ui.stageEmpty.hidden = on;
	}

	async function startShare() {
		stream = await navigator.mediaDevices.getDisplayMedia({
			video: { width: { max: 1920 }, height: { max: 1080 }, frameRate: { ideal: 1 } },
			audio: false,
		});
		stream.getVideoTracks()[0].addEventListener("ended", onShareStopped);

		video = ui.preview || document.createElement("video");
		video.srcObject = stream;
		video.muted = true;
		video.playsInline = true;
		await video.play();
		showPreview(true);
	}

	function stopShare() {
		if (stream) {
			stream.getTracks().forEach((t) => t.stop());
			stream = null;
		}
		if (ui.preview) ui.preview.srcObject = null;
		showPreview(false);
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
		if (trackingMode && ui.mode) {
			ui.mode.textContent = `Tracking mode: ${trackingMode}`;
			ui.mode.hidden = false;
		}

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
		if (typeof confirm.screenshotCount === "number") {
			setShotCount(confirm.screenshotCount);
		} else {
			bumpShotCount();
		}
		flashStage();
		hideAlert();

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
		nextCaptureAtMs = nowMs() + delay;
		renderCountdown();
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
		nextCaptureAtMs = 0;
		renderCountdown();
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
	// One place that says, for each state: what it's called, what the user
	// should do next, and which buttons that leaves them.
	const STATES = {
		loading: {
			label: "Loading…", tone: "",
			hint: "Checking where this session left off…",
			buttons: {},
		},
		idle: {
			label: "Not started", tone: "",
			hint: "When you hit start, your browser asks which screen or window to share — "
				+ "pick the one you'll be modelling in. Nothing is captured until you do.",
			buttons: { start: true },
		},
		live: {
			label: "Recording", tone: "live", dot: true,
			hint: "You're being recorded. Go work — just leave this tab open, and don't close "
				+ "the sharing bar your browser put up.",
			buttons: { pause: true, stop: true },
		},
		paused: {
			label: "Paused", tone: "paused",
			hint: "Paused — no time is being tracked right now. Resume when you're back; "
				+ "you'll be asked to share your screen again.",
			buttons: { resume: true, stop: true },
			stage: "Sharing is off while you're paused. Resume to see your screen here again.",
		},
		reshare: {
			label: "Screen sharing stopped", tone: "warn",
			hint: "This session is still open, but your browser is no longer sharing a screen, "
				+ "so nothing is being captured. Re-share to pick the clock back up.",
			buttons: { reshare: true, stop: true },
			stage: "Nothing is being captured — re-share your screen to start it back up.",
		},
		processing: {
			label: "Building your video", tone: "processing",
			hint: "Your screenshots are being stitched into a timelapse. This usually takes a "
				+ "minute or two — you can leave this page, it'll finish without you.",
			buttons: {},
			stage: "Recording finished. Stitching your screenshots together…",
		},
		done: {
			label: "Ready", tone: "done",
			hint: "All done. Your tracked time only counts once you tape this recording into "
				+ "a lapse.",
			buttons: {},
			stage: "This recording is finished — the video is below.",
		},
		failed: {
			label: "Failed", tone: "error",
			hint: "This session couldn't be turned into a video. Ask an organizer — they can "
				+ "retry the compile without you losing the recording.",
			buttons: {},
			stage: "This recording couldn't be turned into a video.",
		},
		unavailable: {
			label: "Not started", tone: "error",
			hint: "Nothing is recording — the error above happened before a session could be "
				+ "opened. Close this and try again.",
			buttons: {},
			stage: "Nothing to show — this recording never got going.",
		},
	};

	// Terminal states: the popup can be reopened on them, but the project page
	// should hand out a fresh session rather than this one.
	const OVER = { done: true, failed: true, unavailable: true };
	let finished = false;

	function setButtons(buttons) {
		const set = (btn, on) => { if (btn) btn.hidden = !on; };
		set(ui.start, buttons.start);
		set(ui.pause, buttons.pause);
		set(ui.resume, buttons.resume);
		set(ui.stop, buttons.stop);
		set(ui.reshare, buttons.reshare);
	}

	function setState(name) {
		const state = STATES[name] || STATES.idle;
		finished = Boolean(OVER[name]);
		if (ui.pill) {
			ui.pill.className = "tl-pill" + (state.tone ? ` tl-pill--${state.tone}` : "");
			ui.pill.textContent = "";
			if (state.dot) {
				const dot = document.createElement("span");
				dot.className = "tl-dot";
				ui.pill.appendChild(dot);
			}
			ui.pill.appendChild(document.createTextNode(state.label));
		}
		if (ui.hint) ui.hint.textContent = state.hint;
		if (ui.stageEmpty) ui.stageEmpty.textContent = state.stage || DEFAULT_STAGE_TEXT;
		setButtons(state.buttons);
		if (ui.badge) ui.badge.hidden = name !== "live";
		// Make the recording state visible from the tab strip, since the whole
		// point is that the user is off working in another window.
		document.title = name === "live" ? `● Recording — ${PAGE_TITLE}` : PAGE_TITLE;
	}

	function showVideo() {
		if (ui.result) ui.result.hidden = false;
		if (!ui.video) return;
		// Permanent media redirect — safe to embed (presigned URLs expire).
		ui.video.innerHTML = "";
		const v = document.createElement("video");
		v.controls = true;
		v.src = `${BASE}/api/media/${SESSION_ID}/video.mp4`;
		v.poster = `${BASE}/api/media/${SESSION_ID}/thumbnail.jpg`;
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
				if (st === "complete") {
					await syncBackend();
					setState("done");
					stopTicking(true);
					showVideo();
					return;
				}
				if (st === "failed") {
					setState("failed");
					error("Compilation failed. An organizer can trigger a recompile.");
					await syncBackend();
					return;
				}
				setState("processing");
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
		switch (status) {
			case "pending":
				setState("idle");
				break;
			case "active":
				// The screen-share MediaStream doesn't survive a refresh — prompt to re-share.
				setState(stream ? "live" : "reshare");
				break;
			case "paused":
				setState("paused");
				break;
			case "stopped":
			case "compiling":
				setState("processing");
				pollCompilation();
				break;
			case "complete":
				setState("done");
				stopTicking(true);
				showVideo();
				break;
			case "failed":
				setState("failed");
				error("This session failed to compile.");
				break;
			default:
				setState("idle");
		}
	}

	async function recoverFromServer() {
		// Bounded, so a hanging request can't strand the page on "Loading…" —
		// aborting rejects the fetch and drops us into the catch below.
		const abort = new AbortController();
		const timeout = setTimeout(() => abort.abort(), RECOVERY_TIMEOUT_MS);
		try {
			const res = await fetch(`${BASE}/api/sessions/${TOKEN}`, { signal: abort.signal });
			if (!res.ok) {
				warn(`session recovery returned ${res.status}`);
				// Don't strand the user on a button-less page — starting a share
				// works for a pending or an already-active session either way.
				setState("idle");
				return;
			}
			const data = await res.json();
			setShotCount(data.screenshotCount);
			applyStatus(data.status, data.trackedSeconds, data.totalActiveSeconds);
		} catch (e) {
			setState("idle");
			error(e.name === "AbortError"
				? "Couldn't reach Lookout to check this session — you can still try starting a recording."
				: `Could not load session status: ${e.message}`);
		} finally {
			clearTimeout(timeout);
		}
	}

	// --- event handlers -----------------------------------------------------
	function onShareStopped() {
		if (!recording) return;
		stopLoop();
		stopShare();
		stopTicking(false);
		setState("reshare");
		warn("Screen sharing stopped — capture is on hold until you re-share.");
	}

	async function onStart() {
		try {
			await startShare();
		} catch (e) {
			error(`Screen share was not granted: ${e.message}`);
			return;
		}
		hideAlert();
		info("Screen shared. Recording started.");
		setState("live");
		startLoop();
	}

	async function onReshare() {
		try {
			await startShare();
		} catch (e) {
			error(`Screen share was not granted: ${e.message}`);
			return;
		}
		hideAlert();
		info("Screen re-shared. Resuming capture.");
		setState("live");
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
		setState("paused");
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
		setState("live");
		startLoop();
	}

	async function onStop() {
		if (!window.confirm(
			"Finish this timelapse? You can't add more time to it afterwards — "
			+ "record a new one for your next session."
		)) return;
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
		setState("processing");
		await syncBackend();
		pollCompilation();
	}

	// --- mount / boot -------------------------------------------------------
	let wired = false;

	function wire() {
		mountUi();
		if (wired) return;
		wired = true;
		if (ui.start) ui.start.addEventListener("click", onStart);
		if (ui.pause) ui.pause.addEventListener("click", onPause);
		if (ui.resume) ui.resume.addEventListener("click", onResume);
		if (ui.stop) ui.stop.addEventListener("click", onStop);
		if (ui.reshare) ui.reshare.addEventListener("click", onReshare);
		if (ui.alertDismiss) ui.alertDismiss.addEventListener("click", hideAlert);

		// Closing the tab mid-recording silently stops the clock — warn first.
		// Closing the popup is harmless by comparison: the recorder keeps going.
		window.addEventListener("beforeunload", (e) => {
			if (!recording) return;
			e.preventDefault();
			e.returnValue = "";
		});
	}

	// Wipe the previous session out of the markup and the module, so a second
	// recording in the same page load can't inherit its clock, shots or log.
	function clearSession() {
		stopLoop();
		stopTicking(false);
		if (statusPollTimer) { clearTimeout(statusPollTimer); statusPollTimer = null; }
		stopShare();
		BASE = "";
		TOKEN = null;
		SESSION_ID = null;
		SYNC_URL = null;
		SESSION_PK = null;
		baseSeconds = 0;
		lastSyncMs = nowMs();
		shotCount = 0;
		lastCapturedAtMs = 0;
		if (ui.timer) ui.timer.textContent = formatTime(0);
		if (ui.shots) ui.shots.textContent = "0";
		if (ui.mode) { ui.mode.textContent = ""; ui.mode.hidden = true; }
		if (ui.log) ui.log.replaceChildren();
		if (ui.result) ui.result.hidden = true;
		if (ui.video) ui.video.replaceChildren();
		hideAlert();
	}

	// Point the recorder at a session and pick up wherever it left off.
	function boot(config) {
		wire();
		clearSession();
		BASE = (config.baseUrl || "").replace(/\/+$/, "");
		TOKEN = config.token || null;
		SESSION_ID = config.sessionId || null;
		APP_NAME = config.appName || "Atlantis";
		SYNC_URL = config.syncUrl || null;
		CSRF = config.csrfToken || null;
		SESSION_PK = config.sessionPk != null ? String(config.sessionPk) : null;
		CLIENT_INFO = buildClientInfo();

		if (!BASE || !TOKEN || !SESSION_ID) {
			setState("unavailable");
			error("Recorder is misconfigured (missing Lookout config).");
			return;
		}
		setState("loading");
		info(`Client: ${CLIENT_INFO}`);
		// Recover current session state (handles a reopened popup or a refresh).
		recoverFromServer();
	}

	// The session couldn't be fetched at all — say so in the recorder itself,
	// which is where the user is looking.
	function fail(msg) {
		wire();
		clearSession();
		setState("unavailable");
		error(msg);
	}

	window.AtlantisLookout = {
		boot,
		fail,
		// A clean slate to open the popup on while the page fetches a session.
		standby: () => { wire(); clearSession(); setState("loading"); },
		// The session still worth reopening, if there is one. The project page
		// asks before starting a second recording on top of a live one.
		liveSessionPk: () => (TOKEN && !finished ? SESSION_PK : null),
	};
})();
