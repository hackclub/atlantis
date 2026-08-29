/*
 * Reviewer-desk behaviour, shared by the four review queues and the four
 * review pages.
 *
 * The whole point of this file is keystrokes. A reviewer works the same three
 * or four motions a few hundred times a week — read, decide, next — and every
 * one of them that costs a mouse trip costs the whole queue. So:
 *
 *   - anything with `data-key="x"` is clickable by pressing x,
 *   - anything with `data-focus="x"` is focusable by pressing x,
 *   - anything with `data-mod-key="x"` is clickable by pressing Cmd/Ctrl+x,
 *   - anything with `data-mod-focus="x"` is focusable by pressing Cmd/Ctrl+x,
 *   - the primary decision is one Cmd/Ctrl+Enter away from inside any text box,
 *   - and `?` shows the reviewer what all of that is.
 *
 * The split is deliberate. Bare letters *open* things — the owner's profile,
 * the Printables listing, the next item — and are safe to press by accident.
 * Anything that writes a decision wants Cmd/Ctrl held down with it, so a
 * verdict is never one stray keystroke away.
 *
 * Single-letter keys never fire while the reviewer is typing (Escape gets them
 * back); modified ones fire anywhere, because they don't collide with prose.
 *
 * Everything degrades: with JavaScript off, every shortcut here has a real
 * link or button behind it, the panels are <details> that still open, and the
 * forms still post.
 */
