/* Hero graphic: one human sets a mission (orange, left), a swarm of agents carries it
   (dashes), challenged work gets pushed back (rings), the result lands on the right. */
(function () {
  "use strict";
  var canvas = document.getElementById("swarm");
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext("2d");
  var css = getComputedStyle(document.documentElement);
  var INK = css.getPropertyValue("--ink").trim() || "#0B0B0B";
  var HUMAN = css.getPropertyValue("--human").trim() || "#FF5A1F";
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var w = 0, h = 0, parts = [], raf = 0, last = 0;
  var A, B, C;

  function rand(a, b) { return a + Math.random() * (b - a); }

  function point(t) {
    var u = 1 - t;
    return {
      x: u * u * A.x + 2 * u * t * C.x + t * t * B.x,
      y: u * u * A.y + 2 * u * t * C.y + t * t * B.y
    };
  }
  function tangent(t) {
    var dx = 2 * (1 - t) * (C.x - A.x) + 2 * t * (B.x - C.x);
    var dy = 2 * (1 - t) * (C.y - A.y) + 2 * t * (B.y - C.y);
    var len = Math.sqrt(dx * dx + dy * dy) || 1;
    return { x: dx / len, y: dy / len };
  }

  function make(t) {
    return {
      t: t, speed: rand(0.035, 0.075), off: rand(-1, 1), wob: rand(0, Math.PI * 2),
      len: rand(5, 10), shade: rand(0.35, 0.95), challenged: 0
    };
  }

  function resize() {
    var r = canvas.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = Math.max(1, r.width); h = Math.max(1, r.height);
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    A = { x: Math.max(18, w * 0.05), y: h * 0.78 };
    B = { x: w - Math.max(24, w * 0.05), y: h * 0.2 };
    C = { x: w * 0.55, y: h * 0.62 };
    var count = Math.max(90, Math.min(420, Math.round(w / 3)));
    parts = [];
    for (var i = 0; i < count; i++) parts.push(make(Math.random()));
  }

  function draw() {
    ctx.clearRect(0, 0, w, h);
    var spread = Math.min(h * 0.42, 130);

    ctx.setLineDash([2, 5]);
    ctx.strokeStyle = HUMAN;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (var s = 0; s <= 60; s++) {
      var q = point(s / 60);
      if (s === 0) ctx.moveTo(q.x, q.y); else ctx.lineTo(q.x, q.y);
    }
    ctx.stroke();
    ctx.setLineDash([]);

    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      var base = point(p.t), tg = tangent(p.t);
      var bulge = Math.pow(Math.sin(Math.PI * p.t), 0.9) * spread;
      var o = p.off * bulge + Math.sin(p.wob) * 4;
      var x = base.x - tg.y * o, y = base.y + tg.x * o;
      if (p.challenged > 0) {
        ctx.globalAlpha = Math.min(1, p.challenged);
        ctx.strokeStyle = INK;
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2); ctx.stroke();
      } else {
        ctx.globalAlpha = p.shade * (0.25 + 0.75 * Math.min(1, p.t * 6));
        ctx.strokeStyle = INK;
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.moveTo(x - tg.x * p.len / 2, y - tg.y * p.len / 2);
        ctx.lineTo(x + tg.x * p.len / 2, y + tg.y * p.len / 2);
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    ctx.fillStyle = HUMAN;
    ctx.beginPath(); ctx.arc(A.x, A.y, 5, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = HUMAN; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(A.x, A.y, 10, 0, Math.PI * 2); ctx.stroke();

    ctx.fillStyle = INK;
    ctx.beginPath(); ctx.arc(B.x, B.y, 13, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "#F3F3F0"; ctx.lineWidth = 2.2;
    ctx.beginPath(); ctx.moveTo(B.x - 5.5, B.y); ctx.lineTo(B.x - 1.5, B.y + 4); ctx.lineTo(B.x + 5.5, B.y - 4); ctx.stroke();
  }

  function step(t) {
    var dt = last ? Math.min((t - last) / 1000, 0.05) : 0.016;
    last = t;
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      p.wob += dt * 1.6;
      if (p.challenged > 0) {
        p.challenged -= dt * 0.8;
        p.t = Math.max(0.02, p.t - dt * 0.12);
        continue;
      }
      p.t += p.speed * dt;
      if (p.t > 0.45 && p.t < 0.9 && Math.random() < dt * 0.012) p.challenged = 1.4;
      if (p.t >= 1) parts[i] = make(0);
    }
    draw();
    raf = requestAnimationFrame(step);
  }

  resize();
  if (reduce) draw(); else raf = requestAnimationFrame(step);

  var timer;
  window.addEventListener("resize", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { resize(); if (reduce) draw(); }, 150);
  });
  document.addEventListener("visibilitychange", function () {
    if (reduce) return;
    if (document.hidden) cancelAnimationFrame(raf);
    else { last = 0; raf = requestAnimationFrame(step); }
  });
})();
