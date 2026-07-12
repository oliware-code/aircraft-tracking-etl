const STALE_THRESHOLD_SECONDS = 24 * 3600;

function formatElapsed(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

function formatISO8601(epochSeconds) {
  return new Date(epochSeconds * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function updateLiveTimers() {
  const now = Date.now() / 1000;
  document.querySelectorAll('.live-since').forEach((el) => {
    const epoch = parseFloat(el.dataset.epoch);
    if (isNaN(epoch)) return;

    const elapsed = now - epoch;
    const stale = elapsed > STALE_THRESHOLD_SECONDS;
    const compact = el.classList.contains('live-since-compact');

    // Compact (map tags): always the ticking counter. Table cells: always the ISO 8601 timestamp.
    el.textContent = compact ? formatElapsed(elapsed) : formatISO8601(epoch);
    el.classList.toggle('live-since-stale', stale);
  });

  document.querySelectorAll('.status-duration').forEach((el) => {
    const epoch = parseFloat(el.dataset.epoch);
    if (!isNaN(epoch)) {
      el.textContent = formatElapsed(now - epoch);
    }
  });
}

updateLiveTimers();
setInterval(updateLiveTimers, 1000);
