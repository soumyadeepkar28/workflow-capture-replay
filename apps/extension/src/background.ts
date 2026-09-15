import type {
  ActiveCapture,
  CapturedActionMessage,
  CaptureErrorMessage,
  ContentReadyMessage,
  DraftAction,
  ExternalMessage,
  PageIdentity,
} from './messages'

declare const __WORKFLOW_PORTAL_ORIGIN__: string

const ACTIVE_KEY = 'activeCapture'
const PORTAL_ORIGINS = new Set([
  'http://127.0.0.1:8000',
  __WORKFLOW_PORTAL_ORIGIN__,
])
const TARGET_ORIGIN = 'http://127.0.0.1:8765'
const GENERAL_PATH = /^\/tickets\/(?:\d+\/|submit\/)?$/
const LIST_QUERY_KEYS = new Set([
  'assigned_to', 'date_from', 'date_to', 'kbitem', 'priority', 'q', 'queue',
  'saved_query', 'search_type', 'sort', 'sortreverse', 'status',
])

let serializedMutation = Promise.resolve<unknown>(undefined)

function serialize<T>(operation: () => Promise<T>): Promise<T> {
  const next = serializedMutation.then(operation, operation)
  serializedMutation = next.catch(() => undefined)
  return next
}

async function loadActive(): Promise<ActiveCapture | null> {
  const value = await chrome.storage.local.get(ACTIVE_KEY)
  return (value[ACTIVE_KEY] as ActiveCapture | undefined) ?? null
}

async function saveActive(active: ActiveCapture): Promise<void> {
  await chrome.storage.local.set({ [ACTIVE_KEY]: active })
}

async function removeActive(): Promise<void> {
  await chrome.storage.local.remove(ACTIVE_KEY)
}

function exactPortalOrigin(sender: chrome.runtime.MessageSender): boolean {
  if (!sender.url) return false
  try {
    return PORTAL_ORIGINS.has(new URL(sender.url).origin)
  } catch {
    return false
  }
}

function generalPageIdentity(url: URL): PageIdentity | null {
  if (!GENERAL_PATH.test(url.pathname)) return null
  if (url.pathname !== '/tickets/') {
    return url.search ? null : { target_alias: 'helpdesk-demo', path: url.pathname }
  }
  const params = new URLSearchParams(url.search)
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
  if (count > 30 || csrfCount > 1 || unsupported) return null
  return {
    target_alias: 'helpdesk-demo',
    path: url.pathname,
    query: recordable.toString(),
  }
}

function targetPage(url: string | undefined): boolean {
  if (!url) return false
  try {
    const parsed = new URL(url)
    if (parsed.origin !== TARGET_ORIGIN) return false
    return generalPageIdentity(parsed) !== null
  } catch {
    return false
  }
}

async function reconcileStoredCapture(active: ActiveCapture): Promise<void> {
  if (!PORTAL_ORIGINS.has(active.apiOrigin)) {
    await removeActive()
    return
  }
  let response: Response
  try {
    response = await fetch(
      `${active.apiOrigin}/api/captures/${encodeURIComponent(active.draftId)}/status`,
      { headers: { Authorization: `Bearer ${active.capability}` } },
    )
  } catch (error) {
    throw new Error(
      `cannot verify the existing capture with its backend: ${error instanceof Error ? error.message : 'network error'}`,
    )
  }
  if (response.status === 401 || response.status === 404) {
    await removeActive()
    return
  }
  if (!response.ok) {
    throw new Error(`cannot verify the existing capture: backend returned ${response.status}`)
  }
  const payload = await response.json() as { status?: string }
  if (payload.status === 'active') throw new Error('another capture is already active')
  await removeActive()
}

async function uploadPending(active: ActiveCapture): Promise<ActiveCapture> {
  let current = active
  for (const batch of [...current.pendingBatches].sort((left, right) => left.batchIndex - right.batchIndex)) {
    const response = await fetch(
      `${current.apiOrigin}/api/captures/${encodeURIComponent(current.draftId)}/batches`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${current.capability}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          batch_id: batch.batchId,
          batch_index: batch.batchIndex,
          actions: batch.actions,
        }),
      },
    )
    if (!response.ok) throw new Error(`capture upload failed with status ${response.status}`)
    current = {
      ...current,
      pendingBatches: current.pendingBatches.filter((candidate) => candidate.batchId !== batch.batchId),
    }
    await saveActive(current)
  }
  return current
}

