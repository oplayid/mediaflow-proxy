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
  var media = session.media || {};

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
  var subtitleLanguage = document.getElementById("subtitle-language");
  var subtitleUploadButton = document.getElementById("subtitle-upload-button");
  var subtitleFile = document.getElementById("subtitle-file");
  var subtitleStatus = document.getElementById("subtitle-status");

  var hls = null;
  var dashPlayer = null;
  var activeIndex = -1;
  var readySent = false;
  var lastTimeUpdate = 0;
  var trackElements = {};
  var generation = 0;
  var allowedParents = ["https://movie.oplay.id", window.location.origin];
  var customKey = "mfp:custom-subtitles:" + [
    media.type || "", media.id || "", media.season || "", media.episode || ""
  ].join(":");

  function setSubtitleStatus(text) {
    if (subtitleStatus) subtitleStatus.textContent = text || "";
  }

  // A cross-origin iframe may deny localStorage; uploads still work for the
  // current page session because the track is kept in `subtitles` as well.
  function readCustomTracks() {
    try {
      var raw = window.localStorage.getItem(customKey);
      var parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter(function (item) {
        return item && item.id && item.url && item.origin === "custom";
      }) : [];
    } catch (error) {
      return [];
    }
  }

  function saveCustomTracks() {
    var custom = subtitles.filter(function (item) { return item.origin === "custom"; });
    try { window.localStorage.setItem(customKey, JSON.stringify(custom)); } catch (error) {}
  }

  function addCustomTracks(tracks, persist) {
    var known = {};
    subtitles.forEach(function (item) { known[item.id] = true; });
    (tracks || []).forEach(function (item) {
      if (!item || !item.id || known[item.id]) return;
      subtitles.push(item);
      known[item.id] = true;
    });
    if (persist) saveCustomTracks();
  }

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

  function subtitleGroup(item) {
    if (item.origin === "subdl") return "SubDL";
    if (item.origin === "custom") return "Custom";
    return "Host"; // host + API tracks are both shipped by the winning embed host
  }

  function subtitleLabel(item) {
    var label = item.label || item.language || "Subtitle";
    if (item.origin === "custom") {
      return /^custom\b/i.test(label) ? label : "Custom • " + label;
    }
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
    var groups = {};
    subtitles.forEach(function (item) {
      var name = subtitleGroup(item);
      if (!groups[name]) {
        var group = document.createElement("optgroup");
        group.label = name;
        subtitleSelect.appendChild(group);
        groups[name] = group;
      }
      var option = document.createElement("option");
      option.value = item.id;
      option.textContent = subtitleLabel(item);
      groups[name].appendChild(option);
    });
    subtitleSelect.value = preferredSubtitle();
    applySubtitle();
  }

  function rebuildSubtitles() {
    buildTracks();
    fillSubtitles();
  }

  function applySubtitle() {
    var selected = subtitleSelect.value;
    Object.keys(trackElements).forEach(function (id) {
      try { trackElements[id].track.mode = id === selected ? "showing" : "disabled"; } catch (error) {}
    });
  }

  // Keep the opaque /_token_<...> prefix: EncryptionMiddleware restores the
  // media identity from it, so type/id are only needed when api_password is off.
  function uploadEndpoint(language, label) {
    var path = window.location.pathname || "/player/";
    var prefix = "";
    if (path.indexOf("/_token_") === 0) {
      var slash = path.indexOf("/", "/_token_".length);
      if (slash !== -1) {
        prefix = path.slice(0, slash);
        path = path.slice(slash);
      }
    }
    var endpoint = path.replace(/\/player\/.*$/, "/player/subtitle/upload");
    if (endpoint === path) endpoint = "/player/subtitle/upload";
    var query = new URLSearchParams();
    query.set("type", media.type || "");
    query.set("id", media.id || "");
    query.set("language", language || "id");
    query.set("label", label || "Subtitle");
    // The session carries a 6h upload token so uploads keep working after the
    // 10-minute iframe token expires. The fallback covers older sessions.
    var base = session.upload_url || (prefix + endpoint);
    return base + (base.indexOf("?") === -1 ? "?" : "&") + query.toString();
  }

  function selectSubtitleById(id) {
    var match = subtitles.find(function (item) { return item.id === id; });
    if (!match) return false;
    if (!trackElements[id]) rebuildSubtitles();
    subtitleSelect.value = id;
    applySubtitle();
    return true;
  }

  async function uploadSubtitle(file) {
    var language = subtitleLanguage ? subtitleLanguage.value : "id";
    var label = (file.name || "Subtitle").replace(/\.(srt|vtt)$/i, "");
    setSubtitleStatus("Mengunggah subtitle…");
    try {
      var response = await fetch(uploadEndpoint(language, label), {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
        cache: "no-store"
      });
      if (!response.ok) {
        var detail = "HTTP " + response.status;
        try {
          var body = await response.json();
          detail = (body && (body.detail || body.error)) || detail;
        } catch (error) {}
        throw new Error(detail);
      }
      var track = await response.json();
      addCustomTracks([track], true);
      rebuildSubtitles();
      selectSubtitleById(track.id);
      setSubtitleStatus("Subtitle " + label + " aktif");
    } catch (error) {
      setSubtitleStatus("Gagal: " + (error && error.message ? error.message : "upload subtitle"));
    } finally {
      if (subtitleFile) subtitleFile.value = "";
    }
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

  if (subtitleUploadButton) {
    subtitleUploadButton.addEventListener("click", function () {
      if (subtitleFile) subtitleFile.click();
    });
  }
  if (subtitleFile) {
    subtitleFile.addEventListener("change", function () {
      var file = subtitleFile.files && subtitleFile.files[0];
      if (file) uploadSubtitle(file);
    });
  }

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
      var match = data.id
        ? subtitles.find(function (item) { return item.id === data.id; })
        : subtitles.find(function (item) { return (item.language || "") === data.language; });
      if (match) selectSubtitleById(match.id);
    } else if (data.type === "mfp:pause") {
      video.pause();
    } else if (data.type === "mfp:play") {
      video.play().catch(function () {});
    }
  });

  title.textContent = media.type === "tv"
    ? "TV " + media.id + " • S" + (media.season || 1) + " E" + (media.episode || 1)
    : (media.id ? media.type + " • " + media.id : "MediaFlow Player");

  if (!candidates.length) {
    showError("Sumber video tidak tersedia dari host yang diperiksa.");
    return;
  }

  addCustomTracks(readCustomTracks(), false);
  rebuildSubtitles();
  setOverlay("Sumber ditemukan. Menyiapkan video…", 100);
  playCandidate(0);
})();
