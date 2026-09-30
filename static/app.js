/* NanoVMS UI - vanilla JS, no build step, no dependencies. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ------------------------------------------------------------------ util */

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let j = null;
  try { j = await r.json(); } catch { /* empty body */ }
  if (!r.ok || (j && j.ok === false)) {
    throw new Error((j && j.error) || `${r.status} ${r.statusText}`);
  }
  return j;
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 8000 : 4000);
}

const bytes = n => {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return (n / 1024 ** i).toFixed(i ? 1 : 0) + ' ' + u[i];
};

const pad = n => String(n).padStart(2, '0');

function hhmmss(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const fromInput = v => Math.round(new Date(v).getTime() / 1000);

const escapeHtml = s => String(s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}h${pad(m)}m` : (m ? `${m}m${pad(s)}s` : `${s}s`);
}

/* ----------------------------------------------------- MP4 codec sniffing */

/* Return a codec string from an avcC box's actual configuration bytes.
   Header is: box header(4 size + 4 type) + 5 bytes config, so profile starts
   at byte 5. Previous code read byte 6, shifting every hex digit and causing
   Chrome to reject the SourceBuffer type. */
function avcCodecFromInit(initBuf) {
  const b = new Uint8Array(initBuf);
  const t = [0x61, 0x76, 0x63, 0x43]; // 'avcC'
  let at = -1;
  for (let i = 0; i + 4 <= b.length; i++) {
    if (b[i] === t[0] && b[i + 1] === t[1] && b[i + 2] === t[2] && b[i + 3] === t[3]) {
      at = i;
      break;
    }
  }
  if (at < 0 || at + 12 >= b.length) return '';
  const hex = n => n.toString(16).padStart(2, '0');
  return `avc1.${hex(b[at + 5])}${hex(b[at + 6])}${hex(b[at + 7])}`;
}

/* Browser MSE codec strings for the audio tracks we can carry. Anything else
   (G.711 etc.) is transcoded server-side to AAC before it reaches us. */
const AUDIO_MIME = { aac: 'mp4a.40.2', opus: 'opus' };

/* -------------------------------------------------------------- MSE player */

class MseStream {
  constructor(video, initUrl, fragUrl, { onStatus, onEnd } = {}) {
    this.video = video;
    this.initUrl = initUrl;
    this.fragUrl = fragUrl;
    this.onStatus = onStatus || (() => {});
    this.onEnd = onEnd || (() => {});
    this.cursor = 0;
    this.stopped = false;
    this.queue = [];
    this.ms = null;
    this.sb = null;
    this.buffering = false;
  }

  async start() {
    if (!window.MediaSource) throw new Error('this browser has no MSE support');
    this.onStatus('connecting');
    const ctrl = new AbortController();
    // A camera at its RTSP connection limit accepts the TCP request and then
    // never sends the SDP, so the server holds the init open until it times
    // out. Without a client-side deadline the tile just says "connecting" for
    // a minute and a half with no clue why.
    const t = setTimeout(() => ctrl.abort(), 25000);
    let r;
    try {
      r = await fetch(this.initUrl, { signal: ctrl.signal });
    } catch (e) {
      clearTimeout(t);
      if (e.name === 'AbortError') {
        throw new Error('camera sent no video within 25s - it may be at its ' +
                        'RTSP connection limit (a recorder is using the other slot)');
      }
      throw new Error('cannot reach the NanoVMS server: ' + e.message);
    }
    clearTimeout(t);
    if (!r.ok) {
      let msg = `stream failed (${r.status})`;
      try { msg = (await r.json()).error || msg; } catch { /* not json */ }
      if (r.status === 504) {
        msg = 'camera sent no video - it may be at its RTSP connection limit ' +
              '(a recorder is using the other slot)';
      }
      throw new Error(msg);
    }
    this.codec = r.headers.get('X-NanoVMS-Codec') || 'h264';
    this.audioCodec = r.headers.get('X-NanoVMS-Audio-Codec') || '';
    this.transcode = r.headers.get('X-NanoVMS-Transcode') === '1';
    const initBuf = await r.arrayBuffer();

    this.ms = new MediaSource();
    this.video.src = URL.createObjectURL(this.ms);
    await new Promise(res => {
      if (this.ms.readyState === 'open') return res();
      this.ms.addEventListener('sourceopen', res, { once: true });
    });

    const cs = avcCodecFromInit(initBuf);
    if (!cs) {
      throw new Error(`no H.264 configuration (avcC) in stream header - codec=${this.codec}`);
    }
    // a single SourceBuffer can carry both tracks, but the MIME must list them
    const parts = [cs];
    const aMime = AUDIO_MIME[this.audioCodec];
    if (aMime) {
      if (MediaSource.isTypeSupported(`audio/mp4; codecs="${aMime}"`) ||
          MediaSource.isTypeSupported(`video/mp4; codecs="${aMime}"`)) {
        parts.push(aMime);
      }
    }
    const mime = `video/mp4; codecs="${parts.join(', ')}"`;
    if (!MediaSource.isTypeSupported(mime)) {
      // audio is optional: retry video-only rather than failing the whole stream
      const vOnly = `video/mp4; codecs="${cs}"`;
      if (parts.length > 1 && MediaSource.isTypeSupported(vOnly)) {
        this.audioCodec = '';
        this.sb = this.ms.addSourceBuffer(vOnly);
        this.codecString = vOnly;
        this.sb.mode = 'segments';
        this.sb.addEventListener('updateend', () => this._pump());
        this.sb.addEventListener('error', () => this.onStatus('sourcebuffer error'));
        this.sb.appendBuffer(initBuf);
        this.video.muted = false;
        this.video.play().catch(() => {});
        this.onStatus(this.transcode ? 'live (transcoding, no audio)' : 'live (stream copy, no audio)');
        this._loop();
        return;
      }
      throw new Error(`codec not supported by this browser (${mime})`);
    }
    this.codecString = mime;
    this.sb = this.ms.addSourceBuffer(mime);
    this.sb.mode = 'segments';
    this.sb.addEventListener('updateend', () => this._pump());
    this.sb.addEventListener('error', () => this.onStatus('sourcebuffer error'));
    this.sb.appendBuffer(initBuf);

    this.video.muted = false;   // MSE audio starts muted in some browsers
    this.video.play().catch(() => {});
    const tag = this.audioCodec ? ` + ${this.audioCodec}` : '';
    this.onStatus((this.transcode ? 'live (transcoding' : 'live (stream copy') + tag + ')');
    this._loop();
  }

  _bufferedAhead() {
    const v = this.video;
    if (!v.buffered.length) return 0;
    for (let i = 0; i < v.buffered.length; i++) {
      if (v.buffered.start(i) <= v.currentTime && v.currentTime <= v.buffered.end(i)) {
        return v.buffered.end(i) - v.currentTime;
      }
    }
    return 0;
  }

  _trim() {
    const v = this.video;
    const MAX_AHEAD = 24;
    if (!v.buffered.length) return;
    if (this._bufferedAhead() > MAX_AHEAD) {
      const cut = v.currentTime - 2;
      for (let i = 0; i < v.buffered.length; i++) {
        if (v.buffered.end(i) < cut) {
          try { this.sb.remove(v.buffered.start(i), v.buffered.end(i)); } catch { /* ignore */ }
          return;
        }
      }
    }
  }

  _pump() {
    if (this.stopped || !this.sb || this.sb.updating) return;
    if (!this.queue.length) {
      if (this.ms && this.ms.readyState === 'open' && this.ended) {
        try { this.ms.endOfStream(); } catch { /* ignore */ }
      }
      return;
    }
    const buf = this.queue.shift();
    try { this.sb.appendBuffer(buf); }
    catch (e) {
      // InvalidStateError: a remove() is still updating. Queue it for updateend.
      if (e.name === 'InvalidStateError') { this.queue.unshift(buf); return; }
      // QuotaExceededError: drop the buffer and retry once
      this._trim();
      this.queue.unshift(buf);
      setTimeout(() => this._pump(), 300);
    }
  }

  async _loop() {
    let backoff = 300;
    while (!this.stopped) {
      // backpressure: do not pull faster than we can play
      let guard = 0;
      while (!this.stopped && this.queue.length > 6 && guard++ < 400) {
        await sleep(120);
      }
      let r;
      try {
        r = await fetch(`${this.fragUrl}${this.fragUrl.includes('?') ? '&' : '?'}c=${this.cursor}`);
      } catch (e) {
        if (this.stopped) return;
        this.onStatus('network error, retrying');
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 5000);
        continue;
      }
      backoff = 300;
      const cur = r.headers.get('X-NanoVMS-Cursor');
      if (cur) this.cursor = parseInt(cur, 10);
      const eof = r.headers.get('X-NanoVMS-EOF') === '1';

      if (r.status === 204) {
        if (eof) {
          this.ended = true;
          this.onStatus('end of recording');
          this._pump();
          this.onEnd();
          return;
        }
        if (this.rejected) return;
        await sleep(150);
        continue;
      }
      if (!r.ok) {
        this.onStatus(`stream error ${r.status}`);
        if (r.status === 404 || r.status === 504 || r.status === 502) {
          this.rejected = true;   // session is gone/unusable; stop infinite retries
          return;
        }
        await sleep(1000);
        continue;
      }
      const buf = await r.arrayBuffer();
      if (buf.byteLength) this.queue.push(buf);
      this._pump();
      if (eof) {
        this.ended = true;
        this.onEnd();
      }
    }
  }

  stop() {
    this.stopped = true;
    try { if (this.sb && this.ms && this.ms.readyState === 'open') this.ms.endOfStream(); } catch { /* ignore */ }
    try { this.video.pause(); } catch { /* ignore */ }
    try { this.video.removeAttribute('src'); this.video.load(); } catch { /* ignore */ }
    this.queue = [];
    this.onStatus('stopped');
  }
}

/* ------------------------------------------------------------ live as MJPEG */

class MjpegStream {
  constructor(video, url, onStatus) {
    this.video = video; this.url = url; this.onStatus = onStatus;
  }
  start() {
    this.video.src = this.url;
    this.video.play().catch(() => {});
    this.onStatus('live (mjpeg)');
    return Promise.resolve();
  }
  stop() {
    try { this.video.removeAttribute('src'); this.video.load(); } catch { /* ignore */ }
    this.onStatus('stopped');
  }
}

/* ------------------------------------------------------------------ state */

const S = {
  cameras: [],
  status: null,
  live: {},          // cam_id -> {stream, el, kind}
  rec: { cam: null, day: null, segments: [], days: [], playAt: null },
  clips: [],
  cfgDirty: false,
};
window.S = S;   // exposed for console debugging / dogfooding

/* ------------------------------------------------------------------- live */

function liveCard(cam) {
  const el = document.createElement('div');
  el.className = 'cam';
  el.dataset.cam = cam.id;
  el.innerHTML = `
    <div class="cam-head">
      <span class="cam-name">${cam.name}</span>
      <span class="cam-id">${cam.id}</span>
      <span class="cam-tag rec" data-tag="rec" style="display:none">REC</span>
      <span class="cam-tag copy" data-tag="mode" style="display:none"></span>
      <span class="sp">
        <button class="btn sm" data-act="start">Start</button>
        <button class="btn sm ghost" data-act="mute">🔇</button>
        <button class="btn sm ghost" data-act="snap">Snapshot</button>
      </span>
    </div>
    <div class="vwrap">
      <video playsinline></video>
      <div class="overlay">idle - press Start</div>
    </div>
    <div class="cam-foot" data-foot>-</div>`;
  return el;
}

function bindLiveCard(el, cam) {
  const video = $('video', el);
  const overlay = $('.overlay', el);
  const foot = $('[data-foot]', el);
  const modeTag = $('[data-tag="mode"]', el);

  const setStatus = txt => {
    foot.textContent = txt;
    overlay.textContent = txt;
    overlay.classList.toggle('hidden', /live|copy|transcod/i.test(txt));
  };

  const stop = () => {
    const cur = S.live[cam.id];
    if (cur && cur.stream) cur.stream.stop();
    delete S.live[cam.id];
    el.classList.remove('active');
    setStatus('idle - press Start');
    modeTag.style.display = 'none';
    fetch(`/api/live/${cam.id}/stop`, { method: 'POST' }).catch(() => {});
  };

  const start = async kind => {
    stop();
    el.classList.add('active');
    modeTag.style.display = '';
    modeTag.textContent = kind === 'mjpeg' ? 'mjpeg' : 'mse';
    modeTag.className = 'cam-tag ' + (kind === 'mjpeg' ? 'trans' : 'copy');
    const stream = kind === 'mjpeg'
      ? new MjpegStream(video, `/api/live/${cam.id}/mjpeg?w=1280`, setStatus)
      : new MseStream(video, `/api/live/${cam.id}/init.mp4`, `/api/live/${cam.id}/frag.mp4?cam=${cam.id}`, { onStatus: setStatus });
    S.live[cam.id] = { stream, kind };
    try {
      await stream.start();
    } catch (e) {
      setStatus('error: ' + e.message);
      delete S.live[cam.id];
      el.classList.remove('active');
    }
  };

  $('[data-act="start"]', el).addEventListener('click', e => {
    const isRunning = !!S.live[cam.id];
    if (isRunning) { stop(); e.target.textContent = 'Start'; }
    else {
      start($('#live-mjpeg').checked ? 'mjpeg' : 'mse');
      e.target.textContent = 'Stop';
    }
  });
  $('[data-act="mute"]', el).addEventListener('click', e => {
    video.muted = !video.muted;
    e.target.textContent = video.muted ? '🔇' : '🔊';
  });
  $('[data-act="snap"]', el).addEventListener('click', () => {
    window.open(`/api/snapshot/${cam.id}.jpg`, '_blank');
  });
  el.addEventListener('nv-stop', stop);
  return { start, stop, setStatus, video };
}

async function renderLive() {
  const grid = $('#live-grid');
  const running = new Set(Object.keys(S.live));
  $$('.cam', grid).forEach(el => {
    if (!S.cameras.some(c => c.id === el.dataset.cam)) el.remove();
  });
  S.cameras.filter(c => c.enabled).forEach(cam => {
    let el = $(`.cam[data-cam="${cam.id}"]`, grid);
    if (!el) {
      el = liveCard(cam);
      grid.appendChild(el);
      bindLiveCard(el, cam);
    }
    if (running.has(cam.id)) {
      el.classList.add('active');
      $('[data-act="start"]', el).textContent = 'Stop';
    }
  });
  if ($('#live-autoplay').checked) {
    for (const cam of S.cameras.filter(c => c.enabled)) {
      if (!S.live[cam.id]) {
        const el = $(`.cam[data-cam="${cam.id}"]`, grid);
        if (el) $('[data-act="start"]', el).click();
      }
    }
  }
  const st = S.status;
  $('#live-hint').textContent = st
    ? `${Object.keys(S.live).length} open - server sessions: ${(st.live || []).length}` +
      `, limit ${st.live_config ? st.live_config : ''}`
    : '';
}

/* ------------------------------------------------------------- recordings */

/* One continuous timeline, click anywhere to play from there.
   No drag-to-select, no From/To boxes, no numbered steps. */

const SHIN_SEG = 120;          // seconds of footage handed to the player at a time

async function loadDays() {
  const cam = $('#rec-cam').value;
  if (!cam) return;
  const j = await api(`/api/segments?cam=${encodeURIComponent(cam)}&days=14`);
  const days = j.days || [];
  S.rec.days = days;
  if (!days.length) {
    $('#rec-track').innerHTML = '';
    $('#rec-msg').textContent = 'no recordings yet for this camera';
    return;
  }
  // date picker: default to the newest day that has footage, and let the user
  // reach back only as far as we actually have recordings
  const d = $('#rec-date');
  if (!d.value || !days.some(x => x.day === d.value)) {
    d.value = days[0].day;
  }
  d.min = days[days.length - 1].day;
  d.max = days[0].day;
  S.rec.day = d.value;
  await loadSegments();
}

function dayBounds() {
  if (!S.rec.day) return [0, 86400];
  const base = fromInput(`${S.rec.day}T00:00:00`);
  return [base, base + 86400];
}

/* The whole day is always on screen: no zoom state, nothing to reset. */
function viewBounds() { return dayBounds(); }

async function loadSegments() {
  const cam = $('#rec-cam').value;
  if (!cam || !S.rec.day) return;
  const j = await api(`/api/segments?cam=${encodeURIComponent(cam)}&day=${S.rec.day}`);
  S.rec.segments = j.segments || [];
  drawTrack();
  const n = S.rec.segments.length;
  $('#rec-msg').textContent = n
    ? `${n} segment${n > 1 ? 's' : ''} on ${S.rec.day} — click the bar to play`
    : `no recordings on ${S.rec.day}`;
  if (n && !S.rec.playAt) S.rec.playAt = S.rec.segments[0].start;
}

function drawTrack() {
  const tl = $('#rec-track');
  const [d0, d1] = viewBounds();
  const span = d1 - d0;
  tl.innerHTML = '';
  S.rec.segments.forEach(s => {
    const a = Math.max(s.start, d0), b = Math.min(s.end, d1);
    if (b <= a) return;
    const el = document.createElement('div');
    el.className = 'tl-seg';
    el.style.left = ((a - d0) / span * 100) + '%';
    el.style.width = Math.max(0.2, (b - a) / span * 100) + '%';
    el.title = `${hhmmss(s.start)} - ${hhmmss(s.end)} (${dur(s.duration_sec)}, ${bytes(s.size)})`;
    tl.appendChild(el);
  });
  drawAxis();
  drawPlayhead();
}

function drawAxis() {
  const ax = $('#rec-axis');
  const [d0, d1] = viewBounds();
  const span = d1 - d0;
  const steps = [60, 300, 600, 900, 1800, 3600, 7200, 21600];
  const step = steps.find(s => span / s <= 12) || 21600;
  const out = [];
  for (let t0 = Math.ceil(d0 / step) * step; t0 <= d1; t0 += step) {
    out.push(`<span style="left:${(t0 - d0) / span * 100}%">${hhmmss(t0)}</span>`);
  }
  ax.innerHTML = out.join('');
  $('#rec-day').textContent = S.rec.day
    ? new Date(d0 * 1000).toLocaleDateString(undefined,
        { weekday: 'short', day: 'numeric', month: 'long' })
    : '';
}

function drawPlayhead() {
  const old = $('.tl-head', $('#rec-track'));
  if (old) old.remove();
  if (!S.rec.playAt) return;
  const [d0, d1] = viewBounds();
  const span = d1 - d0;
  const el = document.createElement('div');
  el.className = 'tl-head';
  el.style.left = ((S.rec.playAt - d0) / span * 100) + '%';
  $('#rec-track').appendChild(el);
}

function trackTime(e) {
  const r = $('#rec-track').getBoundingClientRect();
  const [d0, d1] = viewBounds();
  const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  return d0 + f * (d1 - d0);
}

let recStream = null;

/* Snap a requested time onto real footage so a click in a gap still plays. */
function nearestFootage(t) {
  if (!S.rec.segments.length) return t;
  for (const s of S.rec.segments) {
    if (t >= s.start && t <= s.end) return t;
  }
  let best = S.rec.segments[0], bd = Infinity;
  for (const s of S.rec.segments) {
    const d = t < s.start ? s.start - t : t - s.end;
    if (d < bd) { bd = d; best = s; }
  }
  return t < best.start ? best.start : best.end;
}

async function playAt(t) {
  const cam = $('#rec-cam').value;
  if (!cam) { toast('pick a camera', 'bad'); return; }
  if (!S.rec.segments.length) { toast('no footage on this date', 'bad'); return; }
  const start = nearestFootage(t);
  const end = start + SHIN_SEG;
  S.rec.playAt = start;
  drawPlayhead();
  if (recStream) recStream.stop();
  const video = $('#rec-player');
  const info = $('#rec-player-info');
  const q = `cam=${encodeURIComponent(cam)}&start=${start}&end=${end}`;
  const meta = await api('/api/play?' + q);
  const info0 = `${new Date(start * 1000).toLocaleString()} · ` +
    `${meta.session.segments} segment(s) · ${dur(meta.session.duration)}` +
    (meta.session.skipped_segments && meta.session.skipped_segments.length
      ? ` · ${meta.session.skipped_segments.length} file(s) skipped (damaged)` : '');
  info.textContent = info0;
  $('#rec-when').textContent = new Date(start * 1000).toLocaleString();
  // use the server's own init/frag URLs: the media endpoints take s=/e= while
  // the plan endpoint takes start=/end=, and hand-building them here is how the
  // two drifted apart before
  recStream = new MseStream(video, meta.init_url, meta.frag_url, {
    onStatus: txt => { if (txt === 'end of recording' || txt === 'connecting') info.textContent = txt; },
    onEnd: () => {},
  });
  try { await recStream.start(); }
  catch (err) { toast('playback: ' + err.message, 'bad'); info.textContent = err.message; }
}

function nudge(sec) {
  if (!S.rec.playAt) return;
  playAt(S.rec.playAt + sec);
}

async function exportSelection() {
  const cam = $('#rec-cam').value;
  const s = S.rec.playAt;
  if (!cam || !s) { toast('click the timeline to pick a time first', 'bad'); return; }
  const e = s + SHIN_SEG;
  const btn = $('#rec-export');
  btn.disabled = true; btn.textContent = 'Exporting...';
  try {
    const j = await api('/api/clips', {
      method: 'POST',
      body: {
        cam, start: s, end: e,
        name: `${cam} ${new Date(s * 1000).toLocaleString()}`,
        precise: $('#rec-precise').checked,
        audio: $('#rec-audio').checked,
      },
    });
    const msg = `clip saved: ${bytes(j.clip.size)}, ${j.elapsed_sec}s — see Clips tab`;
    toast(msg, 'ok');
    loadClips();
  } catch (err) {
    toast('export failed: ' + err.message, 'bad');
  } finally {
    btn.disabled = false; btn.textContent = '⬇ Export clip';
  }
}

/* ------------------------------------------------------------------ clips */

async function loadClips() {
  const f = $('#clip-cam-filter').value;
  const j = await api('/api/clips' + (f ? `?cam=${encodeURIComponent(f)}` : ''));
  S.clips = j.clips || [];
  const tb = $('#clip-table tbody');
  tb.innerHTML = '';
  $('#clip-hint').textContent = `${S.clips.length} clip(s), ${bytes(S.clips.reduce((a, c) => a + c.size, 0))}`;
  S.clips.forEach(c => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${c.name || c.id}</td>
      <td class="mono">${c.cam_id}</td>
      <td class="mono">${new Date(c.start * 1000).toLocaleString()}</td>
      <td>${dur(c.duration_sec)}</td>
      <td>${bytes(c.size)}</td>
      <td></td>`;
    const cell = tr.lastElementChild;
    const play = document.createElement('button');
    play.className = 'btn sm ghost'; play.textContent = 'Play';
    play.addEventListener('click', () => playClip(c));
    const dl = document.createElement('a');
    dl.className = 'btn sm ghost'; dl.textContent = 'Download';
    dl.href = `/api/clip-file?id=${c.id}`; dl.setAttribute('download', '');
    dl.style.marginLeft = '6px';
    const del = document.createElement('button');
    del.className = 'btn sm danger'; del.textContent = 'Delete';
    del.style.marginLeft = '6px';
    del.addEventListener('click', async () => {
      if (!confirm(`Delete clip "${c.name}"?`)) return;
      await api(`/api/clips/${c.id}`, { method: 'DELETE' });
      toast('clip deleted', 'ok');
      loadClips();
    });
    cell.append(play, dl, del);
    tb.appendChild(tr);
  });
}

