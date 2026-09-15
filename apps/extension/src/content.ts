import { computeAccessibleName } from 'dom-accessibility-api'

import type {
  BeginContentCaptureMessage,
  DraftAction,
  EndContentCaptureMessage,
  PageIdentity,
} from './messages'

const TARGET_ORIGIN = 'http://127.0.0.1:8765'
const GENERAL_PATH = /^\/tickets\/(?:\d+\/|submit\/)?$/
const LIST_QUERY_KEYS = new Set([
  'assigned_to', 'date_from', 'date_to', 'kbitem', 'priority', 'q', 'queue',
  'saved_query', 'search_type', 'sort', 'sortreverse', 'status',
])

let capturing = false
let pendingFill: { element: HTMLInputElement | HTMLTextAreaElement; value: string } | null = null

function normalizedText(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

function ticketListQuery(): string {
  if (!window.location.search) return ''
  const params = new URLSearchParams(window.location.search)
  const recordable = new URLSearchParams()
  let count = 0
  let csrfCount = 0
  let unsupported = false
  params.forEach((value, key) => {
    count += 1
    if (key === 'csrfmiddlewaretoken') {
      csrfCount += 1
      unsupported ||= value.length > 200
      return
    }
    unsupported ||= !LIST_QUERY_KEYS.has(key) || value.length > 200
    recordable.append(key, value)
  })
  if (count > 30 || csrfCount > 1 || unsupported) {
    throw new Error('ticket list contains unsupported search parameters')
  }
  return recordable.toString()
}

function pageIdentity(): PageIdentity {
  if (window.location.origin !== TARGET_ORIGIN || !GENERAL_PATH.test(window.location.pathname)) {
    throw new Error('capture page is outside the supported Helpdesk staff ticket routes')
  }
  if (window.location.pathname !== '/tickets/' && window.location.search) {
    throw new Error('search parameters are supported only on the Helpdesk ticket list')
  }
  const heading = normalizedText(document.querySelector('h2')?.textContent ?? '')
  const record_ref = heading.startsWith('Review access request #')
    ? 'review'
    : heading.startsWith('Complete access request #')
      ? 'completion'
      : undefined
  return {
    target_alias: 'helpdesk-demo',
    path: window.location.pathname,
    ...(window.location.pathname === '/tickets/' ? { query: ticketListQuery() } : {}),
    ...(record_ref ? { record_ref } : {}),
  }
}

function elementRole(element: Element): string | null {
  const explicit = element.getAttribute('role')
  if (explicit) return explicit
  if (element instanceof HTMLButtonElement) return 'button'
  if (element instanceof HTMLAnchorElement && element.href) return 'link'
  if (element instanceof HTMLInputElement && ['radio', 'checkbox'].includes(element.type)) {
    return element.type
  }
  return null
}

function fingerprint(element: Element) {
  const input = element instanceof HTMLInputElement ? element : null
  const field = element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement
    ? element
    : null
  return {
    tag: element.tagName.toLowerCase(),
    ...(input?.type ? { input_type: input.type } : {}),
    ...(element.id ? { element_id: element.id } : {}),
    ...(field?.name ? { field_name: field.name } : {}),
    ...(elementRole(element) ? { observed_role: elementRole(element)! } : {}),
  }
}

function labelText(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement): string | null {
  const label = element.labels?.[0]
  if (label) return normalizedText(label.textContent ?? '') || null
  const accessibleName = normalizedText(computeAccessibleName(element))
  if (accessibleName) return accessibleName
  if (element instanceof HTMLSelectElement && element.name === 'owner' && !element.id) {
    const summary = element.closest('.col')?.querySelector('small')
    const summaryText = normalizedText(summary?.textContent ?? '')
    if (summaryText === 'Assigned to') return summaryText
  }
  return null
}

function labelLocator(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement) {
  const label = labelText(element)
  if (!label) {
    const identity = `${element.tagName.toLowerCase()}[name="${element.name}"]`
    throw new Error(`supported field ${identity} has no exact associated label`)
  }
  return {
    strategy: 'label' as const,
    label,
    fallback: fingerprint(element),
  }
}

function roleLocator(element: Element) {
  const role = elementRole(element)
  if (!role || !['button', 'link', 'tab', 'menuitem', 'radio', 'checkbox'].includes(role)) {
    throw new Error('clicked element has no supported semantic role')
  }
  const name = normalizedText(computeAccessibleName(element))
  if (!name) throw new Error('clicked element has no accessible name')
  return {
    strategy: 'role' as const,
    role: role as 'button' | 'link' | 'tab' | 'menuitem' | 'radio' | 'checkbox',
    name,
    fallback: fingerprint(element),
  }
}

async function sendActions(actions: DraftAction[]): Promise<void> {
  if (actions.length === 0) return
  const response = await chrome.runtime.sendMessage({
    type: 'workflow:captured-actions',
    actions,
  }) as { ok: boolean; error?: string }
  if (!response?.ok) throw new Error(response?.error ?? 'background rejected captured actions')
}

function takePendingFill(): DraftAction | null {
  const pending = pendingFill
  pendingFill = null
  if (!pending) return null
  return {
    kind: 'fill',
    page: pageIdentity(),
    locator: labelLocator(pending.element),
    value: pending.value,
  }
}

async function flushPendingFill(): Promise<void> {
  const action = takePendingFill()
  await sendActions(action ? [action] : [])
}

function supportedClickTarget(target: EventTarget | null): Element | null {
  if (!(target instanceof Element)) return null
  const candidate = target.closest('button, a[href], [role="tab"], [role="menuitem"], input[type="radio"], input[type="checkbox"]')
  return candidate instanceof Element ? candidate : null
}

document.addEventListener('input', (event) => {
  if (!capturing) return
  const element = event.target
  if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)) return
  if (element instanceof HTMLInputElement && !['text', 'search', 'email', 'url', 'tel'].includes(element.type)) return
  if (pendingFill?.element !== element) {
    const prior = takePendingFill()
    if (prior) void sendActions([prior])
  }
  pendingFill = { element, value: element.value }
}, true)

