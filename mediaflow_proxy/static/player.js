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
  var sourceBadge = document.getElementById("source-badge");
  var title = document.getElementById("title");
  var qualitySelect = document.getElementById("quality-select");
  var subtitleSelect = document.getElementById("subtitle-select");

  var hls = null;
  var dashPlayer = null;
  var activeIndex = -1;
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
    errorStatus.textContent = text || "Video tidak dapat diputar.";
    errorBox.classList.remove("hidden");
    post("error", { message: errorStatus.textContent });
  }

  function currentCandidate() {
    return candidates[activeIndex] || null;
  }

  function isNative(item) {
    return item.origin === "host" || item.origin === "provider";
  }

  function byLanguage(items, prefix) {
    return items.find(function (item) {
      return (item.language || "").toLowerCase().startsWith(prefix);
    });
  }

  function subtitleLabel(item) {
    var label = item.label || item.language || "Subtitle";
    if (item.origin === "subdl") {
      return /^subdl\b/i.test(label) ? label : "SubDL • " + label;
    }
    return item.origin === "provider" && !/^api\b/i.test(label) ? "API • " + label : label;
  }

  function buildTracks() {
    Array.prototype.slice.call(video.querySelectorAll("track")).forEach(function (element) { element.remove(); });
    trackElements = {};
    subtitles.forEach(function (item) {
      var element = document.createElement("track");
      element.kind = "subtitles";
      element.label = subtitleLabel(item);
      element.srclang = item.language || "";
      element.src = item.url;
      video.appendChild(element);
      trackElements[item.id] = element;
    });
  }

  function preferredSubtitle() {
    // Default to the native subtitle package carried by the winning host.
    var native = subtitles.filter(isNative);
    var chosen = byLanguage(native, "id") || byLanguage(native, "en") || native[0];
    if (!chosen) chosen = byLanguage(subtitles, "id") || subtitles[0];
    return chosen ? chosen.id : "";
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
      option.textContent = subtitleLabel(item);
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

  // Decision A: there is no post-playback failover. The host was already
  // checked once and the winner is the only candidate in the session.
  function handleError(source, fatal) {
    if (!fatal) return;
    showError(source === "transcode"
      ? "Transcode HEVC gagal. Video tidak dapat diputar."
      : "Sumber video gagal diputar. Silakan coba lagi nanti.");
  }

  function sendReady() {
    if (readySent) return;
    readySent = true;
    hideOverlay();
    var candidate = currentCandidate();
    post("ready", {
      duration: Number.isFinite(video.duration) ? video.duration : 0,
      transcode: !!(candidate && candidate.needs_transcode),
      audit: session.audit || {}
    });
  }

  function attachCommon() {
    applySubtitle();
  }

  function startPlayback(candidate, url, playbackType) {
    setOverlay("Memutar video…", 90);
    attachCommon();

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
    post("sourcechange", { type: playbackType, transcode: !!candidate.needs_transcode });
  }

  function playUrl(candidate, url) {
    cleanup();
    var token = generation;
    if (candidate.needs_transcode) {
      startPlaybackAfterTranscode(candidate, token);
      return;
    }
    startPlayback(candidate, url, candidate.playback_type || candidate.manifest_type);
  }

  function startPlaybackAfterTranscode(candidate, token) {
    setOverlay("Menyiapkan transcode HEVC → H.264…", 5);
    waitForTranscode(candidate.transcode_url, token).then(function () {
      if (token !== generation) return;
      startPlayback(candidate, candidate.transcode_url, "hls");
    }).catch(function () {
      if (token !== generation) return;
      // Last chance before showing the error: play the native proxied source.
      startPlayback(candidate, candidate.url, candidate.playback_type || candidate.manifest_type);
    });
  }

  function playCandidate(index) {
    var candidate = candidates[index];
    if (!candidate) { showError("Sumber video tidak tersedia."); return; }
    activeIndex = index;
    fillQualities();
    sourceBadge.textContent = (candidate.needs_transcode ? "HEVC → H.264" : candidate.manifest_type) + " • siap";
    sourceBadge.classList.add("good");
    sourceBadge.classList.toggle("transcode", !!candidate.needs_transcode);
    playUrl(candidate, candidate.url);
  }

  qualitySelect.addEventListener("change", function () {
    var candidate = currentCandidate();
    if (!candidate) return;
    if (!qualitySelect.value) { playUrl(candidate, candidate.url); return; }
    playUrl(candidate, qualitySelect.value);
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
  });

  var media = session.media || {};
  title.textContent = media.type === "tv"
    ? "TV " + media.id + " • S" + (media.season || 1) + " E" + (media.episode || 1)
    : (media.id ? media.type + " • " + media.id : "MediaFlow Player");

  if (!candidates.length) {
    showError("Sumber video tidak tersedia dari host yang diperiksa.");
    return;
  }

  buildTracks();
  fillSubtitles();
  setOverlay("Sumber ditemukan. Menyiapkan video…", 100);
  playCandidate(0);
})();
