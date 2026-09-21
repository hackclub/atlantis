(function () {
	"use strict";

	const KEY = "mihi-mode";
	const SKIP_TEXT =
		"script, style, noscript, textarea, [contenteditable], [data-mihi-mode-ignore]";
	const LABEL_ATTRIBUTES = ["placeholder", "title", "alt", "aria-label"];
	const mihify = (text) =>
		text.replace(/[\p{L}\p{M}\p{N}]+(?:['’][\p{L}\p{M}]+)*/gu, "mihi");

	const store = {
		get() {
			try {
				return sessionStorage.getItem(KEY) === "1";
			} catch {
				return false;
			}
		},
		set(on) {
			try {
				if (on) sessionStorage.setItem(KEY, "1");
				else sessionStorage.removeItem(KEY);
			} catch {
				return;
			}
		},
	};

	function boot() {
		const tag = document.querySelector("script[data-mihi-portrait]");
		if (!tag) return;

		const nav = performance.getEntriesByType("navigation")[0];
		if (nav && nav.type === "reload") store.set(false);

		const button = document.getElementById("mihi-toggle");
		const portrait = tag.dataset.mihiPortrait || "";
		const image = portrait ? `url(${JSON.stringify(portrait)})` : "";
		const root = document.body;

		const originals = new Map();
		const attributes = new Map();
		const images = new Set();
		const observer = new MutationObserver(() => refresh());
		let enabled = false;

		function remember(element, name) {
			if (!attributes.has(element)) attributes.set(element, new Map());
			attributes.get(element).set(name, element.getAttribute(name));
		}

		function updateButton() {
			if (!button) return;
			button.disabled = enabled;
			button.setAttribute("aria-pressed", String(enabled));
			button.textContent = enabled
				? "u stupid #getmihid"
				: "free 10,000 pearls hack *LEGIT* 2026 (WORKING)";
		}

		function replaceImages() {
			if (!image) return;
			for (const element of images) {
				if (!element.isConnected) images.delete(element);
			}

			root.querySelectorAll("img").forEach((element) => {
				if (element.closest("script, style, [data-mihi-mode-ignore]")) return;
				if (images.has(element)) return;

				const src = element.currentSrc || element.getAttribute("src") || "";
				if (!src) return;
				const mask = `url(${JSON.stringify(src)})`;

				const style = getComputedStyle(element);
				remember(element, "style");
				element.style.width = style.width;
				element.style.height = style.height;
				element.style.objectFit = "cover";
				element.style.content = image;
				element.style.maskImage = mask;
				element.style.webkitMaskImage = mask;
				element.style.maskSize = "100% 100%";
				element.style.webkitMaskSize = "100% 100%";
				element.style.maskRepeat = "no-repeat";
				images.add(element);
			});
		}

		function refresh() {
			observer.disconnect();
			replaceImages();
			for (const map of [originals, attributes]) {
				for (const node of map.keys()) {
					if (!node.isConnected) map.delete(node);
				}
			}

			const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
			let node;
			while ((node = walker.nextNode())) {
				if (node.parentElement.closest(SKIP_TEXT) || !node.textContent.trim())
					continue;

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

		if (button) {
			button.addEventListener("click", () => {
				if (enabled) return;
				enabled = true;
				store.set(true);
				refresh();
				updateButton();
			});
		}

		if (store.get()) {
			enabled = true;
			refresh();
		}
		updateButton();
	}

	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", boot);
	} else {
		boot();
	}
})();
