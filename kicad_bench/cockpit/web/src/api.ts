let mutationToken = ''

export function setMutationToken(token: string) { mutationToken = token }

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const method = (init?.method || 'GET').toUpperCase()
  const headers = new Headers(init?.headers)
  if (!['GET', 'HEAD'].includes(method) && mutationToken) headers.set('X-Cockpit-Token', mutationToken)
  const response = await fetch(url, { ...init, headers })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const data = await response.json()
      detail = data.detail || detail
    } catch { /* non-JSON response */ }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const getJSON = <T>(url: string) => request<T>(url)
export const postJSON = <T>(url: string, body?: unknown) => request<T>(url, {
  method: 'POST',
  headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
})
export const deleteJSON = <T>(url: string) => request<T>(url, { method: 'DELETE' })
export const boardUrl = (board: string, path = '') =>
  `/api/boards/${encodeURIComponent(board)}${path ? `/${path}` : ''}`

export type Board = {
  id: string
  name: string
  schematic: string | null
  pcb: string | null
  has_schematic: boolean
  has_pcb: boolean
  pcb_mtime: number
  sch_mtime: number
}

export type LiveStatus = {
  board: string
  pcb_mtime: number
  sch_mtime: number
  audit: Record<string, any>
  review: Record<string, any>
  stage: Record<string, any>
  git: Record<string, any>
  release: Record<string, any>
}