let clipStream = null;
async function playClip(c) {
  if (clipStream) clipStream.stop();
  const video = $('#clip-player');
  const info = $('#clip-player-info');
  info.textContent = `${c.name} - ${c.cam_id} - ${new Date(c.start * 1000).toLocaleString()}`;
  clipStream = new MseStream(
    video,
    `/api/clip/play/init.mp4?id=${c.id}`,
    `/api/clip/play/frag.mp4?id=${c.id}`,
    { onStatus: t => { info.textContent = t; } });
  try { await clipStream.start(); }
  catch (e) { toast('clip playback: ' + e.message, 'bad'); }
}

/* ---------------------------------------------------------------- storage */

async function loadStorage() {
  const j = await api('/api/storage');
  const d = j.disk;
  const pct = d.percent;
  const cls = pct > 92 ? 'bad' : (pct > 80 ? 'warn' : '');
  $('#storage-cards').innerHTML = `
    <div class="card"><div class="k">Recordings</div>
      <div class="v">${bytes(j.bytes)}</div>
      <div class="s">${j.cameras.reduce((a, c) => a + c.segments, 0)} segments</div></div>
    <div class="card"><div class="k">Clips</div>
      <div class="v">${j.clips.count}</div><div class="s">${bytes(j.clips.bytes)}</div></div>
    <div class="card"><div class="k">Disk used</div>
      <div class="v">${pct}%</div>
      <div class="s">${bytes(d.free)} free of ${bytes(d.total)}</div>
      <div class="bar"><i class="${cls}" style="width:${Math.min(pct, 100)}%"></i></div></div>
    <div class="card"><div class="k">Folder</div>
      <div class="v" style="font-size:13px;word-break:break-all">${j.root}</div></div>`;
  const tb = $('#storage-table tbody');
  tb.innerHTML = j.cameras.map(c => `
    <tr><td>${c.name}</td><td class="mono">${c.segments}</td>
    <td class="mono">${c.days}</td><td class="mono">${bytes(c.bytes)}</td></tr>`).join('');

  // Warn when auto-retention is actively eating footage, so a range that
  // vanishes from the timeline is never mistaken for a playback bug.
  try {
    const w = await api('/api/sweep/why');
    const why = $('#sweep-warn');
    if (w.over_percent || w.over_space) {
      const bits = [];
      if (w.over_percent) bits.push(`disk ${w.disk_percent}% is over the ${w.max_usage_percent}% limit`);
      if (w.over_space) bits.push(`only ${w.free_gb} GB free (limit ${w.keep_free_gb} GB)`);
      why.innerHTML = `<div class="sweep-warn">&#9888; Auto-cleanup is deleting your oldest recordings every
        ${Math.round(w.sweep_interval_sec / 60)} min because ${bits.join(' and ')}.
        Recordings you can see on the timeline may be removed before you play them.
        Raise <b>Max disk %</b> in Setup, or free up space.</div>`;
    } else {
      why.innerHTML = '';
    }
  } catch { /* endpoint unavailable */ }
}

