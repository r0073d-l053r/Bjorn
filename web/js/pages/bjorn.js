/**
 * Bjorn page module — EPD (e-paper display) live view.
 *
 * Displays a live-updating screenshot of the Bjorn device's e-paper display.
 * The image is refreshed at a configurable interval fetched from /get_web_delay.
 * Supports mouse-wheel zoom and auto-fits to the container on window resize.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $ } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'bjorn';
const DEFAULT_DELAY = 5000;
const ZOOM_FACTOR = 1.1;

let tracker = null;
let refreshInterval = null;
let currentScale = 1;
let delay = DEFAULT_DELAY;
let imgEl = null;
let containerEl = null;

/* ============================
 * Mount
 * ============================ */
export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  currentScale = 1;

  // Fetch the configured refresh delay
  try {
    const data = await api.get('/get_web_delay', { timeout: 5000, retries: 1 });
    if (!tracker) return; /* unmounted while awaiting */
    if (data && typeof data.web_delay === 'number' && data.web_delay > 0) {
      delay = data.web_delay;
    }
  } catch (err) {
    if (!tracker) return; /* unmounted while awaiting */
    console.warn(`[${PAGE}] Failed to fetch web_delay, using default ${DEFAULT_DELAY}ms:`, err.message);
    delay = DEFAULT_DELAY;
  }

  // Build layout
  imgEl = el('img', {
    src: `/web/screen.png?t=${Date.now()}`,
    alt: t('nav.bjorn'),
    class: 'bjorn-epd-img',
    style: {
      maxWidth: '100%',
      maxHeight: '100%',
      width: 'auto',
      objectFit: 'contain',
      display: 'block',
    },
    draggable: 'false',
  });

  containerEl = el('div', {
    class: 'bjorn-container', style: {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      width: '100%',
      height: '100%',
      overflow: 'hidden',
    }
  }, [imgEl]);

  container.appendChild(containerEl);

  // Click to toggle UI (restored from old version)
  const onImageClick = () => {
    const topbar = $('.topbar');
    const bottombar = $('.bottombar');
    const console = $('.console');
    const appContainer = $('#app');

    const toggle = (el) => {
      if (!el) return;
      el.style.display = (el.style.display === 'none') ? '' : 'none';
    };

    toggle(topbar);
    toggle(bottombar);
    toggle(console);

    // Expand/restore app-container to use full space when bars hidden
    if (appContainer) {
      const barsHidden = topbar && topbar.style.display === 'none';
      if (barsHidden) {
        appContainer.style.position = 'fixed';
        appContainer.style.inset = '0';
        appContainer.style.zIndex = '50';
      } else {
        appContainer.style.position = '';
        appContainer.style.inset = '';
        appContainer.style.zIndex = '';
      }
    }

    // 🔥 Force reflow + refit after layout change
    requestAnimationFrame(() => {
      fitToContainer();
    });
  };
  tracker.trackEventListener(imgEl, 'click', onImageClick);

  // Fit image to container on initial load
  fitToContainer();

  // Set up periodic image refresh
  refreshInterval = tracker.trackInterval(() => refreshImage(), delay);

  // Mouse wheel zoom
  const onWheel = (e) => {
    e.preventDefault();
    if (e.deltaY < 0) {
      currentScale *= ZOOM_FACTOR;
    } else {
      currentScale /= ZOOM_FACTOR;
    }
    applyZoom();
  };
  tracker.trackEventListener(containerEl, 'wheel', onWheel, { passive: false });

  // Window resize: re-fit image to container
  const onResize = () => fitToContainer();
  tracker.trackEventListener(window, 'resize', onResize);
}

/* ============================
 * Unmount — guaranteed cleanup
 * ============================ */
export function unmount() {
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  refreshInterval = null;
  imgEl = null;
  containerEl = null;
  currentScale = 1;
}

/* ============================
 * Image refresh (graceful swap)
 * ============================ */
function refreshImage() {
  if (!imgEl) return;

  const loader = new Image();
  const cacheBust = `/web/screen.png?t=${Date.now()}`;

  loader.onload = () => {
    // Only swap if the element is still mounted
    if (imgEl) {
      imgEl.src = cacheBust;
    }
  };

  // On error: keep the old image, do nothing
  loader.onerror = () => {
    console.debug(`[${PAGE}] Image refresh failed, keeping current frame`);
  };

  loader.src = cacheBust;
}

/* ============================
 * Zoom helpers
 * ============================ */
function applyZoom() {
  if (!imgEl || !containerEl) return;
  const baseHeight = containerEl.clientHeight;
  imgEl.style.height = `${baseHeight * currentScale}px`;
  imgEl.style.width = 'auto';
  imgEl.style.maxWidth = 'none';
  imgEl.style.maxHeight = 'none';
}

function fitToContainer() {
  if (!imgEl || !containerEl) return;
  // Reset scale on resize so the image re-fits
  currentScale = 1;
  imgEl.style.height = `${containerEl.clientHeight}px`;
  imgEl.style.width = 'auto';
  imgEl.style.maxWidth = '100%';
  imgEl.style.maxHeight = '100%';
}
