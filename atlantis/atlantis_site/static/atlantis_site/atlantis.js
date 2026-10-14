/*
 * The handful of things every page on the site does the same way, so no page
 * has to invent its own: the note that says what just happened, the question
 * asked before something can't be taken back, and a form that knows it has
 * already been sent.
 *
 * Loaded once per page by _banner.html, which every page includes. It is a
 * plain script rather than a deferred one so a page script further down can
 * lean on window.Atlantis while it runs; everything that touches the document
 * hangs off listeners on `document`, so nothing here waits for it to load.
 *
 * Everything degrades. With JavaScript off the forms post as they always did,
 * a `data-confirm` simply isn't asked, and the server's messages still show,
 * they just don't leave on their own.
 */
(function () {
    'use strict';

    if (window.Atlantis) return;

    /* ---------------------------------------------------------------- notes */

    /*
     * One note for the whole site, whoever raised it: the server's messages
     * arrive already in the banner, and a script's toast() goes into the same
     * place in the same hand. Something that went wrong is tinted and stays
     * long enough to be read; something that went right says so and gets out
     * of the way. Hovering holds a note, a click takes it away.
     */
    var LINGER = { bad: 7000, ok: 2800, plain: 2800 };

    function noteHost() {
        var host = document.getElementById('message-banner');
        if (host) return host;
        host = document.createElement('div');
        host.className = 'messages';
        host.id = 'message-banner';
        host.setAttribute('role', 'status');
        host.setAttribute('aria-live', 'polite');
        document.body.appendChild(host);
        return host;
    }

    function toneOf(note) {
        if (note.classList.contains('is-bad')) return 'bad';
        if (note.classList.contains('is-ok')) return 'ok';
        return 'plain';
    }

    function dismiss(note) {
        if (note.dataset.leaving) return;
        note.dataset.leaving = '1';
        clearTimeout(note._atlTimer);
        note.addEventListener('transitionend', function () { note.remove(); }, { once: true });
        note.classList.add('leaving');
        // Belt and braces for the case where no transition runs at all.
        setTimeout(function () { note.remove(); }, 700);
    }

    function arm(note) {
        if (note._atlArmed) return;
        note._atlArmed = true;
        var wait = LINGER[toneOf(note)];
        var start = function () {
            clearTimeout(note._atlTimer);
            note._atlTimer = setTimeout(function () { dismiss(note); }, wait);
        };
        note.addEventListener('click', function () { dismiss(note); });
        note.addEventListener('mouseenter', function () { clearTimeout(note._atlTimer); });
        note.addEventListener('mouseleave', start);
        start();
    }

    function toast(text, tone) {
        var note = document.createElement('p');
        note.className = 'message' + (tone === 'bad' ? ' is-bad' : tone === 'ok' ? ' is-ok' : '');
        if (tone === 'bad') note.setAttribute('role', 'alert');
        note.textContent = text;
        noteHost().appendChild(note);
        arm(note);
        return note;
    }

    function armServerNotes() {
        document.querySelectorAll('#message-banner .message').forEach(arm);
    }

    /* -------------------------------------------------------------- asking */

    /*
     * The browser's own confirm() is a grey box from another program, with
     * "OK" on a question that might be "delete this forever". This one is in
     * the site's hand, says what the button does, and answers with a promise.
     * Enter agrees, Escape and the backdrop don't.
     */
    var asking = null;

    function confirmDialog() {
        var dialog = document.getElementById('atl-confirm');
        if (dialog) return dialog;
        dialog = document.createElement('dialog');
        dialog.id = 'atl-confirm';
        dialog.className = 'atl-confirm';
        dialog.innerHTML =
            '<form method="dialog">' +
                '<p class="atl-confirm-text"></p>' +
                '<div class="atl-confirm-actions">' +
                    '<button type="submit" value="no" class="atl-confirm-no">Cancel</button>' +
                    '<button type="submit" value="yes" class="atl-confirm-yes">OK</button>' +
                '</div>' +
            '</form>';
        // A click on the backdrop lands on the dialog itself, never on the
        // form inside it.
        dialog.addEventListener('click', function (event) {
            if (event.target === dialog) dialog.close('no');
        });
        // Cmd/Ctrl+Enter is how a reviewer sends a decision; pressed again at
        // the question it means yes, rather than reaching the page behind and
        // asking all over.
        dialog.addEventListener('keydown', function (event) {
            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                event.preventDefault();
                event.stopPropagation();
                dialog.close('yes');
            }
        });
        document.body.appendChild(dialog);
        return dialog;
    }

    function confirm(message, options) {
        options = options || {};
        var dialog = confirmDialog();
        if (typeof dialog.showModal !== 'function') {
            return Promise.resolve(window.confirm(message));
        }
        // A second question while one is open answers the first with no.
        if (asking) asking('no');

        dialog.querySelector('.atl-confirm-text').textContent = message;
        var yes = dialog.querySelector('.atl-confirm-yes');
        yes.textContent = options.ok || 'OK';
        yes.classList.toggle('is-danger', Boolean(options.danger));
        dialog.querySelector('.atl-confirm-no').textContent = options.cancel || 'Cancel';
        dialog.returnValue = '';

        return new Promise(function (resolve) {
            var settle = function (value) {
                asking = null;
                dialog.removeEventListener('close', onClose);
                if (dialog.open) dialog.close(value);
                resolve(value === 'yes');
            };
            // `close` is queued, so the one from a question this replaced can
            // arrive after this one has opened; only a closed dialog answers.
            var onClose = function () {
                if (!dialog.open) settle(dialog.returnValue);
            };
            asking = settle;
            dialog.addEventListener('close', onClose);
            dialog.showModal();
            yes.focus();
        });
    }

    /*
     * `data-confirm` on a submit button (or on the form, for every button in
     * it) asks first. The submit is held, the question asked, and on a yes
     * sent again with the same button, so its name and value — which is
     * often the whole decision — go with it. Listening in the capture phase
     * means the form's own submit handlers only ever see a confirmed submit.
     *
     * `data-confirm-ok` names the yes button; `data-confirm-danger` paints it.
     */
    function confirmSource(form, submitter) {
        if (submitter && submitter.dataset.confirm) return submitter;
        if (form.dataset.confirm) return form;
        return null;
    }

    // Unless told otherwise, the yes button says what the pressed button
    // said ("Approve", "Lock project"), minus any shortcut hint beside it.
    function confirmLabel(source, submitter) {
        if (source.dataset.confirmOk) return source.dataset.confirmOk;
        if (!submitter || submitter.tagName !== 'BUTTON') return undefined;
        var copy = submitter.cloneNode(true);
        copy.querySelectorAll('kbd').forEach(function (kbd) { kbd.remove(); });
        var text = copy.textContent.replace(/\s+/g, ' ').trim();
        return text && text.length <= 24 ? text : undefined;
    }

    document.addEventListener('submit', function (event) {
        var form = event.target;
        var source = confirmSource(form, event.submitter);
        if (!source) return;
        if (form.dataset.atlConfirmed === '1') {
            delete form.dataset.atlConfirmed;
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        var submitter = event.submitter;
        confirm(source.dataset.confirm, {
            ok: confirmLabel(source, submitter),
            danger: source.hasAttribute('data-confirm-danger')
                || Boolean(submitter && /danger/.test(submitter.className)),
        }).then(function (ok) {
            if (!ok) return;
            form.dataset.atlConfirmed = '1';
            if (submitter && submitter.form === form) form.requestSubmit(submitter);
            else form.requestSubmit();
        });
    }, true);

    /* ---------------------------------------------------------- busy forms */

    /*
     * A form that has been sent says so and won't be sent twice. A second
     * click used to abort the first request and come back as "you're doing
     * that too fast", which is a strange thing to be told for waiting.
     *
     * Every POST form gets this unless it carries `data-no-busy`. Anything
     * another handler has already stopped (a missing field, a "cancel") is
     * left alone, since nothing was sent. The buttons are disabled a beat
     * late: disabling one before the browser has read the form drops its
     * name and value, and on a decision form that value *is* the decision.
     *
     * A page that comes back from the back/forward cache, or a post that
     * never navigates (a download), would otherwise leave the form stuck, so
     * both let go.
     */
    var BUSY_GIVE_UP_MS = 20000;

    function setBusy(form, busy) {
        if (busy) {
            form.dataset.atlBusy = '1';
            form.classList.add('is-busy');
            form.setAttribute('aria-busy', 'true');
        } else {
            delete form.dataset.atlBusy;
            form.classList.remove('is-busy');
            form.removeAttribute('aria-busy');
        }
        // form.elements, not a query inside the form: "tape it in" is a
        // button elsewhere on the page that names its form with form="…".
        var buttons = Array.prototype.filter.call(form.elements, function (el) {
            return el.type === 'submit';
        });
        if (busy) {
            setTimeout(function () {
                if (form.dataset.atlBusy !== '1') return;
                buttons.forEach(function (button) {
                    if (button.disabled) return;
                    button.disabled = true;
                    button.dataset.atlBusyDisabled = '1';
                });
            }, 0);
        } else {
            buttons.forEach(function (button) {
                if (button.dataset.atlBusyDisabled !== '1') return;
                button.disabled = false;
                delete button.dataset.atlBusyDisabled;
            });
        }
    }

    function isBusyCandidate(form) {
        if (!form || form.tagName !== 'FORM') return false;
        if ((form.getAttribute('method') || '').toLowerCase() !== 'post') return false;
        if (form.hasAttribute('data-no-busy')) return false;
        if ((form.getAttribute('target') || '_self') !== '_self') return false;
        return true;
    }

    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!isBusyCandidate(form)) return;
        if (form.dataset.atlBusy === '1') {
            event.preventDefault();
            return;
        }
        // Decided once the event has finished: a page script listening after
        // this one may still stop the submit, and then nothing went anywhere.
        setTimeout(function () {
            if (event.defaultPrevented) return;
            setBusy(form, true);
            clearTimeout(form._atlBusyTimer);
            form._atlBusyTimer = setTimeout(function () { setBusy(form, false); }, BUSY_GIVE_UP_MS);
        }, 0);
    });

    window.addEventListener('pageshow', function (event) {
        if (!event.persisted) return;
        document.querySelectorAll('form[data-atl-busy]').forEach(function (form) {
            setBusy(form, false);
        });
    });

    /* ------------------------------------------------- forms sent by script */

    /*
     * Two kinds of form are sent from here instead of by the browser, both
     * to views wrapped in fetch_aware (views/helpers.py), which answer a
     * script in JSON and a browser exactly as before:
     *
     *   data-upload  files go up with a progress bar, and a file that's
     *                over its input's `data-max-mb` is turned away before a
     *                byte is sent, instead of after the whole thing.
     *   data-ajax    a small change that shouldn't reload the page. With
     *                `data-ajax-swap="#id"` the element is replaced by the
     *                `html` the view returns; without it the page follows
     *                the redirect like any form would.
     *
     * Either way, when the server says no, the page stays exactly as it was
     * — the title typed, the photo picked, the 200 MB STL chosen — and says
     * why, instead of reloading into an empty form.
     *
     * `data-progress="#id"` says where the bar goes when the form itself
     * isn't somewhere you'd see it.
     */
    var uploading = 0;

    function oversizeFile(form) {
        var fields = Array.prototype.filter.call(form.elements, function (el) {
            return el.type === 'file' && el.dataset.maxMb && el.files;
        });
        for (var i = 0; i < fields.length; i++) {
            var limit = parseFloat(fields[i].dataset.maxMb) * 1024 * 1024;
            for (var j = 0; j < fields[i].files.length; j++) {
                if (fields[i].files[j].size > limit) {
                    return { field: fields[i], file: fields[i].files[j] };
                }
            }
        }
        return null;
    }

    function formBytes(form) {
        var total = 0;
        Array.prototype.forEach.call(form.elements, function (el) {
            if (el.type !== 'file' || !el.files) return;
            Array.prototype.forEach.call(el.files, function (file) { total += file.size; });
        });
        return total;
    }

    function formData(form, submitter) {
        try {
            return new FormData(form, submitter || undefined);
        } catch (e) {
            // Browsers before FormData(form, submitter) learned it: add the
            // button's name and value by hand, which is the same thing.
            var data = new FormData(form);
            if (submitter && submitter.name) data.append(submitter.name, submitter.value);
            return data;
        }
    }

    function progressBar(form, submitter) {
        var bar = document.createElement('span');
        bar.className = 'atl-progress';
        bar.setAttribute('role', 'progressbar');
        bar.setAttribute('aria-valuemin', '0');
        bar.setAttribute('aria-valuemax', '100');
        bar.innerHTML = '<span class="atl-progress-track"><span class="atl-progress-fill"></span></span>'
            + '<span class="atl-progress-label"></span>';
        // Over the row of buttons, full width, rather than squeezed in
        // among them.
        var where = form.dataset.progress && document.querySelector(form.dataset.progress);
        var row = submitter && form.contains(submitter) && submitter.parentNode !== form
            ? submitter.parentNode : null;
        if (where) where.appendChild(bar);
        else if (row) row.parentNode.insertBefore(bar, row);
        else form.appendChild(bar);

        var fill = bar.querySelector('.atl-progress-fill');
        var label = bar.querySelector('.atl-progress-label');
        return {
            set: function (fraction) {
                var percent = Math.round(fraction * 100);
                fill.style.width = percent + '%';
                bar.setAttribute('aria-valuenow', String(percent));
                label.textContent = 'uploading… ' + percent + '%';
            },
            processing: function () {
                bar.classList.add('is-processing');
                bar.removeAttribute('aria-valuenow');
                label.textContent = 'saving…';
            },
            remove: function () { bar.remove(); },
        };
    }

    function sayAll(list, fallback) {
        if (list && list.length) {
            list.forEach(function (message) {
                var bad = message.level === 'error' || message.level === 'warning';
                toast(message.text, bad ? 'bad' : message.level === 'success' ? 'ok' : undefined);
            });
        } else if (fallback) {
            toast(fallback, 'bad');
        }
    }

    function swapIn(selector, html) {
        var target = document.querySelector(selector);
        if (!target) return false;
        var holder = document.createElement('template');
        holder.innerHTML = html.trim();
        var replacement = holder.content.firstElementChild;
        if (!replacement) return false;
        target.replaceWith(replacement);
        return true;
    }

    function sendForm(form, submitter) {
        var big = oversizeFile(form);
        if (big) {
            toast('"' + big.file.name + '" is too big. The limit is '
                + big.field.dataset.maxMb + ' MB.', 'bad');
            big.field.focus();
            return;
        }

        var isUpload = form.hasAttribute('data-upload');
        var bytes = isUpload ? formBytes(form) : 0;
        var bar = bytes > 0 ? progressBar(form, submitter) : null;
        var data = formData(form, submitter);

        setBusy(form, true);
        if (bar) {
            uploading += 1;
            bar.set(0);
        }

        var xhr = new XMLHttpRequest();
        xhr.open('POST', form.action);
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        xhr.setRequestHeader('X-CSRFToken', csrf());
        xhr.setRequestHeader('Accept', 'application/json');
        if (bar) {
            xhr.upload.addEventListener('progress', function (event) {
                if (event.lengthComputable) bar.set(event.loaded / event.total);
            });
            // Every byte is up; the server is still sniffing and storing it.
            xhr.upload.addEventListener('load', function () { bar.processing(); });
        }

        var finish = function (keepBusy) {
            if (bar) {
                uploading -= 1;
                bar.remove();
            }
            if (!keepBusy) setBusy(form, false);
        };

        xhr.addEventListener('load', function () {
            var body = null;
            try { body = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }

            if (!body && xhr.status >= 200 && xhr.status < 300) {
                // A view that doesn't speak JSON did the thing and redirected,
                // and the request followed it. Go where the browser would have.
                finish(true);
                window.location.assign(xhr.responseURL || window.location.href);
                return;
            }
            if (!body) {
                finish(false);
                toast(xhr.status === 413
                    ? 'That upload is too big for the server.'
                    : 'Something went wrong (' + xhr.status + '). Nothing was changed; try again.', 'bad');
                return;
            }
            if (!body.ok) {
                finish(false);
                sayAll(body.messages, 'That didn’t go through. Try again.');
                return;
            }
            if (body.html && form.dataset.ajaxSwap && swapIn(form.dataset.ajaxSwap, body.html)) {
                finish(false);
                sayAll(body.messages);
                return;
            }
            if (body.redirect) {
                // Stays busy: the page is about to be replaced.
                finish(true);
                window.location.assign(body.redirect);
                return;
            }
            finish(false);
            sayAll(body.messages);
        });
        xhr.addEventListener('error', function () {
            finish(false);
            toast('Couldn’t reach Atlantis. Check your connection and try again.', 'bad');
        });
        xhr.send(data);
    }

    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!form || form.tagName !== 'FORM') return;
        if (!form.hasAttribute('data-upload') && !form.hasAttribute('data-ajax')) return;
        if (event.defaultPrevented || form.dataset.atlBusy === '1') return;
        event.preventDefault();
        sendForm(form, event.submitter);
    });

    // Leaving mid-upload throws the upload away; say so first.
    window.addEventListener('beforeunload', function (event) {
        if (uploading > 0) {
            event.preventDefault();
            event.returnValue = '';
        }
    });

    /* ------------------------------------------------------------- dialogs */

    /*
     * Pop-ups are <dialog class="modal">, opened with showModal(), so the
     * browser does what each page used to hand-roll and half-do: focus stays
     * inside, Escape closes, the page behind can't be clicked, and focus goes
     * back to whatever opened it. What's left is wiring:
     *
     *   data-open-dialog="#id"  on a button, opens that dialog
     *   data-close-dialog       on a button inside one, closes it
     *   a click on the backdrop closes it too
     *
     * Opening puts the cursor in the first field, which is the next thing
     * anyone does with a form in a pop-up.
     */
    function openDialog(dialog) {
        if (!dialog || dialog.open) return;
        dialog.showModal();
        var first = dialog.querySelector('input:not([type="hidden"]):not([disabled]), textarea, select');
        if (first) first.focus();
    }

    document.addEventListener('click', function (event) {
        var opener = event.target.closest('[data-open-dialog]');
        if (opener) {
            openDialog(document.querySelector(opener.dataset.openDialog));
            return;
        }
        var closer = event.target.closest('[data-close-dialog]');
        if (closer) {
            var owner = closer.closest('dialog');
            if (owner) owner.close();
            return;
        }
        // The dialog is only as big as its panel, so a click that lands on
        // the dialog itself landed on the backdrop.
        if (event.target.tagName === 'DIALOG' && event.target.classList.contains('modal')) {
            event.target.close();
        }
    });

    /* ---------------------------------------------------------- live filters */

    /*
     * A GET form with `data-live-filter="#results"` filters as you go: a
     * select applies on change, a text box a moment after you stop typing.
     * It asks for the same page the form would have navigated to and swaps
     * in only the results, so the box you're typing in is never reloaded out
     * from under you, and the address bar keeps up so the filtered view can
     * be linked and survives a refresh. Without the script it's a form with
     * a Filter button, as before.
     */
    var TYPING_PAUSE_MS = 300;

    function liveFilter(form) {
        var region = document.querySelector(form.dataset.liveFilter);
        if (!region) return;
        var pending = null;
        var timer = 0;

        var run = function () {
            var params = new URLSearchParams(new FormData(form));
            // A new filter starts at its first page.
            params.delete('page');
            Array.from(params.keys()).forEach(function (key) {
                if (!params.get(key)) params.delete(key);
            });
            var query = params.toString();
            var url = window.location.pathname + (query ? '?' + query : '');

            if (pending) pending.abort();
            pending = new AbortController();
            region.classList.add('is-loading');
            region.setAttribute('aria-busy', 'true');

            fetch(url, { credentials: 'same-origin', signal: pending.signal })
                .then(function (response) {
                    if (!response.ok) throw new Error(String(response.status));
                    return response.text();
                })
                .then(function (html) {
                    var doc = new DOMParser().parseFromString(html, 'text/html');
                    var fresh = doc.querySelector(form.dataset.liveFilter);
                    if (!fresh) throw new Error('no results region');
                    region.replaceChildren.apply(region, Array.from(fresh.childNodes).map(function (node) {
                        return document.importNode(node, true);
                    }));
                    history.replaceState(history.state, '', url);
                    region.classList.remove('is-loading');
                    region.removeAttribute('aria-busy');
                }, function (error) {
                    if (error.name === 'AbortError') return;
                    region.classList.remove('is-loading');
                    region.removeAttribute('aria-busy');
                    toast('Couldn’t apply that filter. Try again.', 'bad');
                });
        };

        form.addEventListener('submit', function (event) {
            event.preventDefault();
            clearTimeout(timer);
            run();
        });
        form.addEventListener('change', function (event) {
            if (event.target.tagName === 'SELECT' || event.target.type === 'checkbox' || event.target.type === 'radio') {
                clearTimeout(timer);
                run();
            }
        });
        form.addEventListener('input', function (event) {
            if (event.target.tagName !== 'INPUT' || event.target.type === 'checkbox' || event.target.type === 'radio') return;
            clearTimeout(timer);
            timer = setTimeout(run, TYPING_PAUSE_MS);
        });
    }

    function startLiveFilters() {
        document.querySelectorAll('form[data-live-filter]').forEach(liveFilter);
    }

    /* ------------------------------------------------------------- csrf */

    function csrf() {
        var field = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (field) return field.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    /*
     * A POST that answers in JSON. Resolves with the parsed body (or {} if
     * there wasn't one) and the status; only a dropped connection rejects, so
     * callers deal in what the server said rather than in exceptions.
     */
    function postJSON(url, body) {
        return fetch(url, {
            method: 'POST',
            body: body || null,
            credentials: 'same-origin',
            headers: {
                'X-CSRFToken': csrf(),
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json',
            },
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                return { status: response.status, data: data };
            });
        });
    }

    /* ---------------------------------------------------------- addresses */

    /*
     * "View address", wherever it appears (the shop, an item, fulfillment):
     * the address is never in the page, only fetched on a click and shown
     * beside the button until the next one. What to say when there isn't one
     * differs by who's looking, so the page passes its own words in.
     */
    function viewAddress(button, box, words) {
        words = words || {};
        var showLabel = button.dataset.atlShowLabel || button.textContent.trim();
        button.dataset.atlShowLabel = showLabel;

        if (!box.hidden) {
            box.hidden = true;
            button.textContent = showLabel;
            return Promise.resolve();
        }

        var line = function (text, className) {
            var div = document.createElement('div');
            if (className) div.className = className;
            div.textContent = text;
            box.appendChild(div);
        };

        button.disabled = true;
        return postJSON(button.dataset.url).then(function (result) {
            var data = result.data;
            box.textContent = '';
            if (data.ok) {
                // Who the buyer is, per Hack Club identity, above where the
                // parcel goes — only where the server sends it (fulfillment),
                // and only the claims HCA actually gave.
                var contact = data.contact || {};
                [contact.name, contact.email, contact.phone].forEach(function (value) {
                    if (value) line(value, 'address-contact');
                });
                data.address.lines.forEach(function (text) { line(text); });
                button.textContent = showLabel.replace(/^View/, 'Hide');
            } else if (data.error === 'no_address') {
                box.textContent = words.noAddress || 'No address on file.';
            } else if (data.error === 'address_unavailable') {
                box.textContent = words.unavailable || "Couldn't reach Hack Club identity. Try again later.";
            } else {
                box.textContent = 'Could not load the address. Please try again.';
            }
            box.hidden = false;
        }, function () {
            box.textContent = 'Could not load the address. Please try again.';
            box.hidden = false;
        }).then(function () {
            button.disabled = false;
        });
    }

    /* ------------------------------------------------------------- start */

    window.Atlantis = {
        toast: toast,
        confirm: confirm,
        csrf: csrf,
        setBusy: setBusy,
        postJSON: postJSON,
        viewAddress: viewAddress,
        openDialog: openDialog,
    };

    function start() {
        armServerNotes();
        startLiveFilters();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
