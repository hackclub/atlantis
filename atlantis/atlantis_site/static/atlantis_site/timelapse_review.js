/*
 * The timelapse annotation editor.
 *
 * A timelapse reviewer's job is to watch footage, say what it shows, and mark
 * the stretches of it that don't count. This file is the second and third
 * halves of that: a timeline you can draw on, and a per-recording description
 * you have to write before the pass will submit.
 *
 * Two timelines, stacked, per recording:
 *
 *   ████░░░░░░██░░░░░░░░░░░░░░░░  ← what the reviewer is removing
 *   ▁▁▁▁▁▁▓▓▓▓▁▁▁▁▁▁▁▓▓▓▁▁▁▁▁▁▁▁  ← where the checker found nothing changing
 *   ┊                             ← the playhead, across both
 *
 * The top bar is a decision, the bottom one is a hint. The checker never
 * removes anything by itself — it only says where to look, and a deduction
 * nobody typed a reason for is a deduction nobody can defend later.
 *
 * Units. Lapse stitches one recorded minute into one second of compiled
 * video, so every offset here is in *video seconds* — the only timeline a
 * reviewer can see — and one of them is worth sixty tracked seconds. The
 * numeric inputs are labelled "min" for that reason: they are video seconds,
 * and a video second is a recorded minute.
 *
 * Everything here is a preview. views/admin/timelapse_review.py re-parses and
 * re-validates every range that arrives, and it is that pass — not this one —
 * that decides what a lapse is worth.
 */
