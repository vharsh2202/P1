/**
 * Thin API client for the FastAPI backend.
 *
 * The base URL is configurable via VITE_API_BASE (see frontend/.env.example).
 * Defaults to the local FastAPI dev server on port 8000.
 */

const API_BASE = String(import.meta.env?.VITE_API_BASE || 'http://localhost:8000').replace(
  /\/+$/,
);

const TIMEOUT_MS = 120000;

async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      signal: controller.signal,
      ...options,
    });

    if (!response.ok) {
      const body = await response.text().catch(() => '');
      let detail = body;
      try {
        const parsed = JSON.parse(body);
        detail = parsed.detail ? JSON.stringify(parsed.detail) : body;
      } catch {
        /* keep the raw text */
      }
      throw new Error(
        `${response.status} ${response.statusText}${detail ? ` — ${String(detail).slice(0, 240)}` : ''}`,
      );
    }
    return await response.json();
  } catch (error) {
    if (error.name === 'AbortError') {
      throw new Error('The request timed out. Is the backend still working?');
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/* -------------------------------------------------------------------------- *
 * Health & metadata
 * -------------------------------------------------------------------------- */

export function checkHealth() {
  return request('/api/health');
}

export function fetchMeta() {
  return request('/api/meta');
}

export function fetchSchema() {
  return request('/api/schema');
}

export function fetchInsights() {
  return request('/api/insights');
}

/* -------------------------------------------------------------------------- *
 * Datasets
 * -------------------------------------------------------------------------- */

export function listDatasets() {
  return request('/api/datasets');
}

/**
 * Upload files with real progress reporting.
 *
 * `fetch` cannot report upload progress, so this one call uses XHR. The
 * callback receives a number between 0 and 1.
 */
export function uploadDataset(files, { onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const file of files) form.append('files', file, file.name);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}/api/datasets`);
    xhr.timeout = TIMEOUT_MS;

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded / event.total);
      }
    };

    xhr.onload = () => {
      let payload;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        payload = { detail: xhr.responseText };
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(1);
        resolve(payload);
      } else {
        reject(
          new Error(
            `Upload failed (${xhr.status})${payload?.detail ? ` — ${String(payload.detail)}` : ''}`,
          ),
        );
      }
    };

    xhr.onerror = () => reject(new Error('Could not reach the analytics API.'));
    xhr.ontimeout = () => reject(new Error('The upload timed out.'));
    xhr.send(form);
  });
}

export function activateDataset(id) {
  return request(`/api/datasets/${encodeURIComponent(id)}/activate`, { method: 'POST' });
}

export function deleteDataset(id) {
  return request(`/api/datasets/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/* -------------------------------------------------------------------------- *
 * Chat
 * -------------------------------------------------------------------------- */

export function sendQuestion(question) {
  return request('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  });
}

export { API_BASE };
