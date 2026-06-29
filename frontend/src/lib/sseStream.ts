// SSE streaming helper for batch operations.
// POSTs a JSON body (or FormData) to an SSE endpoint and invokes onMessage
// for every `data:` frame received, enabling real-time progress UI.

export interface SSEMessage {
  type?: string;
  current?: number;
  total?: number;
  email?: string;
  step?: string;
  message?: string;
  success_count?: number;
  failed_count?: number;
  failures?: { email: string; reason: string }[];
}

function getToken(): string {
  const authData = localStorage.getItem('kiro-auth');
  if (authData) {
    try {
      return JSON.parse(authData).state?.accessToken || '';
    } catch {
      return '';
    }
  }
  return '';
}

/**
 * Consume a Server-Sent-Events stream from a POST endpoint.
 *
 * @param url      Full request URL (e.g. `/api/accounts/1/batch/delete/stream`).
 * @param body     JSON-serializable payload, or a FormData instance.
 * @param onMessage Called for each parsed SSE data frame.
 */
export async function postSSEStream(
  url: string,
  body: Record<string, unknown> | FormData,
  onMessage: (msg: SSEMessage) => void,
): Promise<void> {
  const token = getToken();
  const isFormData = body instanceof FormData;

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
    },
    body: isFormData ? body : JSON.stringify(body),
  });

  if (!response.ok || !response.body) {
    const text = await response.text().catch(() => '');
    throw new Error(text || `请求失败 (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          onMessage(JSON.parse(line.slice(6)) as SSEMessage);
        } catch {
          // ignore malformed frame
        }
      }
    }
  }
}
