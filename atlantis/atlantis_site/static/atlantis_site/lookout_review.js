/*
 * The Lookout removal editor.
 *
 * A timelapse reviewer's job is to watch footage and say which stretches of it
 * don't count. The slow part was never the judgement — it was transcribing
 * timecodes: pause, read the player, type "1:42" into a box, scrub on, pause,
 * read, type. "Cut from here" removes that entirely: press it where the dead
 * time starts, press it again where it ends, and the range is written for you.
 *
 * Everything here is a preview. views/admin/timelapse_review.py re-parses and
 * re-validates every range that arrives, and it is that pass — not this one —
 * that decides what a lapse is worth.
 */
(function () {
    'use strict';

    // One second of video is a recorded minute. Mirrors
    // models.TRACKED_SECONDS_PER_VIDEO_SECOND.
    var TRACKED_PER_VIDEO_SECOND = 60;

    var template = document.getElementById('removal-row-template');
    var form = document.getElementById('timelapse-review-form');
    if (!template || !form) return;

    var trackedSeconds = parseInt(form.dataset.trackedSeconds, 10) || 0;
    var summary = document.getElementById('removal-summary');

    /* ------------------------------------------------------------ timecode */

    function parseTimecode(value) {
        var parts = String(value || '').trim().split(':');
        if (parts.length < 1 || parts.length > 3) return null;
        if (!parts.every(function (part) { return /^\d+$/.test(part.trim()); })) return null;
        var numbers = parts.map(function (part) { return parseInt(part, 10); });
        if (numbers.slice(1).some(function (number) { return number > 59; })) return null;
        return numbers.reduce(function (total, number) { return total * 60 + number; }, 0);
    }

    function formatTimecode(seconds) {
        seconds = Math.max(0, Math.floor(seconds));
        var minutes = Math.floor(seconds / 60);
        var rest = seconds % 60;
        if (minutes >= 60) {
            return Math.floor(minutes / 60) + ':' + String(minutes % 60).padStart(2, '0') +
                ':' + String(rest).padStart(2, '0');
        }
        return minutes + ':' + String(rest).padStart(2, '0');
    }

    function trackedDisplay(seconds) {
        var minutes = Math.floor(seconds / 60);
        return minutes >= 60
            ? Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm'
            : minutes + 'm';
    }

    function hoursDisplay(seconds) {
        var minutes = Math.max(0, Math.floor(seconds / 60));
        return Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm';
    }

    /* --------------------------------------------------------------- rows */

    function cardFor(node) {
        return node.closest('.rv-lookout');
    }

    function rowsFor(card) {
        return card.querySelector('[data-removal-rows]');
    }

    function videoFor(card) {
        return card.querySelector('.rv-lookout-video');
    }

    function addRow(card, start, end) {
        var host = rowsFor(card);
        var fragment = template.content.cloneNode(true);
        fragment.querySelector('input[name="removal_session"]').value = card.dataset.sessionId;
        if (start != null) fragment.querySelector('input[name="removal_start"]').value = formatTimecode(start);
        if (end != null) fragment.querySelector('input[name="removal_end"]').value = formatTimecode(end);
        host.appendChild(fragment);
        var row = host.lastElementChild;
        // Straight to the reason: the range is already written, and the reason
        // is the only part a person still has to supply.
        var next = start != null ? row.querySelector('input[name="removal_reason"]')
                                 : row.querySelector('input[name="removal_start"]');
        next.focus();
        render();
        return row;
    }

    /*
     * The range a "cut from here" has opened but not yet closed, per Lookout.
     * Held as the row itself so a reviewer can still type over it by hand.
     */
    function openRow(card) {
        var rows = rowsFor(card);
        if (!rows) return null;
        var candidates = rows.querySelectorAll('.rv-removal-row');
        for (var i = candidates.length - 1; i >= 0; i -= 1) {
            var row = candidates[i];
            var start = row.querySelector('input[name="removal_start"]').value.trim();
            var end = row.querySelector('input[name="removal_end"]').value.trim();
            if (start && !end) return row;
        }
        return null;
    }

    function cutHere(card) {
        var video = videoFor(card);
        if (!video) return;
        var at = Math.floor(video.currentTime || 0);
        var pending = openRow(card);
        if (!pending) {
            addRow(card, at, null);
            return;
        }
        var startValue = parseTimecode(pending.querySelector('input[name="removal_start"]').value);
        if (startValue !== null && at <= startValue) {
            window.rvToast && window.rvToast('That range would end before it starts — scrub forward first.', 'bad');
            return;
        }
        pending.querySelector('input[name="removal_end"]').value = formatTimecode(at);
        pending.querySelector('input[name="removal_reason"]').focus();
        render();
    }

    function updateCutButtons() {
        document.querySelectorAll('.rv-lookout').forEach(function (card) {
            var button = card.querySelector('[data-cut-here]');
            if (!button) return;
            button.textContent = openRow(card) ? 'Cut to here' : 'Cut from here';
        });
    }

    /* -------------------------------------------------------------- totals */

    function render() {
        var total = 0;
        var unreadable = false;
        var rowCount = 0;

        document.querySelectorAll('.rv-lookout').forEach(function (card) {
            var cardTotal = 0;
            var limit = parseInt(card.dataset.videoSeconds, 10) || 0;
            card.querySelectorAll('.rv-removal-row').forEach(function (row) {
                rowCount += 1;
                var startField = row.querySelector('input[name="removal_start"]');
                var endField = row.querySelector('input[name="removal_end"]');
                var cost = row.querySelector('[data-removal-cost]');
                var start = parseTimecode(startField.value);
                var end = parseTimecode(endField.value);

                var bad = (startField.value.trim() && start === null) ||
                          (endField.value.trim() && end === null) ||
                          (start !== null && end !== null && end <= start) ||
                          (limit && end !== null && end > limit);
                row.classList.toggle('rv-removal-bad', Boolean(bad));

                if (start === null || end === null || end <= start) {
                    unreadable = true;
                    if (cost) cost.textContent = bad ? 'unreadable' : '';
                    return;
                }
                var seconds = (end - start) * TRACKED_PER_VIDEO_SECOND;
                cardTotal += seconds;
                total += seconds;
                if (cost) cost.textContent = '−' + trackedDisplay(seconds);
            });

            var readout = card.querySelector('[data-session-total]');
            if (readout) {
                readout.textContent = cardTotal
                    ? '−' + trackedDisplay(cardTotal) + ' from this Lookout'
                    : 'nothing removed';
                readout.classList.toggle('is-no', Boolean(cardTotal));
            }
        });

        document.querySelectorAll('[data-live-removed]').forEach(function (el) {
            el.textContent = hoursDisplay(total);
            el.classList.toggle('is-no', total > 0);
        });
        document.querySelectorAll('[data-live-approved]').forEach(function (el) {
            el.textContent = hoursDisplay(Math.max(trackedSeconds - total, 0));
        });

        if (summary) {
            if (!rowCount) {
                summary.textContent = 'No time removed.';
            } else {
                var label = rowCount + (rowCount === 1 ? ' range' : ' ranges');
                summary.textContent = unreadable
                    ? label + ' — ' + trackedDisplay(total) + ' so far, plus the ranges still being filled in.'
                    : label + ' — ' + trackedDisplay(total) + ' will be removed.';
            }
        }

        updateCutButtons();
    }

    /* ------------------------------------------------------------- wiring */

    document.addEventListener('click', function (event) {
        var cut = event.target.closest('[data-cut-here]');
        if (cut) { cutHere(cardFor(cut)); return; }

        var add = event.target.closest('[data-add-removal]');
        if (add) { addRow(cardFor(add), null, null); return; }

        var drop = event.target.closest('[data-drop-removal]');
        if (drop) { drop.closest('.rv-removal-row').remove(); render(); return; }

        var seek = event.target.closest('[data-seek-start]');
        if (seek) {
            var row = seek.closest('.rv-removal-row');
            var card = cardFor(seek);
            var video = videoFor(card);
            var at = parseTimecode(row.querySelector('input[name="removal_start"]').value);
            if (video && at !== null) {
                video.currentTime = at;
                video.play();
            }
        }
    });

    document.addEventListener('input', function (event) {
        if (event.target.closest('.rv-removal-row')) render();
    });

    var rate = document.querySelector('[data-playback-rate]');
    if (rate) {
        rate.addEventListener('change', function () {
            document.querySelectorAll('.rv-lookout-video').forEach(function (video) {
                video.playbackRate = parseFloat(rate.value) || 1;
            });
        });
    }

    render();
})();