async function runSweep(dry) {
  const btn = dry ? $('#sweep-dry') : $('#sweep-run');
  btn.disabled = true;
  try {
    const j = await api(`/api/sweep?dry=${dry ? 1 : 0}`, { method: 'POST' });
    const totalItems = j.items ? j.items.length : 0;
    $('#sweep-hint').textContent =
      `${dry ? 'would free' : 'freed'} ${bytes(j.freed)} across ${j.removed} item(s)` +
      (totalItems ? ` (showing ${totalItems})` : '');
    toast(`${dry ? 'dry run' : 'cleanup'}: ${bytes(j.freed)} ${dry ? 'would be freed' : 'freed'}`, 'ok');
    if (!dry) { loadStorage(); loadDays(); }
  } catch (e) {
    toast('cleanup failed: ' + e.message, 'bad');
  } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------------ setup */

const FIELDS = [
  ['#cfg-seg', ['storage', 'segment_minutes'], Number],
  ['#cfg-ret', ['storage', 'retention_days'], Number],
  ['#cfg-maxpct', ['storage', 'max_usage_percent'], Number],
  ['#cfg-freegb', ['storage', 'keep_free_gb'], Number],
  ['#cfg-clipret', ['storage', 'clips_retention_days'], Number],
  ['#cfg-root', ['storage', 'root'], String],
  ['#cfg-maxlive', ['live', 'max_concurrent'], Number],
  ['#cfg-idle', ['live', 'idle_timeout_sec'], Number],
  ['#cfg-fps', ['live', 'fps'], Number],
  ['#cfg-width', ['live', 'max_width'], Number],
];

function fillConfigForm(cfg) {
  FIELDS.forEach(([sel, path, cast]) => {
    const v = path.reduce((o, k) => (o || {})[k], cfg);
    if (v !== undefined) $(sel).value = v;
  });
}

function readConfigForm(cfg) {
  const out = JSON.parse(JSON.stringify(cfg));
  FIELDS.forEach(([sel, path, cast]) => {
    const v = $(sel).value;
    let cur = out;
    for (const k of path.slice(0, -1)) cur = cur[k] = cur[k] || {};
    cur[path.at(-1)] = cast === Number ? Number(v) : v;
  });
  return out;
}

function renderCamList() {
  const box = $('#cam-list');
  box.innerHTML = '';
  if (!S.cameras.length) {
    box.innerHTML = '<p class="hint">No cameras yet. Add one below - use "Test URL" first.</p>';
    return;
  }
  S.cameras.forEach(cam => {
    const r = cam.recorder || {};
    const row = document.createElement('div');
    row.className = 'camrow';
    row.innerHTML = `
      <div class="grow" style="max-width:180px"><input value="${cam.name}" data-f="name"></div>
      <div class="grow"><input value="${cam.url}" data-f="url" class="mono"></div>
      <label class="chk"><input type="checkbox" data-f="record" ${cam.record ? 'checked' : ''}> rec</label>
            <label class="chk"><input type="checkbox" data-f="audio" ${cam.audio ? 'checked' : ''}> audio</label>
            <span class="cam-tag ${r.state === 'recording' ? 'rec' : ''}">${r.state || 'idle'}</span>
            <span class="hint">pid ${r.pid || '-'}${r.restarts ? ` &middot; ${r.restarts} restarts` : ''}</span>
            <details class="camadv"><summary title="Live view options">live</summary>
              <label class="chk" title="Copy the camera's video straight to the browser. Faster and cheaper, but only works when the browser can decode the camera's codec and the camera emits keyframes often. Untick for HEVC.">
                <input type="checkbox" data-f="live_passthrough" ${cam.live_passthrough !== false ? 'checked' : ''}> stream-copy</label>
              <label class="chk" title="Rewrite camera wallclock timestamps to start at zero. Leave on unless a source already starts near zero - a frozen single frame usually means this was wrong, and a blank tile usually means stream-copy was wrong.">
                <input type="checkbox" data-f="live_rebase_ts" ${cam.live_rebase_ts !== false ? 'checked' : ''}> rebase ts</label>
            </details>`;
    const btns = document.createElement('span');
    btns.style.display = 'flex';
    btns.style.gap = '6px';
    const mk = (label, fn, cls = 'ghost') => {
      const b = document.createElement('button');
      b.className = 'btn sm ' + cls;
      b.textContent = label;
      b.addEventListener('click', fn);
      return b;
    };
    btns.append(
      mk('Save', async () => {
        const body = {};
        $$('input[data-f]', row).forEach(i => {
          body[i.dataset.f] = i.type === 'checkbox' ? i.checked : i.value;
        });
        try {
          await api(`/api/cameras/${cam.id}`, { method: 'PUT', body });
          toast('camera saved', 'ok');
          refresh();
        } catch (e) { toast('save failed: ' + e.message, 'bad'); }
      }),
      mk('Restart', async () => {
        await api(`/api/cameras/${cam.id}/motion`, { method: 'POST', body: { action: 'restart' } });
        toast('recorder restarted', 'ok');
        refresh();
      }),
      mk('Delete', async () => {
        if (!confirm(`Remove camera "${cam.name}"? Recordings on disk are kept.`)) return;
        await api(`/api/cameras/${cam.id}`, { method: 'DELETE' });
        toast('camera removed', 'ok');
        refresh();
      }, 'danger'));
    row.appendChild(btns);
    if (r.last_error) {
      const e = document.createElement('div');
      e.className = 'hint';
      e.style.color = 'var(--warn)';
      e.style.width = '100%';
      e.textContent = 'last error: ' + r.last_error;
      row.appendChild(e);
    }
    box.appendChild(row);
  });
}

/* -------------------------------------------------------------- top level */

async function refresh() {
  try {
    const st = await api('/api/status');
    S.status = st;
    S.cameras = (await api('/api/cameras')).cameras || [];
    st.live_config = st.live ? (window.__maxlive || '') : '';
    $('#health-dot').className = 'dot ' + (st.sys.ffmpeg_ok ? 'ok' : 'bad');

    const rec = st.recorders.filter(r => r.state === 'recording').length;
    const errs = st.recorders.filter(r => r.state === 'error').length;
    const d = st.storage.disk;
    $('#pills').innerHTML = `
      <span class="pill ${rec ? 'rec' : ''}"><b>${rec}</b>/${S.cameras.length} recording</span>
      ${errs ? `<span class="pill hot"><b>${errs}</b> error</span>` : ''}
      <span class="pill ${d.percent > 88 ? 'hot' : ''}">disk <b>${d.percent}%</b></span>
      <span class="pill">storage <b>${bytes(st.storage.bytes)}</b></span>
      <span class="pill">live <b>${(st.live || []).length}</b></span>`;
    if (st.sys.ffmpeg_ok === false) {
      toast('ffmpeg not found - recording and live view cannot work', 'bad');
    }

    $('#sys-info').textContent = JSON.stringify({
      host: st.sys.host, python: st.sys.python, ffmpeg: st.sys.ffmpeg,
      cpus: st.sys.cpu_count, uptime_sec: st.sys.uptime_sec,
      storage_root: st.storage.root, disk: st.storage.disk,
    }, null, 2);

    fillSelect('#rec-cam', S.cameras, S.cameras[0] && S.cameras[0].id);
    fillSelect('#clip-cam-filter', [{ id: '', name: 'all' }, ...S.cameras], '');
    if (!S.cfgLoaded) {
      fillConfigForm((await api('/api/config')).config);
      S.cfgLoaded = true;
      // show validation for current recordings folder
      const rootInput = $('#cfg-root');
      if (rootInput && rootInput.value) rootInput.dispatchEvent(new Event('input'));
    }
    renderCamList();
    renderLive();
  } catch (e) {
    $('#health-dot').className = 'dot bad';
    toast('server error: ' + e.message, 'bad');
  }
}

function fillSelect(sel, cams, keep) {
  const el = $(sel);
  const cur = el.value || keep;
  el.innerHTML = cams.map(c => `<option value="${c.id}">${c.name || c.id}</option>`).join('');
  if (cur && [...el.options].some(o => o.value === cur)) el.value = cur;
}

/* ------------------------------------------------------------------- init */

$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  $$('.panel').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $('#tab-' + t.dataset.tab).classList.add('active');
  if (t.dataset.tab === 'storage') loadStorage();
  if (t.dataset.tab === 'clips') loadClips();
  if (t.dataset.tab === 'recordings') loadDays();
}));