async function appendActionsToActive(
  active: ActiveCapture,
  actions: DraftAction[],
): Promise<ActiveCapture> {
  if (active.nextSequence + actions.length > 200) {
    throw new Error('capture reached the 200 action limit')
  }
  let updated = active
  for (const action of actions) {
    const completeAction = {
      ...action,
      action_id: `act_${crypto.randomUUID()}`,
      sequence: updated.nextSequence,
    } as ActiveCapture['pendingBatches'][number]['actions'][number]
    const batch = {
      batchId: `batch_${crypto.randomUUID()}`,
      batchIndex: updated.nextBatchIndex,
      actions: [completeAction],
    }
    updated = {
      ...updated,
      nextBatchIndex: updated.nextBatchIndex + 1,
      nextSequence: updated.nextSequence + 1,
      pendingBatches: [...updated.pendingBatches, batch],
    }
  }
  await saveActive(updated)
  return uploadPending(updated)
}

async function appendActions(actions: DraftAction[]): Promise<void> {
  await serialize(async () => {
    const active = await loadActive()
    if (!active) throw new Error('no active capture')
    await appendActionsToActive(active, actions)
  })
}

async function startCapture(message: Extract<ExternalMessage, { type: 'workflow:start-capture' }>) {
  if (!PORTAL_ORIGINS.has(message.apiOrigin)) throw new Error('API origin is not permitted')
  const existing = await loadActive()
  if (existing) await reconcileStoredCapture(existing)
  const tabs = await chrome.tabs.query({})
  const candidates = tabs.filter((tab) => targetPage(tab.url))
  if (candidates.length !== 1 || candidates[0]?.id === undefined) {
    throw new Error(`expected one supported Helpdesk ticket tab, found ${candidates.length}`)
  }
  const tabId = candidates[0].id
  const active: ActiveCapture = {
    apiOrigin: message.apiOrigin,
    capability: message.capability,
    draftId: message.draftId,
    tabId,
    startedAt: new Date().toISOString(),
    nextBatchIndex: 0,
    nextSequence: 0,
    documentIds: [],
    pendingBatches: [],
  }
  await saveActive(active)
  const response = await chrome.tabs.sendMessage(tabId, {
    type: 'workflow:begin-content-capture',
    initial: true,
  }) as { ok: boolean; page?: PageIdentity; error?: string }
  if (!response?.ok || !response.page) {
    await removeActive()
    throw new Error(response?.error ?? 'target content script did not accept capture')
  }
  const initialPage = response.page
  await serialize(async () => {
    const stored = await loadActive()
    if (!stored) throw new Error('capture disappeared during startup')
    const updated = await appendActionsToActive(
      { ...stored, lastPage: initialPage },
      [{ kind: 'navigate', destination: initialPage }],
    )
    await saveActive(updated)
  })
  return { draftId: message.draftId, tabId }
}

async function stopCapture(message: Extract<ExternalMessage, { type: 'workflow:stop-capture' }>) {
  const active = await loadActive()
  if (!active || active.draftId !== message.draftId) throw new Error('capture draft is not active')
  if (!active.captureError) {
    const contentResponse = await chrome.tabs.sendMessage(active.tabId, {
      type: 'workflow:end-content-capture',
    }) as { ok: boolean; error?: string }
    if (!contentResponse?.ok) throw new Error(contentResponse?.error ?? 'target did not stop cleanly')
  }

  return serialize(async () => {
    let current = await loadActive()
    if (!current) throw new Error('capture state disappeared before finalization')
    current = await uploadPending(current)
    if (current.captureError) {
      await fetch(
        `${current.apiOrigin}/api/captures/${encodeURIComponent(current.draftId)}/interrupt`,
        {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${current.capability}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ reason: current.captureError }),
        },
      ).then((response) => {
        if (!response.ok) throw new Error(`capture interruption failed with status ${response.status}`)
      })
      await removeActive()
      throw new Error(`Capture interrupted: ${current.captureError}`)
    }
    const response = await fetch(
      `${current.apiOrigin}/api/captures/${encodeURIComponent(current.draftId)}/finalize`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${current.capability}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          expected_batch_count: current.nextBatchIndex,
          expected_action_count: current.nextSequence,
          capture: {
            extension_version: chrome.runtime.getManifest().version,
            captured_at: current.startedAt,
            document_count: Math.max(1, current.documentIds.length),
            event_count: current.nextSequence,
            completeness: 'complete',
          },
        }),
      },
    )
    if (!response.ok) throw new Error(`capture finalization failed with status ${response.status}`)
    await removeActive()
    return { draftId: current.draftId, review: await response.json() }
  })
}