(function () {
    'use strict';

    var TRACKED_PER_VIDEO_SECOND = 60;
    var RATE_KEY = 'timelapse_playback_rate';
    var RATES = [0.5, 1, 1.5, 2, 3, 4];
    /* How close to a key point a drawn edge has to land before it snaps to it:
     * the wider of 1.5% of the video and three seconds. Below that it just
     * quantises to whole seconds, which is the finest offset that means
     * anything. */
    var SNAP_FRACTION = 0.015;
    var SNAP_FLOOR = 3;

    var payloadEl = document.getElementById('ta-payload');
    var root = document.getElementById('ta-root');
    if (!payloadEl || !root) return;

    var payload;
    try {
        payload = JSON.parse(payloadEl.textContent);
    } catch (e) {
        return;
    }

    var form = document.getElementById('timelapse-review-form');
    var finalDialog = document.getElementById('ta-final');
    var draftKey = 'timelapse-draft:' + payload.projectId;

    /* A key that means something on the page must not mean it mid-sentence. */
    function isTyping(el) {
        if (!el) return false;
        var tag = el.tagName;
        return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
    }

    /* ---------------------------------------------------------------- time */

    function formatTimecode(seconds) {
        seconds = Math.max(0, Math.round(seconds));
        var minutes = Math.floor(seconds / 60);
        var rest = seconds % 60;
        if (minutes >= 60) {
            return Math.floor(minutes / 60) + ':' + String(minutes % 60).padStart(2, '0') +
                ':' + String(rest).padStart(2, '0');
        }
        return minutes + ':' + String(rest).padStart(2, '0');
    }

    /* Tracked seconds, the way the rest of the admin writes a duration. */
    function hoursDisplay(seconds) {
        var minutes = Math.max(0, Math.floor(seconds / 60));
        return Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm';
    }

    /* Compact, for the places a whole "0h 45m" is more than the line can hold. */
    function shortDuration(seconds) {
        seconds = Math.max(0, Math.round(seconds));
        if (seconds < 60) return seconds + 's';
        var minutes = Math.floor(seconds / 60);
        var hours = Math.floor(minutes / 60);
        return hours ? hours + 'h ' + (minutes % 60) + 'm' : minutes + 'm';
    }

    function videoToTracked(videoSeconds) {
        return Math.round(videoSeconds) * TRACKED_PER_VIDEO_SECOND;
    }

    /* --------------------------------------------------------------- state */

    /*
     * One record per recording, keyed by id. `segments` is the editable set;
     * a read-only recording's came from the database and are drawn but never
     * touched.
     */
    var recordings = {};
    var entries = [];

    payload.entries.forEach(function (entry) {
        var ids = [];
        entry.recordings.forEach(function (rec) {
            ids.push(rec.id);
            recordings[rec.id] = {
                id: rec.id,
                entryId: entry.id,
                editable: entry.editable,
                videoSeconds: rec.videoSeconds,
                trackedSeconds: rec.trackedSeconds,
                activityChecked: rec.activityChecked,
                inactivePercentage: rec.inactivePercentage,
                inactiveSegments: rec.inactiveSegments || [],
                segments: (rec.segments || []).map(function (segment) {
                    return { start: segment.start, end: segment.end, reason: segment.reason };
                }),
                description: rec.description || '',
                saved: !entry.editable,
                // The range a "cut from here" has opened but not yet closed.
                pendingCut: null,
                el: null,
                video: null,
                currentTime: 0,
            };
        });
        entries.push({ id: entry.id, editable: entry.editable, recordingIds: ids });
    });

    /* ------------------------------------------------------------- drafting */

    /*
     * A pass is one form post at the end, so a reload mid-project would
     * otherwise cost the reviewer everything they had written. The draft is
     * per project and per browser; it is a convenience, not a record, and the
     * server never sees it.
     */
    function saveDraft() {
        var draft = {};
        Object.keys(recordings).forEach(function (id) {
            var rec = recordings[id];
            if (!rec.editable) return;
            draft[id] = {
                segments: rec.segments,
                description: rec.description,
                saved: rec.saved,
            };
        });
        var notes = document.getElementById('ta-internal');
        try {
            localStorage.setItem(draftKey, JSON.stringify({
                recordings: draft,
                notes: notes ? notes.value : '',
            }));
        } catch (e) { /* private mode; the pass still submits */ }
    }

    function loadDraft() {
        var raw;
        try {
            raw = localStorage.getItem(draftKey);
        } catch (e) { return; }
        if (!raw) return;
        var draft;
        try {
            draft = JSON.parse(raw);
        } catch (e) { return; }

        Object.keys(draft.recordings || {}).forEach(function (id) {
            var rec = recordings[id];
            if (!rec || !rec.editable) return;
            var saved = draft.recordings[id];
            rec.segments = (saved.segments || []).filter(function (segment) {
                // A draft outlives the page it was written on; a range past the
                // end of the video is one the server would refuse anyway.
                return segment.end > segment.start && segment.end <= rec.videoSeconds;
            });
            rec.description = saved.description || '';
            rec.saved = Boolean(saved.saved) && Boolean(rec.description.trim());
        });

        var notes = document.getElementById('ta-internal');
        if (notes && draft.notes && !notes.value) notes.value = draft.notes;
    }

    function clearDraft() {
        try {
            localStorage.removeItem(draftKey);
        } catch (e) { /* nothing to clear */ }
    }

    /* ------------------------------------------------------------ geometry */

    function snapPoints(rec) {
        var points = [0, rec.videoSeconds];
        rec.segments.forEach(function (segment) {
            points.push(segment.start, segment.end);
        });
        rec.inactiveSegments.forEach(function (segment) {
            points.push(segment.start, segment.end);
        });
        return points.sort(function (a, b) { return a - b; });
    }

    /* Land on a key point when close enough to one, else on a whole second. */
    function snap(rec, time) {
        var threshold = Math.max(rec.videoSeconds * SNAP_FRACTION, SNAP_FLOOR);
        var closest = null;
        var best = threshold;
        snapPoints(rec).forEach(function (point) {
            var distance = Math.abs(time - point);
            if (distance < best) {
                best = distance;
                closest = point;
            }
        });
        return closest === null ? Math.round(time) : closest;
    }

    function nextSnapAfter(rec, time) {
        var found = snapPoints(rec).filter(function (point) { return point > time + 1; })[0];
        return found === undefined ? rec.videoSeconds : found;
    }

    function overlaps(rec, start, end, ignore) {
        return rec.segments.some(function (segment, index) {
            if (index === ignore) return false;
            return start < segment.end && segment.start < end;
        });
    }

    function removedSeconds(rec) {
        return rec.segments.reduce(function (total, segment) {
            return total + videoToTracked(segment.end - segment.start);
        }, 0);
    }

    function approvedSeconds(rec) {
        return Math.max(rec.trackedSeconds - removedSeconds(rec), 0);
    }

    /* -------------------------------------------------------------- render */

    function percent(value, of) {
        return of > 0 ? (value / of) * 100 : 0;
    }

    function renderTimeline(rec) {
        var main = rec.el.querySelector('[data-track-main]');
        var activity = rec.el.querySelector('[data-track-activity]');
        var legend = rec.el.querySelector('[data-legend]');
        if (!main) return;

        var preview = main.querySelector('[data-preview]');
        Array.prototype.slice.call(main.querySelectorAll('.ta-seg')).forEach(function (node) {
            node.remove();
        });

        rec.segments.forEach(function (segment) {
            var node = document.createElement('span');
            node.className = 'ta-seg';
            node.style.left = percent(segment.start, rec.videoSeconds) + '%';
            node.style.width = Math.max(percent(segment.end - segment.start, rec.videoSeconds), 0.5) + '%';
            node.title = '−' + shortDuration(videoToTracked(segment.end - segment.start)) +
                (segment.reason ? ': ' + segment.reason : '');
            main.insertBefore(node, preview);
        });

        if (activity && !activity.dataset.drawn) {
            rec.inactiveSegments.forEach(function (segment) {
                var node = document.createElement('span');
                node.className = 'ta-inactive';
                node.style.left = percent(segment.start, rec.videoSeconds) + '%';
                node.style.width = Math.max(percent(segment.end - segment.start, rec.videoSeconds), 0.3) + '%';
                node.title = 'Inactive: ' + formatTimecode(segment.start) + ' – ' + formatTimecode(segment.end);
                activity.appendChild(node);
            });
            activity.dataset.drawn = '1';
        }

        renderLegend(rec, legend);
    }

    /*
     * Only the colours actually on this timeline. "Removed" appears once
     * something has been removed, and the second track says which of three
     * things it is: found inactivity, found none, or was never analysed —
     * "clean" and "unknown" are different claims and the reviewer is owed
     * the difference.
     */
    function renderLegend(rec, legend) {
        if (!legend) return;
        var keys = [];
        if (rec.segments.length) keys.push(['ta-key-removed', 'Removed']);
        if (rec.inactiveSegments.length) {
            keys.push(['ta-key-inactive', 'Inactive']);
        } else if (!rec.activityChecked) {
            keys.push(['ta-key-unknown', 'Not analysed']);
        } else {
            keys.push(['ta-key-clean', 'No inactivity found']);
        }

        legend.innerHTML = '';
        keys.forEach(function (key) {
            var node = document.createElement('span');
            node.className = 'ta-key';
            var swatch = document.createElement('i');
            swatch.className = key[0];
            node.appendChild(swatch);
            node.appendChild(document.createTextNode(key[1]));
            legend.appendChild(node);
        });
    }

    function renderPreview(rec, range) {
        var preview = rec.el.querySelector('[data-preview]');
        if (!preview) return;
        if (!range || range.end <= range.start) {
            preview.hidden = true;
            return;
        }
        preview.hidden = false;
        preview.style.left = percent(range.start, rec.videoSeconds) + '%';
        preview.style.width = Math.max(percent(range.end - range.start, rec.videoSeconds), 0.5) + '%';
    }

    function renderCursor(rec) {
        var cursor = rec.el.querySelector('[data-cursor]');
        if (cursor) cursor.style.left = percent(rec.currentTime, rec.videoSeconds) + '%';
        var timeline = rec.el.querySelector('[data-timeline]');
        if (timeline) timeline.setAttribute('aria-valuenow', String(Math.round(rec.currentTime)));
    }

    function renderSegments(rec) {
        var host = rec.el.querySelector('[data-segments]');
        if (!host || !rec.editable) return;
        host.innerHTML = '';

        rec.segments
            .map(function (segment, index) { return { segment: segment, index: index }; })
            .sort(function (a, b) { return a.segment.start - b.segment.start; })
            .forEach(function (row) {
                var segment = row.segment;
                var node = document.createElement('div');
                node.className = 'ta-segment';
                node.innerHTML =
                    '<span class="ta-segment-type">Removed</span>' +
                    '<span class="ta-segment-range"></span>' +
                    '<span class="ta-segment-reason"></span>' +
                    '<span class="ta-segment-cost"></span>' +
                    '<button type="button" class="ta-segment-drop" title="Drop this range" ' +
                    'aria-label="Drop this range">&times;</button>';
                node.querySelector('.ta-segment-range').textContent =
                    formatTimecode(segment.start) + ' – ' + formatTimecode(segment.end);
                node.querySelector('.ta-segment-reason').textContent = segment.reason;
                node.querySelector('.ta-segment-cost').textContent =
                    '−' + shortDuration(videoToTracked(segment.end - segment.start));
                node.querySelector('.ta-segment-drop').addEventListener('click', function () {
                    rec.segments.splice(row.index, 1);
                    markUnsaved(rec);
                    renderRecording(rec);
                    renderTotals();
                    saveDraft();
                });
                node.querySelector('.ta-segment-range').addEventListener('click', function () {
                    seek(rec, segment.start);
                });
                host.appendChild(node);
            });
    }

    function renderRecordingSummary(rec) {
        if (!rec.editable) return;
        var approved = rec.el.querySelector('[data-rec-approved]');
        var removed = rec.el.querySelector('[data-rec-removed]');
        if (approved) approved.textContent = hoursDisplay(approvedSeconds(rec));
        if (removed) {
            var cut = removedSeconds(rec);
            removed.hidden = cut === 0;
            removed.textContent = '−' + shortDuration(cut) + ' removed';
        }

        var button = rec.el.querySelector('[data-save-recording]');
        var label = rec.el.querySelector('[data-save-label]');
        if (button && label) {
            var described = Boolean(rec.description.trim());
            button.classList.toggle('is-saved', rec.saved);
            button.disabled = !described && !rec.saved;
            label.textContent = rec.saved ? 'Saved' : 'Save';
        }
    }

    function renderRecording(rec) {
        renderTimeline(rec);
        renderSegments(rec);
        renderRecordingSummary(rec);
        renderCursor(rec);
    }

    function entryIsDone(entry) {
        if (!entry.editable) return true;
        if (!entry.recordingIds.length) return true;
        return entry.recordingIds.every(function (id) {
            var rec = recordings[id];
            return rec.saved && rec.description.trim();
        });
    }

    function renderTotals() {
        var removed = 0;
        var tracked = 0;

        entries.forEach(function (entry) {
            if (!entry.editable) return;
            var entryTracked = 0;
            var entryRemoved = 0;
            entry.recordingIds.forEach(function (id) {
                entryTracked += recordings[id].trackedSeconds;
                entryRemoved += removedSeconds(recordings[id]);
            });
            tracked += entryTracked;
            removed += entryRemoved;

            var section = document.querySelector('[data-entry-id="' + entry.id + '"]');
            if (!section) return;
            var clock = section.querySelector('[data-entry-time]');
            if (clock) {
                clock.textContent = entryRemoved
                    ? hoursDisplay(Math.max(entryTracked - entryRemoved, 0)) + ' / ' + hoursDisplay(entryTracked)
                    : hoursDisplay(entryTracked);
                clock.classList.toggle('is-no', entryRemoved > 0);
            }
            var done = section.querySelector('[data-entry-done]');
            if (done) done.hidden = !entryIsDone(entry);
        });

        document.querySelectorAll('[data-live-removed]').forEach(function (el) {
            el.textContent = hoursDisplay(removed);
            el.classList.toggle('is-no', removed > 0);
        });
        document.querySelectorAll('[data-live-approved]').forEach(function (el) {
            el.textContent = hoursDisplay(Math.max(tracked - removed, 0));
        });

        renderSubmitState();
    }

    /*
     * Submit is gated on every recording in the pass being described and
     * saved — the same rule the server enforces, said early enough to be
     * useful. The progress line names what is left rather than just refusing.
     */
    function renderSubmitState() {
        var outstanding = [];
        entries.forEach(function (entry) {
            if (!entry.editable) return;
            entry.recordingIds.forEach(function (id) {
                var rec = recordings[id];
                if (!rec.saved || !rec.description.trim()) outstanding.push(rec);
            });
        });

        var ready = outstanding.length === 0;
        document.querySelectorAll('[data-submit-pass]').forEach(function (button) {
            // Approve lives inside the sign-off panel, and [data-primary] is
            // looked up across the whole document: left enabled while the
            // panel is shut, Cmd+Enter would post the pass without ever
            // showing the reviewer the notes field it exists to raise.
            var panel = button.closest('dialog');
            button.disabled = !ready || Boolean(panel && !panel.open);
            button.title = ready
                ? 'Approve every waiting lapse on this project'
                : 'Describe and save every timelapse before submitting';
        });

        var progress = document.querySelector('[data-save-progress]');
        if (progress) {
            progress.textContent = ready
                ? 'Every timelapse in this pass has been described.'
                : outstanding.length + ' timelapse' + (outstanding.length === 1 ? '' : 's') +
                  ' still to describe and save.';
            progress.classList.toggle('is-no', !ready);
        }
    }

    /* --------------------------------------------------------- sign-off panel */

    /*
     * The pass ends in a modal rather than at the bottom of the column: the
     * notes are the last thing written and the first thing a reviewer reaches
     * for, and scrolling past every entry to find them was the long way round.
     */
    function openFinal() {
        if (!finalDialog || finalDialog.open) return;
        finalDialog.showModal();
        renderSubmitState();
        var notes = document.getElementById('ta-internal');
        if (notes) notes.focus();
    }

    function closeFinal() {
        if (finalDialog && finalDialog.open) finalDialog.close();
    }

    function setupFinal() {
        if (!finalDialog) return;
        document.querySelectorAll('[data-open-final]').forEach(function (button) {
            button.addEventListener('click', openFinal);
        });
        document.querySelectorAll('[data-close-final]').forEach(function (button) {
            button.addEventListener('click', closeFinal);
        });
        // Esc closes it without going through the button, so re-gate from the
        // event the dialog fires either way.
        finalDialog.addEventListener('close', renderSubmitState);
    }

    function markUnsaved(rec) {
        rec.saved = false;
    }

    /* ------------------------------------------------------------ playback */

    function seek(rec, videoSeconds) {
        rec.currentTime = Math.max(0, Math.min(videoSeconds, rec.videoSeconds));
        if (rec.video) rec.video.currentTime = rec.currentTime;
        renderCursor(rec);
    }

    function storedRate() {
        var saved;
        try {
            saved = parseFloat(localStorage.getItem(RATE_KEY));
        } catch (e) { return null; }
        return RATES.indexOf(saved) === -1 ? null : saved;
    }

    /*
     * One rate for every player on the page, remembered between projects. A
     * reviewer who watches at 2x watches everything at 2x, and re-choosing it
     * per recording is exactly the kind of friction this page exists to remove.
     */
    function setupPlaybackRate() {
        var select = document.querySelector('[data-playback-rate]');
        var initial = storedRate();
        if (select && initial !== null) select.value = String(initial);

        function apply(rate) {
            document.querySelectorAll('.ta-video').forEach(function (video) {
                video.playbackRate = rate;
            });
        }

        if (initial !== null) apply(initial);

        if (select) {
            select.addEventListener('change', function () {
                var rate = parseFloat(select.value) || 1;
                try { localStorage.setItem(RATE_KEY, String(rate)); } catch (e) { /* ignore */ }
                apply(rate);
            });
        }

        // A player that loads its source late resets its own rate; put it back.
        document.querySelectorAll('.ta-video').forEach(function (video) {
            video.addEventListener('loadedmetadata', function () {
                var rate = select ? parseFloat(select.value) : storedRate();
                if (rate) video.playbackRate = rate;
            });
            // The rate menu in the browser's own controls is a legitimate way
            // to change it; follow that rather than fighting it.
            video.addEventListener('ratechange', function () {
                if (!select) return;
                var rate = video.playbackRate;
                if (RATES.indexOf(rate) === -1 || String(rate) === select.value) return;
                select.value = String(rate);
                try { localStorage.setItem(RATE_KEY, String(rate)); } catch (e) { /* ignore */ }
                apply(rate);
            });
        });
    }

    /* -------------------------------------------------------------- wiring */

    function wireTimeline(rec) {
        var timeline = rec.el.querySelector('[data-timeline]');
        if (!timeline) return;
        var dragging = false;

        function timeFrom(clientX) {
            var rect = timeline.getBoundingClientRect();
            if (!rect.width) return 0;
            var ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
            return snap(rec, ratio * rec.videoSeconds);
        }

        timeline.addEventListener('pointerdown', function (event) {
            dragging = true;
            timeline.setPointerCapture(event.pointerId);
            seek(rec, timeFrom(event.clientX));
        });
        timeline.addEventListener('pointermove', function (event) {
            if (dragging) seek(rec, timeFrom(event.clientX));
        });
        timeline.addEventListener('pointerup', function () {
            dragging = false;
        });
        timeline.addEventListener('keydown', function (event) {
            var step = event.shiftKey ? 10 : 1;
            if (event.key === 'ArrowLeft') {
                event.preventDefault();
                seek(rec, rec.currentTime - step);
            } else if (event.key === 'ArrowRight') {
                event.preventDefault();
                seek(rec, rec.currentTime + step);
            }
        });
    }

    /*
     * The length the page was served is derived: Lapse reports the recorded
     * time that went into a timelapse, and sixty of those seconds is one
     * second of video, which is close but not the file's own duration. So the
     * player is asked for the real one the moment it knows it,
     * and everything measured against the video — the timeline's scale, the
     * far end of a cut, the length in the header — is redrawn to agree with
     * the clock the reviewer is actually reading.
     */
    function applyMeasuredLength(rec, duration) {
        if (!isFinite(duration)) return;
        // Truncated, like the player's own clock: a video running 70.9 seconds
        // ends at 1:10 there, and offering a cut to 1:11 would be offering a
        // second the footage doesn't have.
        var measured = Math.floor(duration);
        if (measured < 1 || measured === rec.videoSeconds) return;
        rec.videoSeconds = measured;

        var length = rec.el.querySelector('[data-rec-video-len]');
        if (length) length.textContent = formatTimecode(measured);

        var timeline = rec.el.querySelector('[data-timeline]');
        if (timeline) timeline.setAttribute('aria-valuemax', String(measured));
        ['[data-add-start]', '[data-add-end]'].forEach(function (selector) {
            var input = rec.el.querySelector(selector);
            if (input) input.max = String(measured);
        });

        // The activity track is drawn once and then left alone; its bars are
        // placed as a fraction of the length, so they have to be placed again.
        var activity = rec.el.querySelector('[data-track-activity]');
        if (activity) {
            delete activity.dataset.drawn;
            activity.innerHTML = '';
        }
        renderRecording(rec);
    }

    function wireVideo(rec) {
        var video = rec.el.querySelector('.ta-video');
        if (!video) return;
        rec.video = video;
        video.addEventListener('loadedmetadata', function () {
            applyMeasuredLength(rec, video.duration);
        });
        // Metadata a cached player already had, which fires no event.
        if (video.readyState >= 1) applyMeasuredLength(rec, video.duration);
        var frame = 0;
        function tick() {
            if (Math.abs(video.currentTime - rec.currentTime) > 0.02) {
                rec.currentTime = video.currentTime;
                renderCursor(rec);
            }
            frame = requestAnimationFrame(tick);
        }
        video.addEventListener('play', function () {
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(tick);
        });
        video.addEventListener('pause', function () { cancelAnimationFrame(frame); });
        video.addEventListener('seeked', function () {
            rec.currentTime = video.currentTime;
            renderCursor(rec);
        });
    }

    function addForm(rec) {
        return {
            root: rec.el.querySelector('[data-add-form]'),
            start: rec.el.querySelector('[data-add-start]'),
            end: rec.el.querySelector('[data-add-end]'),
            reason: rec.el.querySelector('[data-add-reason]'),
            error: rec.el.querySelector('[data-add-error]'),
        };
    }

    function openAdd(rec, start, end) {
        var fields = addForm(rec);
        if (!fields.root) return;
        var from = start === undefined ? Math.round(rec.currentTime) : start;
        var to = end === undefined ? nextSnapAfter(rec, from) : end;
        fields.start.value = String(from);
        fields.end.value = String(Math.max(to, from + 1));
        fields.reason.value = '';
        fields.error.hidden = true;
        fields.root.hidden = false;
        rec.el.querySelector('[data-add-row]').hidden = true;
        fields.reason.focus();
        previewFromForm(rec);
    }

    function closeAdd(rec) {
        var fields = addForm(rec);
        if (!fields.root) return;
        fields.root.hidden = true;
        rec.el.querySelector('[data-add-row]').hidden = false;
        renderPreview(rec, null);
    }

    function previewFromForm(rec) {
        var fields = addForm(rec);
        if (!fields.root || fields.root.hidden) return;
        renderPreview(rec, {
            start: Number(fields.start.value) || 0,
            end: Number(fields.end.value) || 0,
        });
    }

    function confirmAdd(rec) {
        var fields = addForm(rec);
        var start = Math.round(Number(fields.start.value));
        var end = Math.round(Number(fields.end.value));
        var reason = fields.reason.value.trim();

        function fail(message) {
            fields.error.textContent = message;
            fields.error.hidden = false;
        }

        if (!(end > start)) return fail('That range has to end after it starts.');
        if (end > rec.videoSeconds) {
            return fail('That range runs past the end of this timelapse (' +
                formatTimecode(rec.videoSeconds) + ' of video).');
        }
        if (!reason) return fail('Every removed range needs a reason.');
        if (overlaps(rec, start, end)) return fail('That range overlaps one already on this timelapse.');

        rec.segments.push({ start: start, end: end, reason: reason });
        rec.pendingCut = null;
        markUnsaved(rec);
        closeAdd(rec);
        renderRecording(rec);
        renderTotals();
        saveDraft();
    }

    /*
     * "Cut from here": press it where the dead time starts, press it again
     * where it ends. The slow part of this job was never the judgement — it
     * was transcribing timecodes by hand between scrubs.
     */
    function cutHere(rec) {
        var at = Math.round(rec.currentTime);
        var button = rec.el.querySelector('[data-cut-here]');
        if (rec.pendingCut === null) {
            rec.pendingCut = at;
            if (button) button.textContent = 'Cut to here';
            renderPreview(rec, { start: at, end: at + 1 });
            return;
        }
        if (at <= rec.pendingCut) {
            if (window.rvToast) {
                window.rvToast('That range would end before it starts — scrub forward first.', 'bad');
            }
            return;
        }
        var start = rec.pendingCut;
        rec.pendingCut = null;
        if (button) button.textContent = 'Cut from here';
        openAdd(rec, start, at);
    }

    function wireEditor(rec) {
        if (!rec.editable) return;
        var el = rec.el;
        var fields = addForm(rec);

        el.querySelector('[data-open-add]').addEventListener('click', function () { openAdd(rec); });
        el.querySelector('[data-cut-here]').addEventListener('click', function () { cutHere(rec); });
        el.querySelector('[data-add-cancel]').addEventListener('click', function () {
            rec.pendingCut = null;
            el.querySelector('[data-cut-here]').textContent = 'Cut from here';
            closeAdd(rec);
        });
        el.querySelector('[data-add-confirm]').addEventListener('click', function () { confirmAdd(rec); });

        el.querySelector('[data-set-start]').addEventListener('click', function () {
            fields.start.value = String(Math.round(rec.currentTime));
            previewFromForm(rec);
        });
        el.querySelector('[data-set-end]').addEventListener('click', function () {
            fields.end.value = String(Math.round(rec.currentTime));
            previewFromForm(rec);
        });

        [fields.start, fields.end].forEach(function (input) {
            input.addEventListener('input', function () {
                fields.error.hidden = true;
                previewFromForm(rec);
            });
        });
        fields.reason.addEventListener('input', function () { fields.error.hidden = true; });
        fields.reason.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                event.preventDefault();
                confirmAdd(rec);
            }
        });

        el.querySelectorAll('[data-preset]').forEach(function (button) {
            button.addEventListener('click', function () {
                fields.reason.value = button.dataset.preset;
                fields.error.hidden = true;
                var presets = el.querySelector('.ta-presets');
                if (presets) presets.open = false;
                fields.reason.focus();
            });
        });

        var description = el.querySelector('[data-description]');
        description.value = rec.description;
        description.addEventListener('input', function () {
            rec.description = description.value;
            markUnsaved(rec);
            renderRecordingSummary(rec);
            renderTotals();
            saveDraft();
        });

        el.querySelector('[data-save-recording]').addEventListener('click', function () {
            saveRecording(rec);
        });
    }

    /*
     * "Save" doesn't post anything — the pass is one form submission at the
     * end — but it is not decorative either: it is the reviewer saying they
     * are done with this recording, which is what the Submit button is gated
     * on, and it puts the work somewhere a reload can't lose it.
     */
    function saveRecording(rec) {
        if (!rec.description.trim()) {
            if (window.rvToast) window.rvToast('Describe this timelapse before saving it.', 'bad');
            return false;
        }
        rec.saved = true;
        renderRecordingSummary(rec);
        renderTotals();
        saveDraft();
        return true;
    }

    /* ------------------------------------------------------------- entries */

    function setExpanded(section, open) {
        section.classList.toggle('is-open', open);
        var toggle = section.querySelector('[data-entry-toggle]');
        if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    function wireEntries() {
        document.querySelectorAll('[data-entry]').forEach(function (section) {
            setExpanded(section, section.dataset.open === '1');
            section.querySelector('[data-entry-toggle]').addEventListener('click', function () {
                setExpanded(section, !section.classList.contains('is-open'));
            });
        });
    }

    /*
     * Shift+Enter: finish the entry you are looking at and move to the next
     * one. The scroll column snaps, so "the entry you are looking at" is the
     * child whose top is nearest the scroll position.
     */
    function saveAndNext() {
        var scroller = document.getElementById('ta-scroll');
        if (!scroller) return;
        var children = Array.prototype.slice.call(scroller.children);
        var top = scroller.scrollTop;
        var current = 0;
        var best = Infinity;
        children.forEach(function (child, index) {
            var distance = Math.abs(child.offsetTop - top);
            if (distance < best) {
                best = distance;
                current = index;
            }
        });

        var section = children[current];
        if (section && section.dataset.entryId) {
            var entry = entries.filter(function (candidate) {
                return String(candidate.id) === section.dataset.entryId;
            })[0];
            if (entry && entry.editable) {
                entry.recordingIds.forEach(function (id) {
                    var rec = recordings[id];
                    if (!rec.saved && rec.description.trim()) saveRecording(rec);
                });
                // An entry that is finished folds itself away, unless it is the
                // last one — there is nothing after it to make room for.
                if (entryIsDone(entry) && section.dataset.last !== '1') {
                    setExpanded(section, false);
                }
            }
        }

        var next = children[current + 1];
        if (next) {
            next.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else {
            // Nothing below the last entry any more — the pass ends here.
            openFinal();
        }
    }

    /* -------------------------------------------------------------- submit */

    /*
     * The editor's state, written out as the wire format the view parses: four
     * parallel lists of ranges, plus one description per timelapse.
     */
    function serialise() {
        var host = form.querySelector('[data-removal-inputs]');
        host.innerHTML = '';

        function hidden(name, value) {
            var input = document.createElement('input');
            input.type = 'hidden';
            input.name = name;
            input.value = value;
            host.appendChild(input);
        }

        Object.keys(recordings).forEach(function (id) {
            var rec = recordings[id];
            if (!rec.editable) return;
            hidden('description_' + rec.id, rec.description.trim());
            rec.segments.forEach(function (segment) {
                hidden('removal_session', String(rec.id));
                hidden('removal_start', formatTimecode(segment.start));
                hidden('removal_end', formatTimecode(segment.end));
                hidden('removal_reason', segment.reason);
            });
        });
    }

    function setupSubmit() {
        if (!form) return;
        form.addEventListener('submit', function (event) {
            if (form.dataset.submitted === '1') {
                event.preventDefault();
                return;
            }
            serialise();
            form.dataset.submitted = '1';
            // Only once the browser has accepted the submission: a draft
            // cleared before a failed post would take the reviewer's work with it.
            clearDraft();
        });
    }

    /* ---------------------------------------------------------------- init */

    function init() {
        loadDraft();

        document.querySelectorAll('[data-recording]').forEach(function (el) {
            var rec = recordings[el.dataset.recordingId];
            if (!rec) return;
            rec.el = el;
            wireVideo(rec);
            wireTimeline(rec);
            wireEditor(rec);
            renderRecording(rec);
        });

        wireEntries();
        setupPlaybackRate();
        setupFinal();
        setupSubmit();
        renderTotals();

        document.addEventListener('keydown', function (event) {
            if (event.defaultPrevented) return;
            if (event.altKey) return;

            // Cmd/Ctrl+Enter raises the panel; from inside it, review.js has a
            // live [data-primary] to press and submits the pass instead.
            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                event.preventDefault();
                openFinal();
                return;
            }
            if (event.metaKey || event.ctrlKey) return;
            if (finalDialog && finalDialog.open) return;

            if (event.key === 'Enter' && event.shiftKey) {
                event.preventDefault();
                saveAndNext();
                return;
            }

            // I still means "the notes on the pass" — it just has to open the
            // panel holding them first. Not while something is being typed in.
            if (event.key.toLowerCase() === 'i' && !isTyping(event.target)) {
                event.preventDefault();
                openFinal();
            }
        });

        var notes = document.getElementById('ta-internal');
        if (notes) notes.addEventListener('input', saveDraft);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