$('#btn-refresh').addEventListener('click', () => { refresh(); loadStorage(); });
$('#rec-cam').addEventListener('change', () => { S.rec.playAt = null; loadDays(); });
$('#rec-date').addEventListener('change', () => {
  S.rec.day = $('#rec-date').value;
  S.rec.playAt = null;
  loadSegments();
});
$('#rec-reload').addEventListener('click', () => { S.rec.playAt = null; loadDays(); });
$('#rec-export').addEventListener('click', exportSelection);
$('#clip-reload').addEventListener('click', loadClips);

// click anywhere on the bar to play from there; drag to scrub
let scrubbing = false;
$('#rec-track').addEventListener('mousedown', e => {
  if (!S.rec.segments.length) return;
  scrubbing = true;
  playAt(trackTime(e));
});
window.addEventListener('mousemove', e => { if (scrubbing) playAt(trackTime(e)); });
window.addEventListener('mouseup', () => { scrubbing = false; });

$('#rec-back').addEventListener('click', () => nudge(-300));
$('#rec-rew').addEventListener('click', () => nudge(-60));
$('#rec-fwd').addEventListener('click', () => nudge(60));
$('#rec-fwd2').addEventListener('click', () => nudge(300));
$$('#rec-speed button').forEach(b => b.addEventListener('click', () => {
  $$('#rec-speed button').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  const v = $('#rec-player');
  v.playbackRate = parseFloat(b.dataset.rate) || 1;
}));
$('#clip-cam-filter').addEventListener('change', loadClips);
$('#sweep-dry').addEventListener('click', () => runSweep(true));
$('#sweep-run').addEventListener('click', () => runSweep(false));

