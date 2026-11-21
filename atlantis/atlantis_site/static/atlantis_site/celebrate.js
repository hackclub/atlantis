/* The effect played on the page after a review decision.
 *
 * templatetags/review_streak.py writes the #review-celebration payload only on
 * the one page a decision redirected to:
 *   approved   confetti, for approvals only
 *   onFire     embers off the streak badge, for a run of FIRE_AT or more
 *   milestone  a callout naming the streak (5, 10, 25, ...)
 *   dailyStreak  present only on the decision that met the day's goal
 *              (daily_streak.py): a callout with the days kept, and confetti
 *              whatever the decision was
 *
 * It plays over a review page, often over a playing timelapse, so it is kept
 * cheap on purpose: a few dozen flat particles, no blur, shadows or blending
 * (each of those makes the canvas redo every pixel it touches), a canvas at
 * CSS resolution rather than the screen's, and a hard stop at MAX_MS. The
 * canvas is removed when it's done, so nothing runs once the effect has
 * finished. It makes no sound. Anyone who has asked for reduced motion gets
 * the milestone callout and nothing that moves.
 */
(function () {
    "use strict";

    var source = document.getElementById("review-celebration");
    if (!source) return;
    var data;
    try {
        data = JSON.parse(source.textContent);
    } catch (e) {
        return;
    }

    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Half as much on a machine with few cores, where a review page with a
    // video playing is already most of what it can do.
    var scale = navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 4 ? 0.5 : 1;

    var MAX_MS = 2000;
    var CONFETTI = ["#5fd4c4", "#ffd166", "#ef476f", "#118ab2", "#06d6a0", "#f78c6b"];
    var EMBERS = ["#ffb703", "#fb8500", "#ff5400", "#ffd60a"];

    var particles = [];
    var canvas, ctx, width, height, started, last;

    function rand(min, max) {
        return min + Math.random() * (max - min);
    }

    function confetti() {
        var count = Math.round(30 * scale);
        // Two cannons from the bottom corners, aimed up and in.
        [[0, 1], [width, -1]].forEach(function (cannon) {
            for (var i = 0; i < count; i++) {
                var angle = rand(55, 80) * Math.PI / 180;
                var speed = rand(11, 18);
                particles.push({
                    confetti: true,
                    x: cannon[0],
                    y: height,
                    vx: Math.cos(angle) * speed * cannon[1],
                    vy: -Math.sin(angle) * speed,
                    size: rand(5, 9),
                    spin: rand(0, Math.PI * 2),
                    vspin: rand(-0.2, 0.2),
                    color: CONFETTI[i % CONFETTI.length],
                    life: 0,
                    ttl: rand(80, 120),
                });
            }
        });
    }

    function embers(big) {
        var badge = document.querySelector("[data-review-streak]");
        var x = width / 2;
        var y = 40;
        if (badge) {
            var box = badge.getBoundingClientRect();
            x = box.left + box.width / 2;
            y = box.top + box.height / 2;
        }
        var count = Math.round((big ? 30 : 18) * scale);
        for (var i = 0; i < count; i++) {
            var angle = rand(0, Math.PI * 2);
            var speed = rand(1, big ? 5 : 4);
            particles.push({
                confetti: false,
                x: x,
                y: y,
                vx: Math.cos(angle) * speed,
                vy: Math.sin(angle) * speed - rand(0.5, 2),
                size: rand(2, big ? 5 : 4),
                color: EMBERS[i % EMBERS.length],
                life: 0,
                ttl: rand(35, big ? 70 : 55),
            });
        }
    }

    function finish() {
        if (canvas) canvas.remove();
        canvas = ctx = null;
        particles = [];
    }

    function step(now) {
        if (!canvas) return;
        if (!started) started = now;
        if (now - started > MAX_MS) return finish();
        // Frames counted in 60ths of a second, so a 120Hz screen doesn't
        // play everything at double speed.
        var dt = last ? Math.min((now - last) / (1000 / 60), 3) : 1;
        last = now;

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.globalAlpha = 1;
        ctx.clearRect(0, 0, width, height);

        var alive = [];
        for (var i = 0; i < particles.length; i++) {
            var p = particles[i];
            p.life += dt;
            if (p.life >= p.ttl) continue;
            if (p.confetti) {
                p.vy += 0.35 * dt;
                p.vx *= 0.985;
                p.x += p.vx * dt;
                p.y += p.vy * dt;
                p.spin += p.vspin * dt;
            } else {
                // Embers rise: heat beats gravity.
                p.vy -= 0.05 * dt;
                p.vx *= 0.96;
                p.x += p.vx * dt;
                p.y += p.vy * dt;
            }
            if (p.y > height + 20 || p.y < -20) continue;
            alive.push(p);

            ctx.globalAlpha = Math.min(1, (p.ttl - p.life) / 20);
            ctx.fillStyle = p.color;
            if (p.confetti) {
                // Squashed by its spin, so each piece flutters, without a
                // save/restore per piece.
                var c = Math.cos(p.spin), s = Math.sin(p.spin);
                ctx.setTransform(c, s, -s * Math.cos(p.spin * 2), c * Math.cos(p.spin * 2), p.x, p.y);
                ctx.fillRect(-p.size / 2, -p.size / 4, p.size, p.size / 2);
            } else {
                ctx.setTransform(1, 0, 0, 1, 0, 0);
                ctx.fillRect(p.x, p.y, p.size, p.size);
            }
        }
        particles = alive;

        if (particles.length) {
            window.requestAnimationFrame(step);
        } else {
            finish();
        }
    }

    // `slot` stacks a second callout under the first, for the decision that
    // both meets the day's goal and hits a milestone.
    function callout(text, slot) {
        var note = document.createElement("div");
        note.className = "celebrate-callout";
        note.setAttribute("role", "status");
        note.textContent = text;
        if (slot) note.style.top = (64 + slot * 54) + "px";
        document.body.appendChild(note);
        window.setTimeout(function () {
            note.classList.add("celebrate-callout--out");
            window.setTimeout(function () { note.remove(); }, 400);
        }, 2200);
    }

    function play() {
        var notes = [];
        if (data.dailyStreak) {
            notes.push("✅ Today's reviews done! " + data.dailyStreak + "-day streak");
        }
        if (data.milestone) notes.push("🔥 " + data.streak + " in a row!");
        notes.forEach(callout);

        var badge = document.querySelector("[data-review-streak]");
        if (badge && data.onFire) badge.classList.add("rv-streak--flare");

        // A tab opened in the background has nobody to show it to.
        if (still || document.hidden) return;

        width = window.innerWidth;
        height = window.innerHeight;
        if (data.approved || data.dailyStreak) confetti();
        if (data.onFire) embers(data.milestone);
        if (!particles.length) return;

        // At CSS pixels, not the screen's: confetti doesn't need to be sharp,
        // and a retina-sized canvas is four times the pixels to clear and fill.
        canvas = document.createElement("canvas");
        canvas.className = "celebrate-canvas";
        canvas.setAttribute("aria-hidden", "true");
        canvas.width = width;
        canvas.height = height;
        ctx = canvas.getContext("2d");
        document.body.appendChild(canvas);
        window.requestAnimationFrame(step);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", play);
    } else {
        play();
    }
})();
