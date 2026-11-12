// Centralised env access. Vite exposes only `VITE_`-prefixed vars.
export const apiBase = (import.meta.env.VITE_API_BASE_URL ?? '/api').replace(/\/$/, '')

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, body: unknown) {
    let message = 'API error'
    if (typeof body === 'string') message = body
    else if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail?: unknown }).detail
      if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail)) message = detail.map((d) => String(d)).join('; ')
    }
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

type FetchOptions = Omit<RequestInit, 'body'> & {
  body?: unknown
  /** Accepted for API parity with callers that must never navigate away
   *  mid-capture. This client never hard-navigates; `RequireAuth` owns
   *  re-auth. */
  skipAuthRedirect?: boolean
}

/**
 * Fetch wrapper that:
 *   - prepends VITE_API_BASE_URL (defaults to `/api`)
 *   - sends cookies (`credentials: include`) so session cookies stick
 *   - JSON-encodes the request body unless it's already FormData / Blob
 *   - parses JSON or text on response
 *   - throws ApiError on non-2xx
 */
export async function fetchApi<T = unknown>(
  path: string,
  options: FetchOptions = {},
): Promise<T> {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- stripped, see FetchOptions
  const { body, headers, skipAuthRedirect: _skip, ...rest } = options

  const isFormData = body instanceof FormData
  const isBlob = body instanceof Blob

  const finalHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...((headers as Record<string, string> | undefined) ?? {}),
  }
  if (!isFormData && !isBlob && body !== undefined) {
    finalHeaders['Content-Type'] = 'application/json'
  }

  const finalBody: BodyInit | undefined =
    body === undefined
      ? undefined
      : isFormData || isBlob
        ? (body as BodyInit)
        : JSON.stringify(body)

  const res = await fetch(`${apiBase}${path}`, {
    credentials: 'include',
    headers: finalHeaders,
    body: finalBody,
    ...rest,
  })

  if (res.status === 204) return undefined as T

  const contentType = res.headers.get('content-type') ?? ''
  const payload: unknown = contentType.includes('application/json')
    ? await res.json().catch(() => undefined)
    : await res.text()

  if (!res.ok) throw new ApiError(res.status, payload)

  // An HTML page where JSON was expected is the SSO login page (the fetch
  // followed oauth2-proxy's redirect): surface it as an auth error instead
  // of handing a string to callers that read fields off objects.
  if (!contentType.includes('application/json') && contentType.includes('text/html')) {
    throw new ApiError(res.status, payload)
  }

  return payload as T
}