$('#new-test').addEventListener('click', async () => {
  const url = $('#new-url').value.trim();
  const out = $('#new-test-out');
  if (!url) { toast('enter a URL first', 'bad'); return; }
  out.className = 'test-out';
  out.textContent = 'probing...';
  try {
    const j = await api('/api/test', {
      method: 'POST',
      body: { url, transport: $('#new-transport').value || 'tcp' },
    });
    out.className = 'test-out ok';
    out.textContent =
      `OK in ${j.elapsed}s\n` +
      `video   : ${j.video.codec} ${j.video.width}x${j.video.height} @ ${j.video.fps} fps\n` +
      `audio   : ${j.audio ? j.audio.codec : 'none'}\n` +
      `bitrate : ${j.bitrate_kbps} kbps (~${j.est_gb_per_day} GB/day if recorded)\n` +
      `live    : ${j.browser_playable ? 'stream copy, no transcode' : 'WILL TRANSCODE'}\n` +
      `note    : ${j.note}`;
  } catch (e) {
    out.className = 'test-out bad';
    out.textContent = 'FAILED: ' + e.message;
  }
});

$('#new-add').addEventListener('click', async () => {
  const name = $('#new-name').value.trim();
  const url = $('#new-url').value.trim();
  if (!url) { toast('URL required', 'bad'); return; }
  try {
    await api('/api/cameras', {
      method: 'POST',
      body: {
        name: name || url,
        url,
        record: $('#new-record').checked,
                audio: $('#new-audio').checked,
                live_passthrough: $('#new-live-passthrough').checked,
                live_rebase_ts: $('#new-live-rebase-ts').checked,
                transport: $('#new-transport').value,
      },
    });
    $('#new-name').value = ''; $('#new-url').value = '';
    $('#new-test-out').textContent = '';
    toast('camera added', 'ok');
    refresh();
  } catch (e) { toast('add failed: ' + e.message, 'bad'); }
});

