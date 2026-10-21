/**
 * Thin API client for the FastAPI backend.
 *
 * The base URL is configurable via VITE_API_BASE (see frontend/.env.example).
 * Defaults to the local FastAPI dev server on port 8000.
 */

const API_BASE = String(import.meta.env?.VITE_API_BASE || 'http://localhost:8000').replace(
  /\/+$/,
);

const TIMEOUT_MS = 60000;

async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      ...options,
    });

    if (!response.ok) {
      const body = await response.text().catch(() => '');
      throw new Error(
        `${response.status} ${response.statusText}${body ? ` — ${body.slice(0, 240)}` : ''}`,
      );
    }
    return await response.json();
  } catch (error) {
    if (error.name === 'AbortError') {
      throw new Error('The request timed out. Is the backend still warming up?');
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function sendQuestion(question) {
  return request('/api/chat', {
    method: 'POST',
    body: JSON.stringify({ question }),
  });
}

export async function fetchMeta() {
  return request('/api/meta');
}

export async function checkHealth() {
  return request('/api/health');
}

export { API_BASE };
