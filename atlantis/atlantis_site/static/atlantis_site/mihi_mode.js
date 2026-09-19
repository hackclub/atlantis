/*
 * Mihi mode: the joke button that turns every word on the page into "mihi" and
 * every picture into a pearl.
 *
 * Ported from stardance, where it is a Stimulus controller. Two things are
 * worth knowing about it. It is presentation only — markup, link destinations
 * and form values are all left exactly as they were, so a page in mihi mode
 * still submits what it would have submitted. And every write is remembered
 * before it is made, so the mode can be taken back off in place.
 *
 * It boots itself off #mihi-toggle, which carries its own config as data
 * attributes, so a page turns it on by including the button and this file.
 */
(function () {
	"use strict";

	// Text inside these is never replaced: it is not prose a reader reads, and
	// rewriting it would break the page rather than decorate it. The toggle
	// ignores itself so the label it swaps between survives.
	const SKIP_TEXT =
		"script, style, noscript, textarea, [contenteditable], [data-mihi-mode-ignore]";
	const LABEL_ATTRIBUTES = ["placeholder", "title", "alt", "aria-label"];

	// A word is a run of letters, marks and digits, plus any apostrophes inside
	// it — so "don't" is one word and becomes one "mihi" rather than two.
	const mihify = (text) =>
		text.replace(/[\p{L}\p{M}\p{N}]+(?:['’][\p{L}\p{M}]+)*/gu, "mihi");

	function boot() {
		const button = document.getElementById("mihi-toggle");
		if (!button) return;

		const root = document.body;
		const image = `url(${JSON.stringify(button.dataset.mihiImage)})`;

		// What each node said before, and what each attribute held, so restore()
		// can put all of it back. Keyed by node, so a node the page drops takes
		// its entry with it.
		const originals = new Map();
		const attributes = new Map();
		const images = new Set();
		let enabled = false;

		// Our own writes come back as mutations, so the observer is disconnected
		// for the duration of a pass and reconnected at the end of it.
		const observer = new MutationObserver(() => refresh());

		function remember(element, name) {
			if (!attributes.has(element)) attributes.set(element, new Map());
			attributes.get(element).set(name, element.getAttribute(name));
		}

		function updateButton() {
			button.disabled = enabled;
			button.setAttribute("aria-pressed", String(enabled));
			button.textContent = enabled
				? "u stupid #getmihid"
				: "free 10,000 pearls hack *LEGIT* 2026 (WORKING)";
		}

		function replaceImages() {
			root.style.setProperty("--mihi-portrait", image);
			for (const element of images) {
				if (!root.contains(element)) images.delete(element);
			}

			root.querySelectorAll("*").forEach((element) => {
				if (element.closest("script, style, [data-mihi-mode-ignore]")) return;
				if (images.has(element)) return;

				if (element.matches("img")) {
					const style = getComputedStyle(element);
					remember(element, "style");
					element.style.width = style.width;
					element.style.height = style.height;
					element.style.objectFit = "fill";
					// CSS replacement rather than swapping src: the real src, any
					// srcset and the <picture> sources are all left alone, so
					// nothing has to be put back if the page swaps them itself.
					element.style.content = image;
					images.add(element);
					return;
				}

				const style = getComputedStyle(element);
				if (
					element.matches("svg, [role='img']") ||
					/url\(/.test(style.backgroundImage) ||
					/url\(/.test(style.maskImage)
				) {
					remember(element, "data-mihi-image");
					element.setAttribute("data-mihi-image", "true");
					images.add(element);
				}
				for (const pseudo of ["before", "after"]) {
					const decoration = getComputedStyle(element, `::${pseudo}`);
					if (
						/url\(/.test(decoration.backgroundImage) ||
						/url\(/.test(decoration.maskImage)
					) {
						remember(element, `data-mihi-${pseudo}`);
						element.setAttribute(`data-mihi-${pseudo}`, "true");
						images.add(element);
					}
				}
			});
		}

		function refresh() {
			observer.disconnect();
			replaceImages();
			for (const map of [originals, attributes]) {
				for (const node of map.keys()) {
					if (!root.contains(node)) map.delete(node);
				}
			}

			const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
			let node;
			while ((node = walker.nextNode())) {
				if (node.parentElement.closest(SKIP_TEXT) || !node.textContent.trim())
					continue;

				// An <option> with no explicit value submits its text, so replacing
				// the text would change what the form sends. Pin the value down
				// before the text moves.
				const option = node.parentElement.closest("option");
				if (option && !option.hasAttribute("value")) {
					remember(option, "value");
					option.setAttribute("value", option.value);
				}

				const replacement = mihify(node.textContent);
				if (!originals.has(node) || node.textContent !== replacement) {
					originals.set(node, node.textContent);
				}
				if (node.textContent !== replacement) node.textContent = replacement;
			}

			root
				.querySelectorAll("[placeholder], [title], [alt], [aria-label]")
				.forEach((element) => {
					if (element.closest(SKIP_TEXT)) return;
					for (const name of LABEL_ATTRIBUTES) {
						const value = element.getAttribute(name);
						if (!value?.trim() || value === mihify(value)) continue;
						remember(element, name);
						element.setAttribute(name, mihify(value));
					}
				});

			observer.observe(root, {
				childList: true,
				subtree: true,
				characterData: true,
				attributes: true,
				attributeFilter: [...LABEL_ATTRIBUTES, "src", "srcset"],
			});
		}

		function restore() {
			observer.disconnect();
			for (const [node, text] of originals) node.textContent = text;
			for (const [element, held] of attributes) {
				for (const [name, value] of held) {
					if (value === null) element.removeAttribute(name);
					else element.setAttribute(name, value);
				}
			}
			originals.clear();
			attributes.clear();
			images.clear();
			root.style.removeProperty("--mihi-portrait");
			enabled = false;
			updateButton();
		}

		button.addEventListener("click", () => {
			if (enabled) return;
			enabled = true;
			refresh();
			updateButton();
			// Fire and forget: the tally is for the dashboard, and a click that
			// fails to be counted is still a click the visitor has already seen.
			fetch(button.dataset.mihiUrl, {
				method: "POST",
				credentials: "same-origin",
				keepalive: true,
				headers: {
					"X-CSRFToken": button.dataset.mihiCsrf,
					Accept: "application/json",
				},
			}).catch(() => {});
		});

		// Exposed so a page can take itself back out of the mode — the reviewer
		// desks, or anything else that swaps the page's content underneath us.
		window.AtlantisMihiMode = { restore: restore };
		updateButton();
	}

	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", boot);
	} else {
		boot();
	}
})();