// ---- recordings folder picker ----
(function () {
  const inp = $('#cfg-root');
  const hint = $('#cfg-root-hint');
  const modal = $('#fs-modal');
  const dirBox = $('#fs-dirs');
  let cur = '';                       // folder the browser is currently showing
  let parentOf = '';                  // server-reported parent for the current folder

  async function validatePath(p) {
    if (!p) { hint.textContent = ''; hint.style.color = ''; return; }
    try {
      const j = await api('/api/validate-path', { method: 'POST', body: { path: p } });
      if (!j.exists) {
        hint.textContent = '⚠ folder not found — will be created on save';
        hint.style.color = 'var(--warn)';
      } else if (!j.writable) {
        hint.textContent = '✕ folder exists but is not writable';
        hint.style.color = 'var(--bad)';
      } else {
        hint.textContent = '✓ ' + j.path;
        hint.style.color = 'var(--ok)';
      }
    } catch { hint.textContent = ''; }
  }

  let validateTimer = null;
  inp.addEventListener('input', () => {
    clearTimeout(validateTimer);
    validateTimer = setTimeout(() => validatePath(inp.value.trim()), 600);
  });

  // ---- modal folder browser (server-side; no browser file-upload prompt) ----
  function close() { modal.style.display = 'none'; }

  async function loadDrives() {
    const box = $('#fs-drives');
    try {
      const j = await api('/api/fs/drives');
      if (!j.drives || j.drives.length < 2) { box.style.display = 'none'; return; }
      box.style.display = '';
      box.innerHTML = '';
      j.drives.forEach(d => {
        const b = document.createElement('button');
        b.className = 'btn sm ghost';
        b.textContent = d.name + (d.free_gb ? ` (${d.free_gb} GB free)` : '');
        b.addEventListener('click', () => listDir(d.path));
        box.appendChild(b);
      });
    } catch { box.style.display = 'none'; }
  }

  async function listDir(path) {
    dirBox.innerHTML = '<div class="hint">loading…</div>';
    try {
      const j = await api('/api/fs/browse?path=' + encodeURIComponent(path || ''));
      cur = j.path;
      parentOf = j.parent || '';       // trust the server, not string splitting
      $('#fs-path').textContent = j.path;
      $('#fs-up').disabled = !parentOf;
      dirBox.innerHTML = '';
      if (!j.dirs.length) {
        dirBox.innerHTML = '<div class="hint">no subfolders here — this folder can be used as-is</div>';
      }
      j.dirs.forEach(name => {
        const full = j.path.replace(/[\\/]+$/, '') + '\\' + name;
        const row = document.createElement('button');
        row.className = 'fs-row';
        row.innerHTML = '📁 ' + escapeHtml(name);
        row.addEventListener('click', () => listDir(full));
        dirBox.appendChild(row);
      });
      if (j.truncated) {
        const more = document.createElement('div');
        more.className = 'hint';
        more.textContent = '…more folders not shown';
        dirBox.appendChild(more);
      }
    } catch (e) {
      dirBox.innerHTML = `<div class="hint" style="color:var(--bad)">${escapeHtml(e.message)}</div>`;
    }
  }

  $('#cfg-root-browse').addEventListener('click', () => {
    modal.style.display = 'flex';
    loadDrives();
    listDir(inp.value.trim());
  });
  $('#fs-close').addEventListener('click', close);
  $('#fs-cancel').addEventListener('click', close);
  modal.addEventListener('click', e => { if (e.target === modal) close(); });

  // paths arrive from the OS, so join with the separator the server uses
  const join = (base, name) => base.replace(/[\\/]+$/, '') + '\\' + name;

  $('#fs-up').addEventListener('click', () => {
    if (parentOf) listDir(parentOf);
  });

  $('#fs-new').addEventListener('click', async () => {
    const name = prompt('New folder name:', 'recordings');
    if (!name) return;
    try {
      const j = await api('/api/fs/mkdir', { method: 'POST', body: { path: join(cur, name) } });
      $('#fs-hint').textContent = 'created';
      listDir(j.path);
    } catch (e) {
      $('#fs-hint').textContent = 'could not create: ' + e.message;
    }
  });

  $('#fs-use').addEventListener('click', () => {
    if (!cur) { close(); return; }
    inp.value = cur;
    validatePath(cur);
    close();
  });
})();

$('#cfg-save').addEventListener('click', async () => {
  try {
    const cfg = readConfigForm((await api('/api/config')).config);
    await api('/api/config', { method: 'PUT', body: cfg });
    toast('settings saved', 'ok');
    refresh();
  } catch (e) { toast('save failed: ' + e.message, 'bad'); }
});

$('#cfg-reset').addEventListener('click', async () => {
  if (!confirm('Reset all settings to defaults? Cameras are kept.')) return;
  await api('/api/config/reset', { method: 'POST' });
  S.cfgLoaded = false;
  toast('settings reset', 'ok');
  refresh();
});

refresh();
setInterval(() => { if (!$('#live-autoplay').checked) refresh(); }, 15000);