chrome.runtime.onMessageExternal.addListener((message: ExternalMessage, sender, sendResponse) => {
  if (!exactPortalOrigin(sender)) {
    sendResponse({ ok: false, error: 'portal origin is not permitted' })
    return
  }
  const operation = message.type === 'workflow:start-capture'
    ? startCapture(message)
    : message.type === 'workflow:stop-capture'
      ? stopCapture(message)
      : Promise.reject(new Error('unsupported external message'))
  void operation.then(
    (value) => sendResponse({ ok: true, ...value }),
    (error: unknown) => sendResponse({
      ok: false,
      error: error instanceof Error ? error.message : 'extension operation failed',
    }),
  )
  return true
})

chrome.runtime.onMessage.addListener(
  (message: ContentReadyMessage | CapturedActionMessage | CaptureErrorMessage, sender, sendResponse) => {
    if (message.type === 'workflow:capture-error') {
      void serialize(async () => {
        const active = await loadActive()
        if (!active || active.tabId !== sender.tab?.id) return
        await saveActive({ ...active, captureError: message.error.slice(0, 500) })
      })
      return
    }
    if (message.type === 'workflow:captured-actions') {
      if (!sender.tab?.id) {
        sendResponse({ ok: false, error: 'captured action has no source tab' })
        return
      }
      void loadActive().then(async (active) => {
        if (!active || active.tabId !== sender.tab?.id) throw new Error('action came from an unreserved tab')
        await appendActions(message.actions)
        sendResponse({ ok: true })
      }).catch((error: unknown) => sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : 'captured action failed',
      }))
      return true
    }

    if (message.type === 'workflow:content-ready' && sender.tab?.id !== undefined) {
      void serialize(async () => {
        const active = await loadActive()
        if (!active || active.tabId !== sender.tab?.id || !targetPage(sender.url)) return
        const documentId = sender.documentId ?? sender.url ?? 'unknown-document'
        let updated = active.documentIds.includes(documentId)
          ? active
          : { ...active, documentIds: [...active.documentIds, documentId] }
        await saveActive(updated)
        const response = await chrome.tabs.sendMessage(active.tabId, {
          type: 'workflow:begin-content-capture',
          initial: false,
        }) as { ok: boolean; page?: PageIdentity; error?: string }
        if (!response?.ok || !response.page) {
          throw new Error(response?.error ?? 'new target document did not accept capture')
        }
        const changedPage = !updated.lastPage
          || updated.lastPage.path !== response.page.path
          || (updated.lastPage.query ?? '') !== (response.page.query ?? '')
          || updated.lastPage.record_ref !== response.page.record_ref
        updated = { ...updated, lastPage: response.page }
        if (changedPage) {
          updated = await appendActionsToActive(
            updated,
            [{ kind: 'navigate', destination: response.page }],
          )
        }
        await saveActive(updated)
      })
    }
  },
)

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  const updatedUrl = changeInfo.url
  if (!updatedUrl) return
  void serialize(async () => {
    const active = await loadActive()
    if (!active || active.tabId !== tabId) return
    if (targetPage(updatedUrl)) {
      const identity = generalPageIdentity(new URL(updatedUrl))
      if (
        identity?.path !== '/tickets/'
        || active.lastPage?.path !== '/tickets/'
        || (active.lastPage.query ?? '') === (identity.query ?? '')
      ) return
      const updated = await appendActionsToActive(
        { ...active, lastPage: identity },
        [{ kind: 'navigate', destination: identity }],
      )
      await saveActive(updated)
      return
    }
    await saveActive({
      ...active,
      captureError: 'reserved capture tab left the supported Helpdesk staff ticket routes',
    })
  })
})

chrome.tabs.onRemoved.addListener((tabId) => {
  void serialize(async () => {
    const active = await loadActive()
    if (!active || active.tabId !== tabId) return
    await saveActive({ ...active, captureError: 'reserved capture tab was closed' })
  })
})