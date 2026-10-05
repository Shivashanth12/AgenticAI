export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type LinkItem = {
  id: string;
  alias: string;
  target_url: string;
  title: string | null;
  status: string;
  click_count: number;
  version: number;
  expires_at: string | null;
  created_at: string;
};

export type Workflow = {
  id: string;
  scenario: string;
  requirement: string;
  status: string;
  risk_level: string;
  provider: string;
  created_at: string;
};

export type ActivityEvent = {
  id: string;
  action: string;
  action_label: string;
  resource_type: string;
  resource_id: string;
  resource_label: string;
  description: string;
  alias: string | null;
  destination_url: string | null;
  actor: string;
  outcome: string;
  detail: Record<string, unknown>;
  correlation_id: string;
  created_at: string;
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = typeof window === "undefined" ? null : window.sessionStorage.getItem("linkdesk_token");
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
    cache: "no-store",
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(error.detail ?? "Request failed");
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function saveAccessToken(token: string) {
  window.sessionStorage.setItem("linkdesk_token", token);
}


export async function downloadBundle(id: string): Promise<void> {
  const token = window.sessionStorage.getItem("linkdesk_token");
  const response = await fetch(`${API_URL}/api/v1/workflows/${id}/bundle`, {
    headers: { authorization: `Bearer ${token ?? ""}` },
  });
  if (!response.ok) throw new Error("Review bundle is not available");
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `workflow-${id}.zip`;
  anchor.click();
  URL.revokeObjectURL(url);
}
