/*
 * The Lapse picker: the popup a shipper chooses recorded timelapses in.
 *
 * The list is read from Lapse every time it is shown and every time the refresh
 * button is pressed, which is the whole point of the thing — somebody who
 * publishes a timelapse while the book is open should be able to tape it in
 * without reloading the page. Nothing about it is rendered server-side.
 *
 * The checkboxes carry form="new-lapse", so they submit with the compose form
 * even though the popup lives outside it in the DOM. They send Lapse's own ids;
 * the tracked time on each one is re-read from the API server-side when the
 * lapse is written, so the numbers drawn here are for the shipper's benefit and
 * are never what gets recorded.
 *
 * window.AtlantisLapsePicker.boot(config) mounts it. selected() and
 * onChange(fn) are how the compose line keeps its running total in step with a
 * list that can be replaced under it.
 */
(function () {
	"use strict";

	let URL_ = null;
	let CONNECT_URL = null;
	let CSRF = null;
	let NEXT_URL = "";

	const ui = {};
	// Ids the shipper has ticked. Held here rather than read off the DOM so a
	// refresh that replaces every row keeps the selection it had.
	const chosen = new Set();
	// Everything the last fetch returned, by id, so selected() can still price
	// a row after a re-render.
	let byId = new Map();
	let listeners = [];
	let loading = false;
	let loaded = false;

	function wire() {
		ui.root = document.getElementById("lapse-picker");
		ui.list = document.getElementById("lapse-list");
		ui.status = document.getElementById("lapse-status");
		ui.refresh = document.getElementById("lapse-refresh");
		ui.account = document.getElementById("lapse-account");
	}

	function notify() {
		listeners.forEach(function (fn) {
			try {
				fn();
			} catch (e) {
				/* a listener that throws must not stop the rest */
			}
		});
	}

	function formatDuration(seconds) {
		const total = Math.max(parseInt(seconds, 10) || 0, 0);
		const hours = Math.floor(total / 3600);
		const minutes = Math.floor((total % 3600) / 60);
		return hours + "h " + minutes + "m";
	}

	function formatWhen(epochMillis) {
		if (!epochMillis) return "";
		const when = new Date(Number(epochMillis));
		if (isNaN(when.getTime())) return "";
		return when.toLocaleString(undefined, {
			month: "short",
			day: "numeric",
			hour: "numeric",
			minute: "2-digit",
		});
	}

	function setStatus(text, kind) {
		if (!ui.status) return;
		ui.status.textContent = text || "";
		ui.status.hidden = !text;
		ui.status.className = "lapse-status" + (kind ? " is-" + kind : "");
	}

	/* Replace the status area with real controls — a form, so connecting is a
	   POST with a CSRF token and not a link somebody else's page can follow. */
	function setConnectPrompt(message, label) {
		if (!ui.status) return;
		ui.status.hidden = false;
		ui.status.className = "lapse-status is-connect";
		ui.status.replaceChildren();

		const line = document.createElement("p");
		line.textContent = message;
		ui.status.appendChild(line);

		const form = document.createElement("form");
		form.method = "post";
		form.action = CONNECT_URL;

		const token = document.createElement("input");
		token.type = "hidden";
		token.name = "csrfmiddlewaretoken";
		token.value = CSRF;
		form.appendChild(token);

		const next = document.createElement("input");
		next.type = "hidden";
		next.name = "next";
		next.value = NEXT_URL;
		form.appendChild(next);

		const button = document.createElement("button");
		button.type = "submit";
		button.className = "ink-btn ink-btn--strong";
		button.textContent = label;
		form.appendChild(button);

		ui.status.appendChild(form);
	}

	// Why a row can't be picked, or "" when it can.
	const BLOCKED = {
		attached: "already taped into a lapse",
		processing: "still processing on Lapse",
		failed: "Lapse couldn't process this one",
	};

	function row(item) {
		const li = document.createElement("li");
		const blocked = BLOCKED[item.state] || "";
		li.className = "lapse-row" + (blocked ? " is-blocked" : "");

		const label = document.createElement("label");

		const box = document.createElement("input");
		box.type = "checkbox";
		box.name = "timelapses";
		box.value = item.id;
		// The compose form is elsewhere in the document; this is what puts the
		// selection into its submission anyway.
		box.setAttribute("form", "new-lapse");
		box.disabled = Boolean(blocked);
		box.checked = !blocked && chosen.has(item.id);
		box.addEventListener("change", function () {
			if (box.checked) chosen.add(item.id);
			else chosen.delete(item.id);
			notify();
		});
		label.appendChild(box);

		if (item.thumbnailUrl) {
			const thumb = document.createElement("img");
			thumb.className = "lapse-thumb";
			thumb.src = item.thumbnailUrl;
			thumb.alt = "";
			thumb.loading = "lazy";
			label.appendChild(thumb);
		}

		const body = document.createElement("span");
		body.className = "lapse-body";

		const name = document.createElement("strong");
		name.className = "lapse-name";
		name.textContent = item.name;
		body.appendChild(name);

		const meta = document.createElement("span");
		meta.className = "dim";
		const when = formatWhen(item.recordedAt);
		meta.textContent =
			(item.trackedDisplay || formatDuration(item.trackedSeconds)) +
			(when ? " · " + when : "") +
			(blocked ? " · " + blocked : "");
		body.appendChild(meta);

		label.appendChild(body);
		li.appendChild(label);

		if (item.watchUrl) {
			const watch = document.createElement("a");
			watch.href = item.watchUrl;
			watch.target = "_blank";
			watch.rel = "noreferrer noopener";
			watch.textContent = "watch";
			li.appendChild(watch);
		}

		return li;
	}

	function render(items) {
		byId = new Map();
		items.forEach(function (item) {
			byId.set(item.id, item);
		});

		// Anything ticked that is no longer pickable — taped in from another tab,
		// say — is dropped from the selection rather than silently submitted.
		Array.from(chosen).forEach(function (id) {
			const item = byId.get(id);
			if (!item || BLOCKED[item.state]) chosen.delete(id);
		});

		ui.list.replaceChildren();
		items.forEach(function (item) {
			ui.list.appendChild(row(item));
		});
		notify();
	}

	function showAccount(account) {
		if (!ui.account) return;
		const handle = account && (account.handle || account.displayName);
		ui.account.textContent = handle ? "@" + handle : "";
		ui.account.hidden = !handle;
	}

	function load() {
		if (loading) return;
		loading = true;
		if (ui.refresh) ui.refresh.disabled = true;
		setStatus(loaded ? "Refreshing…" : "Reading your timelapses from Lapse…");

		fetch(URL_, {
			headers: { "X-Requested-With": "XMLHttpRequest" },
			credentials: "same-origin",
		})
			.then(function (response) {
				return response.json().then(function (payload) {
					return { ok: response.ok, payload: payload };
				});
			})
			.then(function (result) {
				const payload = result.payload || {};
				if (!result.ok || !payload.ok) {
					setStatus(payload.error || "Couldn't reach Lapse right now.", "bad");
					return;
				}
				if (!payload.connected) {
					ui.list.replaceChildren();
					showAccount(null);
					chosen.clear();
					notify();
					if (payload.expired) {
						setConnectPrompt(
							"Your Lapse connection has expired.",
							"Reconnect Lapse"
						);
					} else {
						setConnectPrompt(
							"Connect your Lapse account to tape in the timelapses you've recorded.",
							"Connect Lapse"
						);
					}
					return;
				}

				loaded = true;
				showAccount(payload.account);
				const items = payload.timelapses || [];
				render(items);
				if (!items.length) {
					setStatus(
						"Nothing published yet — record one on lapse.hackclub.com, then hit refresh.",
						"quiet"
					);
				} else {
					setStatus("");
				}
			})
			.catch(function (e) {
				setStatus("Couldn't reach the server: " + e.message, "bad");
			})
			.finally(function () {
				loading = false;
				if (ui.refresh) ui.refresh.disabled = false;
			});
	}

	function boot(config) {
		wire();
		if (!ui.root) return;
		URL_ = config.url;
		CONNECT_URL = config.connectUrl;
		CSRF = config.csrfToken;
		NEXT_URL = config.nextUrl || "";

		if (ui.refresh) ui.refresh.addEventListener("click", load);

		// Fetched when the popup is first opened rather than on page load: the
		// book is read far more often than a lapse is written, and every open
		// is a call on somebody else's API.
		document.addEventListener("click", function (e) {
			const trigger = e.target.closest('[data-slip="slip-lapses"]');
			if (trigger && !loaded) load();
		});
	}

	window.AtlantisLapsePicker = {
		boot: boot,
		refresh: load,
		onChange: function (fn) {
			listeners.push(fn);
		},
		// [{id, seconds}] for everything currently ticked.
		selected: function () {
			return Array.from(chosen).map(function (id) {
				const item = byId.get(id);
				return { id: id, seconds: (item && item.trackedSeconds) || 0 };
			});
		},
		// True only once the list has actually been read and had nothing
		// pickable in it. That is what tells "you haven't recorded one yet"
		// apart from "pick one" — an unopened picker knows neither.
		nothingToPick: function () {
			if (!loaded) return false;
			let any = false;
			byId.forEach(function (item) {
				if (!BLOCKED[item.state]) any = true;
			});
			return !any;
		},
	};
})();
