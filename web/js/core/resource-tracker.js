/**
 * ResourceTracker — tracks intervals, timeouts, listeners, AbortControllers.
 * Each page module gets one tracker; calling cleanupAll() on unmount
 * guarantees zero leaked resources.
 */
export class ResourceTracker {
  constructor(label = 'anon') {
    this._label = label;
    this._intervals = new Set();
    this._timeouts = new Set();
    this._listeners = []; // {target, event, handler, options}
    this._abortControllers = new Set();
  }

  /* -- Intervals -- */
  trackInterval(fn, ms) {
    const id = setInterval(fn, ms);
    this._intervals.add(id);
    return id;
  }

  clearTrackedInterval(id) {
    clearInterval(id);
    this._intervals.delete(id);
  }

  /* -- Timeouts -- */
  trackTimeout(fn, ms) {
    const id = setTimeout(() => {
      this._timeouts.delete(id);
      fn();
    }, ms);
    this._timeouts.add(id);
    return id;
  }

  clearTrackedTimeout(id) {
    clearTimeout(id);
    this._timeouts.delete(id);
  }

  /* -- Event listeners -- */
  trackEventListener(target, event, handler, options) {
    target.addEventListener(event, handler, options);
    this._listeners.push({ target, event, handler, options });
  }

  /** Shorthand alias for trackEventListener. */
  on(target, event, handler, options) {
    return this.trackEventListener(target, event, handler, options);
  }

  /* -- AbortControllers (for fetch) -- */
  trackAbortController() {
    const ac = new AbortController();
    this._abortControllers.add(ac);
    return ac;
  }

  removeAbortController(ac) {
    this._abortControllers.delete(ac);
  }

  /* -- Cleanup everything -- */
  cleanupAll() {
    // Intervals
    for (const id of this._intervals) clearInterval(id);
    this._intervals.clear();

    // Timeouts
    for (const id of this._timeouts) clearTimeout(id);
    this._timeouts.clear();

    // Listeners & Generic cleanups
    for (const item of this._listeners) {
      if (item.cleanup) {
        try { item.cleanup(); } catch (err) { console.warn(`[ResourceTracker:${this._label}] cleanup error`, err); }
      } else if (item.target) {
        item.target.removeEventListener(item.event, item.handler, item.options);
      }
    }
    this._listeners.length = 0;

    // Abort controllers
    for (const ac of this._abortControllers) {
      try { ac.abort(); } catch { /* already aborted */ }
    }
    this._abortControllers.clear();
  }

  /* -- Generic resources -- */
  trackResource(fn) {
    if (typeof fn === 'function') {
      this._listeners.push({ cleanup: fn });
    }
  }

  /* -- Diagnostics -- */
  stats() {
    return {
      label: this._label,
      intervals: this._intervals.size,
      timeouts: this._timeouts.size,
      listeners: this._listeners.length,
      abortControllers: this._abortControllers.size
    };
  }
}