document.addEventListener('change', (event) => {
  if (!capturing) return
  const element = event.target
  void (async () => {
    const actions: DraftAction[] = []
    const pending = takePendingFill()
    if (pending) actions.push(pending)
    if (element instanceof HTMLSelectElement) {
      const option = element.selectedOptions[0]
      if (!option) throw new Error('select change has no selected option')
      actions.push({
        kind: 'select',
        page: pageIdentity(),
        locator: labelLocator(element),
        option_label: normalizedText(option.label),
        observed_value: element.value,
        ...(['Owner', 'Assigned to'].includes(labelText(element) ?? '')
          && normalizedText(option.label) === 'workflow_actor'
          ? { fixture_ref: 'actor' as const }
          : {}),
      })
    } else if (
      element instanceof HTMLInputElement
      && ['radio', 'checkbox'].includes(element.type)
    ) {
      actions.push({
        kind: 'set_checked',
        page: pageIdentity(),
        locator: roleLocator(element),
        checked: element.checked,
      })
    }
    await sendActions(actions)
  })().catch((error: unknown) => {
    void chrome.runtime.sendMessage({
      type: 'workflow:capture-error',
      error: error instanceof Error ? error.message : 'unsupported change event',
    })
  })
}, true)

document.addEventListener('click', (event) => {
  if (!capturing) return
  const element = supportedClickTarget(event.target)
  if (!element || (element instanceof HTMLInputElement && ['radio', 'checkbox'].includes(element.type))) return
  void (async () => {
    const actions: DraftAction[] = []
    const pending = takePendingFill()
    if (pending) actions.push(pending)
    actions.push({
      kind: 'click',
      page: pageIdentity(),
      locator: roleLocator(element),
    })
    await sendActions(actions)
  })().catch((error: unknown) => {
    void chrome.runtime.sendMessage({
      type: 'workflow:capture-error',
      error: error instanceof Error ? error.message : 'unsupported click event',
    })
  })
}, true)

chrome.runtime.onMessage.addListener((message: BeginContentCaptureMessage | EndContentCaptureMessage, _sender, sendResponse) => {
  if (message.type === 'workflow:begin-content-capture') {
    try {
      capturing = true
      sendResponse({ ok: true, page: pageIdentity() })
    } catch (error) {
      capturing = false
      sendResponse({ ok: false, error: error instanceof Error ? error.message : 'capture cannot start' })
    }
    return
  }
  if (message.type === 'workflow:end-content-capture') {
    void flushPendingFill().then(
      () => {
        capturing = false
        sendResponse({ ok: true })
      },
      (error: unknown) => sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : 'capture cannot stop cleanly',
      }),
    )
    return true
  }
})

if (window.location.origin === TARGET_ORIGIN) {
  void chrome.runtime.sendMessage({ type: 'workflow:content-ready' })
}