(function () {
    'use strict';

    var STORAGE_PREFIX = 'rv-card:';

    function isTypingContext(el) {
        if (!el || !el.tagName) return false;
        var tag = el.tagName;
        return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
    }

    function csrfToken() {
        var field = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return field ? field.value : '';
    }

    /* ---------------------------------------------------------------- toast */

    function toast(text, tone) {
        var host = document.querySelector('.rv-toasts');
        if (!host) {
            host = document.createElement('div');
            host.className = 'rv-toasts';
            document.body.appendChild(host);
        }
        var node = document.createElement('p');
        node.className = 'rv-toast' + (tone ? ' rv-toast-' + tone : '');
        node.textContent = text;
        host.appendChild(node);
        setTimeout(function () { node.classList.add('rv-toast-out'); }, 2600);
        setTimeout(function () { node.remove(); }, 3200);
    }

    /* ------------------------------------------------- collapsible panels */

    /*
     * Panels remember whether they were open, keyed on what they are rather
     * than which review they were on: a reviewer who keeps the journal open
     * wants it open on the next ship too, not just this one.
     */
    function setupCards() {
        var cards = document.querySelectorAll('details[data-card]');
        var number = 0;
        cards.forEach(function (card) {
            var key = card.dataset.card;
            try {
                var saved = localStorage.getItem(STORAGE_PREFIX + key);
                if (saved !== null) card.open = saved === '1';
            } catch (e) { /* private mode; the markup's default stands */ }

            card.addEventListener('toggle', function () {
                try {
                    localStorage.setItem(STORAGE_PREFIX + key, card.open ? '1' : '0');
                } catch (e) { /* nothing to do; the panel still toggled */ }
            });

            var slot = card.querySelector('.rv-card-key');
            if (slot && number < 9) {
                number += 1;
                card.dataset.cardNumber = String(number);
                var kbd = document.createElement('kbd');
                kbd.textContent = String(number);
                slot.appendChild(kbd);
            }
        });
    }

    function toggleCardByNumber(n) {
        var card = document.querySelector('details[data-card-number="' + n + '"]');
        if (!card) return false;
        card.open = !card.open;
        if (card.open) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        return true;
    }

    /* ------------------------------------------------------------ shortcuts */

    function shortcutTarget(key) {
        return document.querySelector('[data-key="' + key.replace(/"/g, '') + '"]:not([disabled])');
    }

    function focusTarget(key) {
        return document.querySelector('[data-focus="' + key.replace(/"/g, '') + '"]');
    }

    function modTarget(key) {
        return document.querySelector('[data-mod-key="' + key.replace(/"/g, '') + '"]:not([disabled])');
    }

    function modFocusTarget(key) {
        return document.querySelector('[data-mod-focus="' + key.replace(/"/g, '') + '"]');
    }

    /*
     * A decision button may name a field it can't be pressed without —
     * "Reject" without feedback is not a decision, it's a shrug. When that
     * field is empty the shortcut puts the cursor in it instead of firing,
     * which is the thing the reviewer was going to have to do anyway.
     */
    function firePrerequisite(button) {
        var selector = button.dataset.requires;
        if (!selector) return false;
        var field = document.querySelector(selector);
        if (!field || field.value.trim()) return false;
        field.focus();
        toast('Write the feedback first — it goes to the shipper.', 'bad');
        return true;
    }

    function primaryButton() {
        // Anywhere on the page: the reviewer shell keeps its decision in an
        // .rv-form, the Lookout page keeps its submit in the top bar, and both
        // mean the same thing — the one action Cmd/Ctrl+Enter performs.
        return document.querySelector('[data-primary]:not([disabled])');
    }

    function submitPrimary() {
        var button = primaryButton();
        if (!button || !button.form) return false;
        if (firePrerequisite(button)) return true;
        // requestSubmit, not submit(): it runs the form's own validation and
        // carries the button's name/value, which is what names the decision.
        button.form.requestSubmit(button);
        return true;
    }

    function shortcutsDialog() {
        return document.getElementById('rv-shortcuts');
    }

    function onKeyDown(event) {
        if (event.defaultPrevented) return;
        var dialog = shortcutsDialog();

        if (event.key === 'Escape') {
            if (dialog && dialog.open) { dialog.close(); return; }
            if (isTypingContext(event.target)) { event.target.blur(); return; }
            return;
        }

        // Cmd/Ctrl+Enter submits the primary decision — the one shortcut worth
        // taking from the browser, and the only one that works mid-sentence.
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
            if (submitPrimary()) event.preventDefault();
            return;
        }

        // The modified family: decisions and the fields they're written in.
        // These fire from inside a textarea, which is where a reviewer is
        // sitting when they finish writing and want to submit.
        if ((event.metaKey || event.ctrlKey) && !event.altKey) {
            var modKey = event.key.toLowerCase();
            var focusable = modFocusTarget(modKey);
            if (focusable) {
                event.preventDefault();
                focusable.focus();
                if (focusable.select) focusable.select();
                return;
            }
            var decision = modTarget(modKey);
            if (decision) {
                event.preventDefault();
                if (!firePrerequisite(decision)) decision.click();
            }
            return;
        }

        if (event.metaKey || event.ctrlKey || event.altKey) return;
        if (dialog && dialog.open) return;

        // Enter in a single-line field would fire the form's first submit
        // button — which on these forms is a decision. Never that by accident.
        if (event.key === 'Enter' && event.target && event.target.tagName === 'INPUT') {
            var owner = event.target.form;
            if (owner && owner.classList.contains('rv-form')) {
                event.preventDefault();
                return;
            }
        }

        if (isTypingContext(event.target)) return;

        var key = event.key.toLowerCase();

        if (key === '?') {
            event.preventDefault();
            if (dialog) dialog.showModal();
            return;
        }

        if (key >= '1' && key <= '9') {
            if (toggleCardByNumber(key)) event.preventDefault();
            return;
        }

        var plainFocusable = focusTarget(key);
        if (plainFocusable) {
            event.preventDefault();
            plainFocusable.focus();
            if (plainFocusable.select) plainFocusable.select();
            return;
        }

        var target = shortcutTarget(event.key === 'Enter' ? 'enter' : key);
        if (target) {
            event.preventDefault();
            target.click();
        }
    }

    /* -------------------------------------------------- forms and decisions */

    function setupForms() {
        document.querySelectorAll('.rv-form').forEach(function (form) {
            form.addEventListener('submit', function (event) {
                var submitter = event.submitter;
                if (submitter && submitter.dataset.confirm && !window.confirm(submitter.dataset.confirm)) {
                    event.preventDefault();
                    return;
                }
                if (form.dataset.submitted === '1') {
                    event.preventDefault();
                    return;
                }
                form.dataset.submitted = '1';
                form.classList.add('rv-form-busy');
                // Deferred: disabling a submit button before the browser has
                // serialised the form drops its name and value, and that value
                // *is* the decision.
                setTimeout(function () {
                    form.querySelectorAll('button[type="submit"]').forEach(function (button) {
                        button.disabled = true;
                    });
                }, 0);
            });
        });
    }

    function setupCounters() {
        document.querySelectorAll('[data-counter-for]').forEach(function (counter) {
            var field = document.getElementById(counter.dataset.counterFor);
            if (!field) return;
            var max = parseInt(counter.dataset.max, 10) || 0;
            var render = function () {
                var used = field.value.length;
                // Silent until it starts to matter — a counter on an empty box
                // is noise, a counter at 90% is a warning.
                if (!max || used < max * 0.8) {
                    counter.textContent = '';
                    counter.classList.remove('rv-counter-near');
                    return;
                }
                counter.textContent = used + ' / ' + max;
                counter.classList.toggle('rv-counter-near', used >= max * 0.95);
            };
            field.addEventListener('input', render);
            render();
        });
    }

    /*
     * "Notes" is a button in the top bar and a card in the column, because the
     * reviewer notes and the previous decisions are the same conversation and
     * splitting them into a panel and a history would mean reading both.
     */
    function setupNotes() {
        var button = document.querySelector('[data-toggle-notes]');
        var card = document.querySelector('[data-notes]');
        if (!button || !card) return;
        button.addEventListener('click', function () {
            card.open = !card.open;
            if (!card.open) return;
            card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            var box = card.querySelector('textarea');
            if (box) box.focus();
        });
    }

    function setupCopy() {
        document.addEventListener('click', function (event) {
            var button = event.target.closest('[data-copy]');
            if (!button) return;
            var value = button.dataset.copy;
            if (!value || !navigator.clipboard) return;
            navigator.clipboard.writeText(value).then(function () {
                toast('Copied ' + value);
            }, function () {
                toast('Could not copy that', 'bad');
            });
        });
    }

    /* ----------------------------------------------------- payout previews */

    /*
     * Mirrors layers_for_minutes() in views/helpers.py, banker's rounding
     * included, so what the reviewer is shown is what the reviewer awards.
     */
    function roundHalfEven(value) {
        var nearest = Math.round(value);
        var isTie = Math.abs(value % 1) === 0.5;
        return isTie && nearest % 2 !== 0 ? nearest - 1 : nearest;
    }

    function layersFor(minutes, pearlsPerHour, multiplier) {
        return roundHalfEven(Math.floor(minutes / 6) * (pearlsPerHour / 10) * multiplier);
    }

    function setupPayout() {
        document.querySelectorAll('[data-payout]').forEach(function (panel) {
            var mode = panel.dataset.payout;
            var logged = parseInt(panel.dataset.logged, 10) || 0;
            var perHour = parseFloat(panel.dataset.pearlsPerHour) || 0;
            var out = panel.querySelector('[data-payout-out]');
            var inLabel = panel.querySelector('[data-payout-in]');
            var input = document.querySelector('[data-payout-input]');
            var slider = panel.dataset.multiplier ? document.getElementById(panel.dataset.multiplier) : null;
            var readout = document.getElementById('multiplier_value');
            if (!out) return;

            var render = function () {
                var multiplier = slider ? parseFloat(slider.value) : 1;
                var minutes;
                if (mode === 'deduct') {
                    var deducted = Math.min(Math.max(parseInt(input && input.value, 10) || 0, 0), logged);
                    minutes = logged - deducted;
                } else {
                    minutes = Math.max(parseInt(input && input.value, 10) || 0, 0);
                }
                var base = layersFor(minutes, perHour, 1);
                var paid = layersFor(minutes, perHour, multiplier);
                if (inLabel) inLabel.textContent = minutes + ' min';
                if (readout) readout.textContent = multiplier.toFixed(1) + '×';
                out.textContent = base === paid
                    ? paid + ' pearls'
                    : base + ' → ' + paid + ' pearls';
                panel.classList.toggle('rv-payout-scaled', base !== paid);
            };

            if (input) input.addEventListener('input', render);
            if (slider) slider.addEventListener('input', render);
            render();
        });
    }

    /* ----------------------------------------------------------- heartbeat */

    /*
     * Renews the lease on the open review. The interesting answer is 409:
     * somebody else has it now, and the reviewer needs to know that before
     * they finish writing feedback that is about to be refused.
     */
    function setupHeartbeat() {
        var script = document.querySelector('script[data-heartbeat-url]');
        if (!script) return;
        var url = script.dataset.heartbeatUrl;
        if (!url) return;
        var seconds = parseInt(script.dataset.heartbeatSeconds, 10) || 90;
        var failures = 0;
        var timer = setInterval(function () {
            fetch(url, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken(), 'Accept': 'application/json' },
                credentials: 'same-origin',
            }).then(function (response) {
                if (response.ok) {
                    failures = 0;
                    return;
                }
                if (response.status === 409) {
                    clearInterval(timer);
                    response.json().then(function (body) {
                        toast((body.holder || 'Another reviewer') + ' has taken over this review.', 'bad');
                    }, function () {
                        toast('Another reviewer has taken over this review.', 'bad');
                    });
                    return;
                }
                failures += 1;
            }, function () {
                failures += 1;
            }).then(function () {
                if (failures >= 3) {
                    clearInterval(timer);
                    toast('Lost contact with the server — your claim on this review may have lapsed.', 'bad');
                }
            });
        }, seconds * 1000);
    }

    /* --------------------------------------------------------- queue tables */

    /*
     * Whole-row links plus j/k/Enter. A desk is a list of things to open one
     * after another, and it should be walkable like one.
     */
    function setupQueueTable() {
        var rows = Array.prototype.slice.call(document.querySelectorAll('.rq-row[data-href]'));
        if (!rows.length) return;

        rows.forEach(function (row) {
            row.addEventListener('click', function (event) {
                if (event.target.closest('a, button, input, label')) return;
                window.location.href = row.dataset.href;
            });
            row.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    window.location.href = row.dataset.href;
                }
            });
        });

        document.addEventListener('keydown', function (event) {
            if (event.metaKey || event.ctrlKey || event.altKey) return;
            if (isTypingContext(event.target)) return;
            var key = event.key.toLowerCase();
            if (key !== 'j' && key !== 'k') return;
            event.preventDefault();
            var current = rows.indexOf(document.activeElement);
            var next = key === 'j'
                ? Math.min(current + 1, rows.length - 1)
                : Math.max(current - 1, 0);
            if (current === -1) next = 0;
            rows[next].focus();
            rows[next].scrollIntoView({ block: 'nearest' });
        });
    }

    /* -------------------------------------------------------------- charts */

    function setupBars() {
        document.querySelectorAll('[data-bar]').forEach(function (el) {
            el.style.width = el.dataset.bar + '%';
        });
        document.querySelectorAll('[data-progress]').forEach(function (el) {
            var of = parseFloat(el.dataset.progressOf) || 1;
            el.style.width = Math.min(100, (parseFloat(el.dataset.progress) / of) * 100) + '%';
        });
    }

    function init() {
        setupCards();
        setupForms();
        setupCounters();
        setupNotes();
        setupCopy();
        setupPayout();
        setupHeartbeat();
        setupQueueTable();
        setupBars();
        document.addEventListener('keydown', onKeyDown);
        var help = document.querySelector('[data-shortcut-help]');
        var dialog = shortcutsDialog();
        if (help && dialog) {
            help.addEventListener('click', function () { dialog.showModal(); });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.rvToast = toast;
})();
