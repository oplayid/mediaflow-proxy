(function () {
  "use strict";

  // The session lives in a non-executable <script type="application/json">
  // data block. The old inline assignment is blocked by `script-src 'self'`.
  function readSession() {
    var element = document.getElementById("mfp-session");
    var legacy = window.__MFP_SESSION__ || { candidates: [], subtitles: [], audit: {} };
    if (!element) return legacy;
    try {
      return JSON.parse(element.textContent || "null") || legacy;
    } catch (error) {
      return legacy;
    }
  }

  var session = readSession();
  var candidates = Array.isArray(session.candidates) ? session.candidates : [];
  var subtitles = Array.isArray(session.subtitles) ? session.subtitles : [];

  var video = document.getElementById("video");
  var overlay = document.getElementById("overlay");
  var overlayStatus = document.getElementById("overlay-status");
  var overlayBar = document.getElementById("overlay-bar");
  var errorBox = document.getElementById("error-box");
  var errorStatus = document.getElementById("error-status");
  var hostBadge = document.getElementById("host-badge");
  var simBadge = document.getElementById("sim-badge");
  var title = document.getElementById("title");
  var hostSelect = document.getElementById("host-select");
  var qualitySelect = document.getElementById("quality-select");
  var subtitleSelect = document.getElementById("subtitle-select");
  var retryButton = document.getElementById("retry-button");
  var errorRetry = document.getElementById("error-retry");

  var hls = null;
  var dashPlayer = null;
  var activeIndex = -1;
  var failed = {};
  var readySent = false;
  var lastTimeUpdate = 0;
  var trackElements = {};
  var generation = 0;
  var allowedParents = ["https://movie.oplay.id", window.location.origin];

  function parentWindow() {
    return window.parent && window.parent !== window ? window.parent : null;
  }

  function post(type, payload) {
    var target = parentWindow();
    if (!target) return;
    var message = { source: "mfp-player", type: type };
    Object.keys(payload || {}).forEach(function (key) { message[key] = payload[key]; });
    target.postMessage(message, "*");
  }

  function setOverlay(text, percent) {
    overlayStatus.textContent = text;
    overlayBar.style.width = (percent || 0) + "%";
  }

  function hideOverlay() {
    overlay.classList.add("hidden");
    errorBox.classList.add("hidden");
  }

  function showError(text) {
    overlay.classList.add("hidden");
    errorStatus.textContent = text || "Tidak ada host yang bisa diputar.";
    errorBox.classList.remove("hidden");
    post("error", { message: errorStatus.textContent });
  }

  function currentCandidate() {
    return candidates[activeIndex] || null;
  }

  function buildTracks() {
    Array.prototype.slice.call(video.querySelectorAll("track")).forEach(function (element) { element.remove(); });
    trackElements = {};
    subtitles.forEach(function (item) {
      var element = document.createElement("track");
      element.kind = "subtitles";
      element.label = (item.origin === "provider" ? "API • " : "") + (item.host ? item.host + " • " : "") + item.label;
      element.srclang = item.language || "";
      element.src = item.url;
      video.appendChild(element);
      trackElements[item.id] = element;
    });
  }

  function preferredSubtitle() {
    var id = subtitles.find(function (item) { return (item.language || "").toLowerCase().startsWith("id"); });
    if (id) return id.id;
    var en = subtitles.find(function (item) { return (item.language || "").toLowerCase().startsWith("en"); });
    if (en) return en.id;
    return subtitles.length ? subtitles[0].id : "";
  }

  function fillSubtitles() {
    subtitleSelect.innerHTML = "";
    var off = document.createElement("option");
    off.value = "";
    off.textContent = "Mati";
    subtitleSelect.appendChild(off);
    subtitles.forEach(function (item) {
      var option = document.createElement("option");
      option.value = item.id;
      option.textContent = (item.origin === "provider" ? "API • " : "") + (item.host ? item.host + " • " : "") + item.label;
      subtitleSelect.appendChild(option);
    });
    subtitleSelect.value = preferredSubtitle();
    applySubtitle();
  }

  function applySubtitle() {
    var selected = subtitleSelect.value;
    Object.keys(trackElements).forEach(function (id) {
      try { trackElements[id].track.mode = id === selected ? "showing" : "disabled"; } catch (error) {}
    });
  }

  function fillHosts() {
    hostSelect.innerHTML = "";
    candidates.forEach(function (candidate, index) {
      var option = document.createElement("option");
      option.value = String(index);
      option.textContent = candidate.host + " • " + (candidate.needs_transcode ? "HEVC → H.264" : candidate.manifest_type) + " • " + candidate.score;
      hostSelect.appendChild(option);
    });
    if (candidates.length) hostSelect.value = "0";
  }

  function fillQualities() {
    qualitySelect.innerHTML = "";
    var auto = document.createElement("option");
    auto.value = "";
    auto.textContent = "Auto";
    qualitySelect.appendChild(auto);
    var candidate = currentCandidate();
    if (candidate && candidate.needs_transcode) {
      qualitySelect.disabled = true;
      return;
    }
    qualitySelect.disabled = false;
    (candidate && candidate.sources ? candidate.sources : []).forEach(function (source) {
      var option = document.createElement("option");
      option.value = source.url;
      option.textContent = source.label || source.id;
      qualitySelect.appendChild(option);
    });
  }

  function cleanup() {
    generation += 1;
    if (hls) { try { hls.destroy(); } catch (error) {} hls = null; }
    if (dashPlayer) { try { dashPlayer.reset(); } catch (error) {} dashPlayer = null; }
  }

  function waitForTranscode(url, token) {
    return new Promise(function (resolve, reject) {
      var started = Date.now();
      // Do not keep the viewer on a spinner for a minute when FFmpeg stalls;
      // playUrl() falls back to the native source once this lapses.
      var deadline = 25000;
      function check() {
        if (token !== generation) return;
        var elapsed = Date.now() - started;
        var percent = Math.min(95, Math.round((elapsed / deadline) * 100));
        setOverlay("Menyiapkan transcode HEVC → H.264… " + percent + "%", percent);
        fetch(url, { cache: "no-store" })
          .then(function (response) {
            if (token !== generation) return;
            if (response.ok) { resolve(); return; }
            if (response.status === 503 && elapsed < deadline) {
              setTimeout(check, 1000);
              return;
            }
            reject(new Error("transcode HTTP " + response.status));
          })
          .catch(function (error) {
            if (token !== generation) return;
            if (elapsed < deadline) { setTimeout(check, 1500); return; }
            reject(error);
          });
      }
      check();
    });
  }

  function handleError(source, fatal) {
    if (!fatal) return;
    var candidate = currentCandidate();
    if (!candidate) return;
    failed[activeIndex] = true;
    var next = candidates.findIndex(function (_item, index) { return !failed[index]; });
    if (next >= 0) {
      var detail = source === "transcode" ? " transcode gagal" : "";
      setOverlay("Host " + candidate.host + detail + ". Mencoba " + candidates[next].host + "…", 70);
      overlay.classList.remove("hidden");
      playCandidate(next, video.currentTime);
      return;
    }
    // Standby host selection stays inside this iframe. Reloading /retry with
    // the failed host excluded makes the server continue down PLAYER_HOSTS;
    // StreamFlix only ever sees the ID and the opaque player URL.
    if ((session.audit || {}).retry_available) {
      setOverlay("Host " + candidate.host + " gagal. Mencoba host berikutnya…", 70);
      retryExcluding(candidate.host);
      return;
    }
    showError(source === "transcode"
      ? "Transcode HEVC gagal. Silakan coba ulang."
      : "Semua host gagal diputar. Silakan cari ulang host.");
  }

  function sendReady() {
    if (readySent) return;
    readySent = true;
    hideOverlay();
    var candidate = currentCandidate();
    post("ready", {
      duration: Number.isFinite(video.duration) ? video.duration : 0,
      host: candidate ? candidate.host : null,
      transcode: !!(candidate && candidate.needs_transcode),
      audit: session.audit || {}
    });
  }

  function attachCommon(startTime) {
    applySubtitle();
    if (Number.isFinite(startTime) && startTime > 0) {
      video.addEventListener("loadedmetadata", function once() {
        try { video.currentTime = startTime; } catch (error) {}
        video.removeEventListener("loadedmetadata", once);
      });
    }
  }

  function startPlayback(candidate, url, startTime, playbackType) {
    setOverlay("Memutar " + candidate.host + "…", 90);
    attachCommon(startTime);

    if (playbackType === "hls") {
      if (window.Hls && window.Hls.isSupported()) {
        hls = new window.Hls({ enableWorker: true, capLevelToPlayerSize: true });
        hls.loadSource(url);
        hls.attachMedia(video);
        hls.on(window.Hls.Events.MANIFEST_PARSED, sendReady);
        hls.on(window.Hls.Events.ERROR, function (event, data) { handleError("hls", data && data.fatal); });
      } else {
        video.src = url;
        video.addEventListener("loadedmetadata", sendReady, { once: true });
      }
    } else if (playbackType === "dash" && window.dashjs) {
      dashPlayer = window.dashjs.MediaPlayer().create();
      dashPlayer.initialize(video, url, false);
      dashPlayer.on(window.dashjs.MediaPlayer.events.ERROR, function (event) {
        handleError("dash", !!(event && event.error));
      });
      video.addEventListener("loadedmetadata", sendReady, { once: true });
    } else {
      video.src = url;
      video.addEventListener("loadedmetadata", sendReady, { once: true });
    }

    video.play().catch(function () {});
    post("sourcechange", { host: candidate.host, type: playbackType, transcode: !!candidate.needs_transcode });
  }

  function playUrl(candidate, url, startTime) {
    cleanup();
    var token = generation;
    if (candidate.needs_transcode) {
      startPlaybackAfterTranscode(candidate, startTime, token);
      return;
    }
    startPlayback(candidate, url, startTime, candidate.playback_type || candidate.manifest_type);
  }

  function startPlaybackAfterTranscode(candidate, startTime, token) {
    setOverlay("Menyiapkan transcode HEVC → H.264…", 5);
    waitForTranscode(candidate.transcode_url, token).then(function () {
      if (token !== generation) return;
      startPlayback(candidate, candidate.transcode_url, startTime, "hls");
    }).catch(function () {
      if (token !== generation) return;
      // Last chance before discarding this host: play the native (already
      // proxied) source. Errors from here go through the normal host fallback.
      startPlayback(candidate, candidate.url, startTime, candidate.playback_type || candidate.manifest_type);
    });
  }

  function playCandidate(index, startTime) {
    var candidate = candidates[index];
    if (!candidate) { showError("Host tidak tersedia."); return; }
    activeIndex = index;
    hostSelect.value = String(index);
    fillQualities();
    hostBadge.textContent = candidate.host + " • " + (candidate.needs_transcode ? "HEVC → H.264" : candidate.manifest_type);
    hostBadge.classList.add("good");
    hostBadge.classList.toggle("transcode", !!candidate.needs_transcode);
    playUrl(candidate, candidate.url, startTime || 0);
  }

  function retry() {
    var path = window.location.pathname.replace(/\/?retry\/?$/, "");
    window.location.href = path.replace(/\/$/, "") + "/retry";
  }

  // Keep the same encrypted token (it lives in the path prefix) and only add
  // the explicit exclude/position query params. The middleware decrypts the
  // token again on the /retry route and forwards these to simulate_session().
  function retryExcluding(host) {
    var audit = session.audit || {};
    var excluded = Array.isArray(audit.excluded_hosts) ? audit.excluded_hosts.slice() : [];
    if (host && excluded.indexOf(host) === -1) excluded.push(host);
    var path = window.location.pathname.replace(/\/?retry\/?$/, "").replace(/\/$/, "");
    var query = new URLSearchParams(window.location.search);
    query.set("exclude", excluded.join(","));
    if (video.currentTime > 0) query.set("t", String(Math.floor(video.currentTime)));
    window.location.href = path + "/retry?" + query.toString();
  }

  retryButton.addEventListener("click", retry);
  errorRetry.addEventListener("click", retry);

  hostSelect.addEventListener("change", function () {
    playCandidate(Number(hostSelect.value), video.currentTime);
  });

  qualitySelect.addEventListener("change", function () {
    if (!qualitySelect.value) { playCandidate(activeIndex, video.currentTime); return; }
    var candidate = currentCandidate();
    if (candidate) playUrl(candidate, qualitySelect.value, video.currentTime);
  });

  subtitleSelect.addEventListener("change", applySubtitle);

  video.addEventListener("timeupdate", function () {
    var now = Date.now();
    if (now - lastTimeUpdate < 1000) return;
    lastTimeUpdate = now;
    post("timeupdate", {
      currentTime: video.currentTime || 0,
      duration: Number.isFinite(video.duration) ? video.duration : 0
    });
  });

  video.addEventListener("ended", function () { post("ended", {}); });
  video.addEventListener("play", function () { post("playing", {}); });
  video.addEventListener("pause", function () { post("paused", {}); });

  window.addEventListener("message", function (event) {
    if (allowedParents.indexOf(event.origin) === -1) return;
    var data = event.data || {};
    if (typeof data !== "object") return;
    if (data.type === "mfp:seek" && Number.isFinite(data.time)) {
      try { video.currentTime = data.time; } catch (error) {}
    } else if (data.type === "mfp:setSubtitle") {
      var match = subtitles.find(function (item) { return (item.language || "") === data.language; });
      if (match) { subtitleSelect.value = match.id; applySubtitle(); }
    } else if (data.type === "mfp:pause") {
      video.pause();
    } else if (data.type === "mfp:play") {
      video.play().catch(function () {});
    }
    // No external host-switch command: Streamflix supplies only the media ID
    // and renders the opaque player URL. Failover is owned here via /retry.
  });

  var media = session.media || {};
  title.textContent = media.type === "tv"
    ? "TV " + media.id + " • S" + (media.season || 1) + " E" + (media.episode || 1)
    : (media.id ? media.type + " • " + media.id : "MediaFlow Player");
  simBadge.textContent = "simulasi " + ((session.audit || {}).simulation_ms || 0) + "ms";

  if (!candidates.length) {
    showError("Tidak ada host embed yang lolos simulasi. Tambahkan atau perbaiki host di MediaFlow.");
    return;
  }

  buildTracks();
  fillSubtitles();
  fillHosts();
  setOverlay("Simulasi selesai. Menyiapkan " + candidates[0].host + "…", 100);
  playCandidate(0, Number(session.start_time) || 0);
})();
