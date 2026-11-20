/* The effect played on the page after a review decision.
 *
 * templatetags/review_streak.py writes the #review-celebration payload only on
 * the one page a decision redirected to:
 *   approved   confetti, for approvals only
 *   onFire     embers off the streak badge, for a run of FIRE_AT or more
 *   milestone  a callout naming the streak (5, 10, 25, ...)
 *
 * One canvas, laid over the page and removed once the last particle has
 * gone, so nothing here can get in the way of the review underneath. Anyone
 * who has asked for reduced motion gets the milestone callout and nothing
 * that moves.
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

    var CONFETTI = ["#5fd4c4", "#ffd166", "#ef476f", "#118ab2", "#06d6a0", "#f78c6b", "#c77dff"];
    var EMBERS = ["#ffb703", "#fb8500", "#ff5400", "#ffd60a", "#e63946"];

    var particles = [];
    var canvas = null;
    var ctx = null;
    var last = 0;

    function rand(min, max) {
        return min + Math.random() * (max - min);
    }

    function ensureCanvas() {
        if (canvas) return;
        canvas = document.createElement("canvas");
        canvas.className = "celebrate-canvas";
        canvas.setAttribute("aria-hidden", "true");
        document.body.appendChild(canvas);
        ctx = canvas.getContext("2d");
        resize();
        window.addEventListener("resize", resize);
    }

    function resize() {
        var ratio = window.devicePixelRatio || 1;
        canvas.width = window.innerWidth * ratio;
        canvas.height = window.innerHeight * ratio;
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }

    function confetti() {
        var w = window.innerWidth;
        // Two cannons from the bottom corners, aimed up and in.
        [[0, 1], [w, -1]].forEach(function (cannon) {
            for (var i = 0; i < 90; i++) {
                var angle = rand(55, 80) * Math.PI / 180;
                var speed = rand(11, 19);
                particles.push({
                    kind: "confetti",
                    x: cannon[0],
                    y: window.innerHeight,
                    vx: Math.cos(angle) * speed * cannon[1],
                    vy: -Math.sin(angle) * speed,
                    w: rand(6, 11),
                    h: rand(3, 6),
                    spin: rand(0, Math.PI * 2),
                    vspin: rand(-0.25, 0.25),
                    color: CONFETTI[i % CONFETTI.length],
                    life: 0,
                    ttl: rand(150, 220),
                });
            }
        });
    }

    function embers(big) {
        var badge = document.querySelector("[data-review-streak]");
        var x, y;
        if (badge) {
            var box = badge.getBoundingClientRect();
            x = box.left + box.width / 2;
            y = box.top + box.height / 2;
        } else {
            x = window.innerWidth / 2;
            y = 40;
        }
        var count = big ? 90 : 45;
        for (var i = 0; i < count; i++) {
            var angle = rand(0, Math.PI * 2);
            var speed = rand(1, big ? 7 : 5);
            particles.push({
                kind: "ember",
                x: x,
                y: y,
                vx: Math.cos(angle) * speed,
                vy: Math.sin(angle) * speed - rand(0.5, 2),
                r: rand(1.5, big ? 4.5 : 3.5),
                color: EMBERS[i % EMBERS.length],
                life: 0,
                ttl: rand(45, big ? 110 : 80),
            });
        }
    }

    function step(now) {
        // Frames are counted in 60ths of a second, so a 120Hz screen doesn't
        // play everything at double speed.
        var dt = last ? Math.min((now - last) / (1000 / 60), 3) : 1;
        last = now;
        ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);

        particles = particles.filter(function (p) {
            p.life += dt;
            if (p.life >= p.ttl) return false;
            var fade = Math.min(1, (p.ttl - p.life) / 30);
            if (p.kind === "confetti") {
                p.vy += 0.32 * dt;
                p.vx *= Math.pow(0.985, dt);
                p.vy *= Math.pow(0.985, dt);
                p.x += p.vx * dt;
                p.y += p.vy * dt;
                p.spin += p.vspin * dt;
                ctx.save();
                ctx.globalAlpha = fade;
                ctx.translate(p.x, p.y);
                ctx.rotate(p.spin);
                // Flattened by its own spin, so each piece flutters.
                ctx.scale(1, Math.cos(p.spin * 2));
                ctx.fillStyle = p.color;
                ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
                ctx.restore();
            } else {
                // Embers rise: heat beats gravity.
                p.vy -= 0.06 * dt;
                p.vx *= Math.pow(0.96, dt);
                p.x += p.vx * dt;
                p.y += p.vy * dt;
                ctx.save();
                ctx.globalAlpha = fade;
                ctx.globalCompositeOperation = "lighter";
                ctx.shadowBlur = 12;
                ctx.shadowColor = p.color;
                ctx.fillStyle = p.color;
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.r * (1 - p.life / p.ttl * 0.6), 0, Math.PI * 2);
                ctx.fill();
                ctx.restore();
            }
            return p.y < window.innerHeight + 40;
        });

        if (particles.length) {
            window.requestAnimationFrame(step);
        } else {
            window.removeEventListener("resize", resize);
            canvas.remove();
            canvas = null;
        }
    }

    function callout(streak) {
        var note = document.createElement("div");
        note.className = "celebrate-callout";
        note.setAttribute("role", "status");
        note.textContent = "🔥 " + streak + " in a row!";
        document.body.appendChild(note);
        window.setTimeout(function () {
            note.classList.add("celebrate-callout--out");
            window.setTimeout(function () { note.remove(); }, 400);
        }, 2200);
    }

    function play() {
        if (data.milestone) callout(data.streak);

        var badge = document.querySelector("[data-review-streak]");
        if (badge && data.onFire) badge.classList.add("rv-streak--flare");

        if (still) return;
        if (data.approved) confetti();
        if (data.onFire) embers(data.milestone);
        if (!particles.length) return;
        ensureCanvas();
        window.requestAnimationFrame(step);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", play);
    } else {
        play();
    }
})();
