import {
  Activity,
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  Library,
  Link2,
  MonitorCheck,
  Plus,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import './App.css'

type Readiness = 'offline' | 'ready' | 'busy' | 'recovery_required'
type WorkflowMode = 'verified_preset' | 'general'

interface RunnerSummary {
  runner_id: string
  label: string
  readiness: Readiness
  last_seen_at: number | null
  companion_version: string
  protocol_version: number
}

interface PortalSession {
  csrf_token: string
  expires_at: number
  runner: RunnerSummary | null
}

interface PairingPreview {
  pairing_id: string
  label: string
  companion_version: string
  expires_at: number
}

interface WorkflowSummary {
  workflow_id: string
  name: string
  description: string
  action_count: number
  target_alias: 'helpdesk-demo'
  workflow_mode: WorkflowMode
  category_id: string
  category: string
  created_at: number
  latest_outcome: null
}

interface RunSummary {
  run_id: string
  workflow_id: string
  workflow_name: string
  workflow_mode: WorkflowMode
  category: string
  phase: 'pending' | 'claimed' | 'accepted' | 'preparing' | 'running' | 'verifying' | 'finished' | 'interrupted' | 'cancelled' | 'rejected' | 'expired'
  outcome: 'succeeded' | 'partially_succeeded' | 'failed' | 'uncertain' | null
  outcome_summary: string | null
  created_at: number
  claimed_at: number | null
  accepted_at: number | null
  started_at: number | null
  finished_at: number | null
  last_event_sequence: number
}

interface RunEventEnvelope {
  event_id: string
  sequence: number
  event: {
    kind: 'phase' | 'action' | 'check' | 'outcome'
    observed_at: string
    phase?: string
    action_id?: string
    action_sequence?: number
    stage?: 'intent' | 'result'
    status?: 'pending' | 'completed' | 'failed' | 'uncertain' | 'passed' | 'unknown' | 'not_evaluated'
    locator_used?: string | null
    error_message?: string | null
    check_id?: string
    expected?: string
    actual?: string
    critical?: boolean
    outcome?: string
    summary?: string
  }
}

interface RunArtifact {
  artifact_id: string
  kind: 'baseline' | 'review_checkpoint' | 'final' | 'failure'
  sha256: string
  size_bytes: number
  content_type: 'image/png'
  url: string
}

interface RunDetail {
  run: RunSummary
  events: RunEventEnvelope[]
  artifacts: RunArtifact[]
}

interface CaptureDraft {
  draft_id: string
  upload_capability: string
  expires_at: number
  status: 'active'
}

interface CapturedAction {
  action_id: string
  sequence: number
  kind: 'navigate' | 'reload' | 'click' | 'fill' | 'select' | 'set_checked'
  [key: string]: unknown
}

interface CaptureReview {
  draft_id: string
  status: 'review'
  actions: CapturedAction[]
}

interface HelpdeskVerificationProfile {
  profile_id: 'helpdesk-linked-tickets-v1'
  version: 1
  content_hash: string
  review_note: 'Demo review completed.'
  completion_note: 'Demo completion recorded.'
  actor_username: 'workflow_actor'
}

interface WorkflowCategory {
  category_id: string
  name: string
  verification_profile: HelpdeskVerificationProfile | null
  built_in: boolean
  created_at: number
}

interface ExtensionResponse {
  ok: boolean
  error?: string
  review?: CaptureReview
}

const EXTENSION_ID = 'ooocecppjnccilieepgobfjnlhhmebdk'

class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message)
  }
}

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  csrfToken?: string,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
      ...init.headers,
    },
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as
      | { detail?: { code?: string; message?: string } | string }
      | null
    const detail = payload?.detail
    const code = typeof detail === 'string' ? undefined : detail?.code
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message ?? `Request failed with status ${response.status}.`
    throw new ApiError(message, response.status, code)
  }
  return response.json() as Promise<T>
}

function sendExtensionMessage(message: unknown): Promise<ExtensionResponse> {
  const runtime = typeof chrome === 'undefined' ? undefined : chrome.runtime
  if (!runtime) {
    return Promise.reject(new Error('The Workflow Replay extension is not installed in this Chrome profile.'))
  }
  return new Promise((resolve, reject) => {
    let settled = false
    const timeout = window.setTimeout(() => {
      settled = true
      reject(new Error('The extension did not acknowledge capture startup within 10 seconds.'))
    }, 10_000)
    runtime.sendMessage(EXTENSION_ID, message, (response?: ExtensionResponse) => {
      if (settled) return
      settled = true
      window.clearTimeout(timeout)
      const runtimeError = runtime.lastError?.message
      if (runtimeError) {
        reject(new Error(runtimeError))
      } else if (!response?.ok) {
        reject(new Error(response?.error ?? 'The extension rejected the capture request.'))
      } else {
        resolve(response)
      }
    })
  })
}

function usePortalSession() {
  const [session, setSession] = useState<PortalSession | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    try {
      setError(null)
      setSession(await requestJson<PortalSession>('/api/session'))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Unable to load the portal session.')
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  return { error, refresh, session }
}

function ReadinessMark({ readiness }: { readiness: Readiness }) {
  const ready = readiness === 'ready'
  const Icon = ready ? CheckCircle2 : readiness === 'recovery_required' ? CircleAlert : Activity
  return (
    <span className={`status status--${readiness}`}>
      <Icon aria-hidden="true" size={15} strokeWidth={2} />
      {ready ? 'Ready' : readiness === 'recovery_required' ? 'Recovery required' : readiness}
    </span>
  )
}

function Shell({ children, runner }: { children: React.ReactNode; runner: RunnerSummary | null }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Workflow Replay home">
          <span className="brand__mark"><Activity aria-hidden="true" size={19} /></span>
          <span>Workflow Replay</span>
        </a>
        <div className="topbar__runner">
          <span className="eyebrow">Local runner</span>
          {runner ? <ReadinessMark readiness={runner.readiness} /> : <span className="status status--offline">Not connected</span>}
        </div>
      </header>
      {children}
    </div>
  )
}

function actionLabel(action: CapturedAction): string {
  if (action.kind === 'navigate') return 'Open supported ticket page'
  const locator = action.locator as { label?: string; name?: string } | undefined
  const target = locator?.label ?? locator?.name ?? 'supported control'
  if (action.kind === 'fill') return `Fill ${target}`
  if (action.kind === 'select') return `Select ${(action.option_label as string | undefined) ?? 'option'} in ${target}`
  if (action.kind === 'set_checked') return `${action.checked ? 'Set' : 'Clear'} ${target}`
  if (action.kind === 'reload') return 'Reload current ticket'
  return `Click ${target}`
}

function runPresentation(run: RunSummary): { label: string; tone: string; summary: string } {
  if (run.workflow_mode === 'general') {
    const phase = run.phase.replace(/_/g, ' ')
    return {
      label: `${phase} · unverified`,
      tone: run.phase === 'finished' ? 'unverified' : run.phase,
      summary: run.phase === 'finished'
        ? 'All recorded actions completed. No business outcome verifier is attached.'
        : run.phase === 'interrupted'
          ? 'Execution stopped before every action completed. No business outcome was evaluated.'
          : 'Execution evidence is still being collected; no business outcome will be evaluated.',
    }
  }
  const value = run.outcome ?? run.phase
  return {
    label: value.replace(/_/g, ' '),
    tone: value,
    summary: run.outcome_summary ?? 'Waiting for local execution evidence.',
  }
}

function capturedFill(
  actions: CapturedAction[],
  recordRef: 'review' | 'completion',
): string | undefined {
  return actions
    .filter((action) => {
      const page = action.page as { record_ref?: string } | undefined
      const locator = action.locator as { label?: string } | undefined
      return action.kind === 'fill'
        && page?.record_ref === recordRef
        && locator?.label === 'Comment / Resolution'
    })
    .at(-1)?.value as string | undefined
}

function CaptureReviewPanel({
  review,
  session,
  onSaved,
}: {
  review: CaptureReview
  session: PortalSession
  onSaved: () => Promise<void>
}) {
  const [name, setName] = useState('Helpdesk workflow')
  const [description, setDescription] = useState('')
  const [categories, setCategories] = useState<WorkflowCategory[]>([])
  const [categoryChoice, setCategoryChoice] = useState('')
  const [newCategoryName, setNewCategoryName] = useState('')
  const [loadingCategories, setLoadingCategories] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const creatingCategory = categoryChoice === '__new__'
  const selectedCategory = categories.find(
    (candidate) => candidate.category_id === categoryChoice,
  )
  const selectedProfile = selectedCategory?.verification_profile ?? null
  const categoryReady = creatingCategory
    ? Boolean(newCategoryName.trim())
    : Boolean(selectedCategory)
  const recordedReviewNote = capturedFill(review.actions, 'review')
  const recordedCompletionNote = capturedFill(review.actions, 'completion')
  const verificationMismatches = [
    selectedProfile && recordedReviewNote !== selectedProfile.review_note ? 'Review note' : null,
    selectedProfile && recordedCompletionNote !== selectedProfile.completion_note ? 'Completion note' : null,
  ].filter((value): value is string => value !== null)

  useEffect(() => {
    let cancelled = false
    void requestJson<{ categories: WorkflowCategory[] }>('/api/workflow-categories')
      .then((payload) => {
        if (!cancelled) setCategories(payload.categories)
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : 'Unable to load workflow categories.')
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingCategories(false)
      })
    return () => { cancelled = true }
  }, [])

  async function save() {
    if (!categoryReady) return
    setSaving(true)
    setError(null)
    try {
      await requestJson(
        `/api/captures/${encodeURIComponent(review.draft_id)}/save`,
        {
          method: 'POST',
          body: JSON.stringify({
            name,
            description,
            ...(creatingCategory
              ? { new_category_name: newCategoryName }
              : { category_id: selectedCategory?.category_id }),
          }),
        },
        session.csrf_token,
      )
      await onSaved()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Unable to save this workflow.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="capture-review" aria-labelledby="capture-review-title">
      <div className="capture-review__heading">
        <div>
          <p className="eyebrow">Capture complete</p>
          <h2 id="capture-review-title">Review recorded actions</h2>
          <p>{review.actions.length} structured actions are ready to classify and save. Actions cannot be edited in this delivery.</p>
        </div>
        <CheckCircle2 aria-hidden="true" size={23} />
      </div>
      <ol className="action-list">
        {review.actions.map((action) => (
          <li key={action.action_id}>
            <span>{action.sequence + 1}</span>
            <div><strong>{actionLabel(action)}</strong><code>{action.kind}</code></div>
          </li>
        ))}
      </ol>
      <div className="category-assignment">
        <label>Workflow category
          <select value={categoryChoice} disabled={loadingCategories || saving} onChange={(event) => setCategoryChoice(event.target.value)}>
            <option value="" disabled>{loadingCategories ? 'Loading categories…' : 'Choose a category'}</option>
            {categories.map((category) => (
              <option key={category.category_id} value={category.category_id}>
                {category.name} · {category.verification_profile ? 'Verified' : 'Unverified'}
              </option>
            ))}
            <option value="__new__">Create new category…</option>
          </select>
        </label>
        {creatingCategory && (
          <label>New category name<input value={newCategoryName} maxLength={80} disabled={saving} onChange={(event) => setNewCategoryName(event.target.value)} /></label>
        )}
      </div>
      {selectedProfile ? <div className="verification-contract">
        <div>
          <p className="eyebrow">{selectedCategory?.name} · Verified</p>
          <h3>Expected replay values</h3>
        </div>
        <dl>
          <div><dt>Review note</dt><dd><code>{selectedProfile.review_note}</code><span>Recorded: {recordedReviewNote ?? 'missing'}</span></dd></div>
          <div><dt>Completion note</dt><dd><code>{selectedProfile.completion_note}</code><span>Recorded: {recordedCompletionNote ?? 'missing'}</span></dd></div>
          <div><dt>Completion owner</dt><dd><code>{selectedProfile.actor_username}</code></dd></div>
        </dl>
        {verificationMismatches.length > 0 && (
          <p className="verification-warning" role="alert">
            {verificationMismatches.join(' and ')} do not match the fixed verifier. This workflow can be saved, but replay is expected to fail.
          </p>
        )}
      </div> : categoryReady && <div className="verification-contract verification-contract--unverified">
        <div>
          <p className="eyebrow">{creatingCategory ? 'New category' : selectedCategory?.name} · Unverified</p>
          <h3>Execution evidence only</h3>
        </div>
        <p className="verification-contract__copy">Replay will report whether every recorded action finished and preserve screenshots and action results. It will not claim that a business outcome succeeded.</p>
      </div>}
      <div className="metadata-form">
        <label>Workflow name<input value={name} maxLength={120} onChange={(event) => setName(event.target.value)} /></label>
        <label>Description<textarea value={description} maxLength={500} rows={3} onChange={(event) => setDescription(event.target.value)} /></label>
      </div>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <button className="button button--primary" type="button" disabled={saving || loadingCategories || !name.trim() || !categoryReady} onClick={() => void save()}>
        <Library aria-hidden="true" size={17} />
        {saving ? 'Saving…' : 'Save workflow'}
      </button>
    </section>
  )
}

function RunDetailView({
  detail,
  onBack,
  runner,
}: {
  detail: RunDetail
  onBack: () => void
  runner: RunnerSummary | null
}) {
  const checks = detail.events.filter((item) => item.event.kind === 'check')
  const actions = detail.events.filter(
    (item) => item.event.kind === 'action' && item.event.stage === 'result',
  )
  const presentation = runPresentation(detail.run)
  return (
    <Shell runner={runner}>
      <main className="page">
        <button className="back-button" type="button" onClick={onBack}>← Replay history</button>
        <div className="run-detail-heading">
          <div>
            <p className="eyebrow">{detail.run.category} · Replay evidence</p>
            <h1>{detail.run.workflow_name}</h1>
            <p>{presentation.summary}</p>
          </div>
          <span className={`run-outcome run-outcome--${presentation.tone}`}>
            {presentation.label}
          </span>
        </div>

        {detail.run.workflow_mode === 'verified_preset' ? <section className="evidence-section" aria-labelledby="checks-title">
          <div className="section-heading"><h2 id="checks-title">Outcome checks</h2><span>{checks.length} observations</span></div>
          <div className="check-rows">
            {checks.map(({ event, event_id: eventId }) => (
              <article className="check-row" key={eventId}>
                <span className={`check-mark check-mark--${event.status}`}>
                  {event.status === 'passed' ? <CheckCircle2 aria-hidden="true" size={17} /> : <CircleAlert aria-hidden="true" size={17} />}
                </span>
                <div><h3>{event.check_id?.replace(/_/g, ' ')}</h3><p>{event.expected}</p><code>{event.actual}</code></div>
                {event.critical && <span className="critical-label">Critical</span>}
              </article>
            ))}
          </div>
        </section> : <section className="evidence-section" aria-labelledby="checks-title">
          <div className="section-heading"><h2 id="checks-title">Business verification</h2><span>Unverified</span></div>
          <p className="run-history__empty">This general workflow has no business outcome profile. Inspect action results and screenshots as execution evidence.</p>
        </section>}

        <section className="evidence-section" aria-labelledby="actions-title">
          <div className="section-heading"><h2 id="actions-title">Executed actions</h2><span>{actions.length} results</span></div>
          <div className="compact-events">
            {actions.map(({ event, event_id: eventId }) => (
              <div key={eventId}><span>Step {(event.action_sequence ?? 0) + 1}</span><strong>{event.status}</strong><code>{event.locator_used ?? event.error_message ?? 'No locator detail'}</code></div>
            ))}
          </div>
        </section>

        <section className="evidence-section" aria-labelledby="images-title">
          <div className="section-heading"><h2 id="images-title">Screenshots</h2><span>{detail.artifacts.length} synchronized files</span></div>
          {detail.artifacts.length === 0 ? <p className="run-history__empty">No synchronized imagery is available.</p> : (
            <div className="evidence-grid">
              {detail.artifacts.map((artifact) => (
                <figure key={artifact.artifact_id}>
                  <a href={artifact.url} target="_blank" rel="noreferrer"><img src={artifact.url} alt={`${artifact.kind.replace(/_/g, ' ')} replay evidence`} /></a>
                  <figcaption><strong>{artifact.kind.replace(/_/g, ' ')}</strong><span>{Math.ceil(artifact.size_bytes / 1024)} KiB</span></figcaption>
                </figure>
              ))}
            </div>
          )}
        </section>
      </main>
    </Shell>
  )
}

function RunComparisonView({
  newer,
  older,
  onBack,
  runner,
}: {
  newer: RunDetail
  older: RunDetail
  onBack: () => void
  runner: RunnerSummary | null
}) {
  const checks = new Map<string, { older?: RunEventEnvelope['event']; newer?: RunEventEnvelope['event'] }>()
  const actions = new Map<number, { older?: RunEventEnvelope['event']; newer?: RunEventEnvelope['event'] }>()
  for (const [side, detail] of [['older', older], ['newer', newer]] as const) {
    for (const envelope of detail.events) {
      const event = envelope.event
      if (event.kind === 'check' && event.check_id) {
        checks.set(event.check_id, { ...checks.get(event.check_id), [side]: event })
      }
      if (event.kind === 'action' && event.stage === 'result' && event.action_sequence !== undefined) {
        actions.set(event.action_sequence, { ...actions.get(event.action_sequence), [side]: event })
      }
    }
  }
  const olderPresentation = runPresentation(older.run)
  const newerPresentation = runPresentation(newer.run)

  return (
    <Shell runner={runner}>
      <main className="page">
        <button className="back-button" type="button" onClick={onBack}>← Workflow library</button>
        <div className="run-detail-heading">
          <div><p className="eyebrow">Execution comparison</p><h1>{newer.run.workflow_name}</h1><p>Expected workflow steps stay fixed; observed execution and outcome evidence can differ by run.</p></div>
        </div>
        <div className="comparison-head" aria-label="Compared runs">
          <div />
          <div><span>Earlier attempt</span><strong>{olderPresentation.label}</strong><time>{new Date(older.run.created_at * 1000).toLocaleString()}</time></div>
          <div><span>Later attempt</span><strong>{newerPresentation.label}</strong><time>{new Date(newer.run.created_at * 1000).toLocaleString()}</time></div>
        </div>

        {newer.run.workflow_mode === 'verified_preset' && <section className="comparison-section" aria-labelledby="compare-checks-title">
          <h2 id="compare-checks-title">Outcome checks</h2>
          {[...checks.entries()].map(([checkId, pair]) => (
            <div className="comparison-row" key={checkId}>
              <div><strong>{checkId.replace(/_/g, ' ')}</strong><span>Business observation</span></div>
              <ComparisonCell event={pair.older} />
              <ComparisonCell event={pair.newer} />
            </div>
          ))}
        </section>}

        <section className="comparison-section" aria-labelledby="compare-actions-title">
          <h2 id="compare-actions-title">Action results</h2>
          {[...actions.entries()].sort(([left], [right]) => left - right).map(([sequence, pair]) => (
            <div className="comparison-row" key={sequence}>
              <div><strong>Step {sequence + 1}</strong><span>Stable captured sequence</span></div>
              <ComparisonCell event={pair.older} />
              <ComparisonCell event={pair.newer} />
            </div>
          ))}
        </section>

        <section className="comparison-section" aria-labelledby="compare-images-title">
          <h2 id="compare-images-title">Final evidence</h2>
          <div className="comparison-row comparison-row--images">
            <div><strong>Final checkpoint</strong><span>Synchronized browser image</span></div>
            <ComparisonImage detail={older} />
            <ComparisonImage detail={newer} />
          </div>
        </section>
      </main>
    </Shell>
  )
}

function ComparisonCell({ event }: { event?: RunEventEnvelope['event'] }) {
  if (!event) return <div className="comparison-cell comparison-cell--missing">Not observed</div>
  const status = event.status ?? event.outcome ?? event.phase ?? 'recorded'
  return (
    <div className="comparison-cell">
      <strong>{status.replace(/_/g, ' ')}</strong>
      <code>{event.actual ?? event.locator_used ?? event.error_message ?? event.summary ?? 'No additional detail'}</code>
    </div>
  )
}

function ComparisonImage({ detail }: { detail: RunDetail }) {
  const artifact = detail.artifacts.find((candidate) => candidate.kind === 'final')
  return artifact ? (
    <a className="comparison-image" href={artifact.url} target="_blank" rel="noreferrer"><img src={artifact.url} alt={`${detail.run.outcome ?? detail.run.phase} final checkpoint`} /></a>
  ) : <div className="comparison-cell comparison-cell--missing">No final image</div>
}

function LibraryView({
  session,
  onRefresh,
  onWorkflowsChanged,
  workflows,
  runs,
  onReplay,
}: {
  session: PortalSession
  onRefresh: () => Promise<void>
  onWorkflowsChanged: () => Promise<void>
  workflows: WorkflowSummary[]
  runs: RunSummary[]
  onReplay: (workflowId: string) => Promise<void>
}) {
  const [activeDraft, setActiveDraft] = useState<CaptureDraft | null>(null)
  const [review, setReview] = useState<CaptureReview | null>(null)
  const [captureError, setCaptureError] = useState<string | null>(null)
  const [recoverableCapture, setRecoverableCapture] = useState(false)
  const [startingCapture, setStartingCapture] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [requestingWorkflow, setRequestingWorkflow] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<RunDetail | null>(null)
  const [comparison, setComparison] = useState<{ newer: RunDetail; older: RunDetail } | null>(null)
  const workflowGroups = workflows.reduce<Map<string, WorkflowSummary[]>>((groups, workflow) => {
    const group = groups.get(workflow.category) ?? []
    group.push(workflow)
    groups.set(workflow.category, group)
    return groups
  }, new Map())

  async function startCapture() {
    setCaptureError(null)
    setRecoverableCapture(false)
    setStartingCapture(true)
    let draft: CaptureDraft | null = null
    try {
      draft = await requestJson<CaptureDraft>(
        '/api/captures',
        { method: 'POST' },
        session.csrf_token,
      )
      await sendExtensionMessage({
        type: 'workflow:start-capture',
        apiOrigin: window.location.origin,
        capability: draft.upload_capability,
        draftId: draft.draft_id,
      })
      setActiveDraft(draft)
    } catch (caught) {
      if (draft) {
        try {
          await requestJson(
            `/api/captures/${encodeURIComponent(draft.draft_id)}/interrupt`,
            {
              method: 'POST',
              headers: { Authorization: `Bearer ${draft.upload_capability}` },
              body: JSON.stringify({ reason: 'extension did not accept capture startup' }),
            },
          )
        } catch {
          // Preserve the original startup error; expired drafts are reclaimed centrally.
        }
      }
      setRecoverableCapture(
        caught instanceof ApiError && caught.code === 'capture_already_active',
      )
      setCaptureError(caught instanceof Error ? caught.message : 'Unable to start capture.')
    } finally {
      setStartingCapture(false)
    }
  }

  async function recoverCapture() {
    setCaptureError(null)
    try {
      await requestJson(
        '/api/capture-recovery',
        { method: 'POST' },
        session.csrf_token,
      )
      setRecoverableCapture(false)
      await startCapture()
    } catch (caught) {
      setCaptureError(caught instanceof Error ? caught.message : 'Unable to recover the stale capture.')
    }
  }

  async function stopCapture() {
    if (!activeDraft) return
    setStopping(true)
    setCaptureError(null)
    try {
      const response = await sendExtensionMessage({
        type: 'workflow:stop-capture',
        draftId: activeDraft.draft_id,
      })
      if (!response.review) throw new Error('The extension stopped without a review payload.')
      setReview(response.review)
      setActiveDraft(null)
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : 'Unable to stop capture cleanly.'
      setCaptureError(message)
      if (message.startsWith('Capture interrupted:')) setActiveDraft(null)
    } finally {
      setStopping(false)
    }
  }

  async function replay(workflowId: string) {
    setRequestingWorkflow(workflowId)
    setCaptureError(null)
    try {
      await onReplay(workflowId)
    } catch (caught) {
      setCaptureError(caught instanceof Error ? caught.message : 'Unable to request replay.')
    } finally {
      setRequestingWorkflow(null)
    }
  }

  async function inspectRun(runId: string) {
    setCaptureError(null)
    try {
      setSelectedRun(await requestJson<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`))
    } catch (caught) {
      setCaptureError(caught instanceof Error ? caught.message : 'Unable to load run evidence.')
    }
  }

  async function compareRuns(workflowId: string) {
    const candidates = runs.filter((run) => run.workflow_id === workflowId).slice(0, 2)
    if (candidates.length !== 2) return
    setCaptureError(null)
    try {
      const [newer, older] = await Promise.all(
        candidates.map((run) => requestJson<RunDetail>(`/api/runs/${encodeURIComponent(run.run_id)}`)),
      )
      setComparison({ newer, older })
    } catch (caught) {
      setCaptureError(caught instanceof Error ? caught.message : 'Unable to compare these runs.')
    }
  }

  if (review) {
    return (
      <Shell runner={session.runner}>
        <main className="page page--narrow">
          <CaptureReviewPanel
            review={review}
            session={session}
            onSaved={async () => {
              await onWorkflowsChanged()
              setReview(null)
            }}
          />
        </main>
      </Shell>
    )
  }
  if (selectedRun) {
    return <RunDetailView detail={selectedRun} runner={session.runner} onBack={() => setSelectedRun(null)} />
  }
  if (comparison) {
    return <RunComparisonView newer={comparison.newer} older={comparison.older} runner={session.runner} onBack={() => setComparison(null)} />
  }

  return (
    <Shell runner={session.runner}>
      <main className="page">
        <div className="page-heading">
          <div>
            <p className="eyebrow">Saved automation</p>
            <h1>Workflow library</h1>
            <p className="page-heading__copy">Capture a browser task once, then inspect every real replay attempt.</p>
          </div>
          <button className="button button--primary" type="button" onClick={() => void startCapture()} disabled={startingCapture || Boolean(activeDraft) || !session.runner || session.runner.readiness !== 'ready'}>
            <Plus aria-hidden="true" size={17} />
            {startingCapture ? 'Starting capture…' : 'Capture workflow'}
          </button>
        </div>

        {activeDraft && (
          <section className="recording-strip" aria-live="polite">
            <span className="recording-dot" />
            <div><strong>Recording Helpdesk workflow</strong><p>Perform the workflow in the reserved Helpdesk tab, then return here.</p></div>
            <button className="button button--secondary" type="button" disabled={stopping} onClick={() => void stopCapture()}>
              {stopping ? 'Stopping…' : 'Stop capture'}
            </button>
          </section>
        )}
        {captureError && <p className="inline-error" role="alert">{captureError}</p>}
        {recoverableCapture && (
          <button className="button button--secondary" type="button" onClick={() => void recoverCapture()}>
            Discard stale capture and retry
          </button>
        )}

        <section className="readiness-band" aria-labelledby="readiness-title">
          <div className="readiness-band__title">
            <MonitorCheck aria-hidden="true" size={21} />
            <div>
              <h2 id="readiness-title">Execution environment</h2>
              <p>The browser work stays on this device; shared history lives in the portal.</p>
            </div>
          </div>
          <dl className="readiness-grid">
            <div>
              <dt>Runner</dt>
              <dd>{session.runner?.label ?? 'No runner paired'}</dd>
            </div>
            <div>
              <dt>Target</dt>
              <dd>Django Helpdesk · local</dd>
            </div>
            <div>
              <dt>Protocol</dt>
              <dd>{session.runner ? `v${session.runner.protocol_version}` : 'Unavailable'}</dd>
            </div>
          </dl>
          <button className="icon-button" type="button" onClick={() => void onRefresh()} title="Refresh runner status" aria-label="Refresh runner status">
            <RefreshCw aria-hidden="true" size={17} />
          </button>
        </section>

        {!session.runner && (
          <section className="notice" aria-labelledby="connect-title">
            <Link2 aria-hidden="true" size={20} />
            <div>
              <h2 id="connect-title">Connect the local runner</h2>
              <p>Run the companion pairing command on this device. It opens a one-time confirmation page here.</p>
            </div>
          </section>
        )}

        <section className="library-table" aria-labelledby="library-title">
          <div className="library-table__header">
            <div>
              <h2 id="library-title">All workflows</h2>
              <p>{workflows.length} saved {workflows.length === 1 ? 'workflow' : 'workflows'}</p>
            </div>
          </div>
          {workflows.length === 0 ? (
            <div className="empty-state">
              <span className="empty-state__icon"><Library aria-hidden="true" size={24} /></span>
              <h3>No workflows recorded</h3>
              <p>Once the runner and extension are ready, your first captured Helpdesk workflow will appear here.</p>
            </div>
          ) : (
            <div className="workflow-groups">
              {[...workflowGroups.entries()].map(([groupCategory, categoryWorkflows]) => (
                <section className="workflow-group" key={groupCategory} aria-label={groupCategory}>
                  <div className="workflow-group__heading"><strong>{groupCategory}</strong><span>{categoryWorkflows[0].workflow_mode === 'verified_preset' ? 'Assertions enabled' : 'Unverified'}</span></div>
                  <div className="workflow-rows">
                  {categoryWorkflows.map((workflow) => (
                <article className="workflow-row" key={workflow.workflow_id}>
                  <div><h3>{workflow.name}</h3><p>{workflow.description || 'No description'}</p></div>
                  <dl><div><dt>Steps</dt><dd>{workflow.action_count}</dd></div><div><dt>Evidence</dt><dd>{workflow.workflow_mode === 'verified_preset' ? 'Verified' : 'Unverified'}</dd></div></dl>
                  <div className="workflow-actions">
                    {runs.filter((run) => run.workflow_id === workflow.workflow_id).length >= 2 && (
                      <button className="button button--secondary" type="button" onClick={() => void compareRuns(workflow.workflow_id)}>Compare runs</button>
                    )}
                    <button
                      className="button button--secondary"
                      type="button"
                      disabled={requestingWorkflow !== null || session.runner?.readiness !== 'ready'}
                      onClick={() => void replay(workflow.workflow_id)}
                    >
                      <ArrowRight aria-hidden="true" size={15} />
                      {requestingWorkflow === workflow.workflow_id ? 'Requesting…' : 'Replay'}
                    </button>
                  </div>
                </article>
                  ))}
                  </div>
                </section>
              ))}
            </div>
          )}
        </section>

        <section className="run-history" aria-labelledby="run-history-title">
          <div className="library-table__header">
            <div><h2 id="run-history-title">Replay history</h2><p>{runs.length} recorded {runs.length === 1 ? 'attempt' : 'attempts'}</p></div>
          </div>
          {runs.length === 0 ? (
            <p className="run-history__empty">Replay attempts will appear here once the companion accepts a request.</p>
          ) : (
            <div className="run-rows">
              {runs.map((run) => {
                const presentation = runPresentation(run)
                return (
                <article className="run-row" key={run.run_id}>
                  <div><h3>{run.workflow_name}</h3><p>{presentation.summary}</p></div>
                  <span className={`run-outcome run-outcome--${presentation.tone}`}>
                    {presentation.label}
                  </span>
                  <time>{new Date(run.created_at * 1000).toLocaleString()}</time>
                  <button className="icon-button" type="button" aria-label={`Inspect ${run.workflow_name} run`} title="Inspect run evidence" onClick={() => void inspectRun(run.run_id)}>
                    <ArrowRight aria-hidden="true" size={16} />
                  </button>
                </article>
                )
              })}
            </div>
          )}
        </section>
      </main>
    </Shell>
  )
}

function PairingView({ session }: { session: PortalSession }) {
  const [ticket] = useState(() => {
    const value = new URLSearchParams(window.location.hash.slice(1)).get('ticket')
    window.history.replaceState(null, '', window.location.pathname)
    return value
  })
  const [preview, setPreview] = useState<PairingPreview | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'connecting' | 'connected' | 'error'>('loading')
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    if (!ticket) {
      setMessage('This pairing link is missing or has already been removed from the address bar.')
      setState('error')
      return
    }
    void requestJson<PairingPreview>(
      '/api/pairings/preview',
      { method: 'POST', body: JSON.stringify({ ticket }) },
      session.csrf_token,
    )
      .then((value) => {
        setPreview(value)
        setState('ready')
      })
      .catch((caught: unknown) => {
        setMessage(caught instanceof Error ? caught.message : 'Unable to inspect this pairing request.')
        setState('error')
      })
  }, [session.csrf_token, ticket])

  async function connect() {
    if (!ticket) return
    setState('connecting')
    try {
      await requestJson(
        '/api/pairings/connect',
        { method: 'POST', body: JSON.stringify({ ticket }) },
        session.csrf_token,
      )
      setState('connected')
    } catch (caught) {
      setMessage(caught instanceof Error ? caught.message : 'Unable to connect this runner.')
      setState('error')
    }
  }

  return (
    <Shell runner={session.runner}>
      <main className="pair-page">
        <section className="pair-panel" aria-labelledby="pair-title">
          <span className="pair-panel__icon"><ShieldCheck aria-hidden="true" size={25} /></span>
          {state === 'connected' ? (
            <>
              <p className="eyebrow">Connection complete</p>
              <h1 id="pair-title">Runner connected</h1>
              <p>The companion can now receive only the bounded Helpdesk replay requests authorized by this browser session.</p>
              <a className="button button--primary" href="/">
                Open workflow library <ArrowRight aria-hidden="true" size={17} />
              </a>
            </>
          ) : (
            <>
              <p className="eyebrow">Local device permission</p>
              <h1 id="pair-title">Connect replay runner</h1>
              {preview && (
                <div className="pair-detail">
                  <span>{preview.label}</span>
                  <span>Companion {preview.companion_version}</span>
                </div>
              )}
              <p>{message ?? 'Confirm that this browser may request bounded workflow execution from the pending local companion.'}</p>
              <div className="pair-actions">
                <button className="button button--primary" type="button" onClick={() => void connect()} disabled={state !== 'ready'}>
                  <Link2 aria-hidden="true" size={17} />
                  {state === 'connecting' ? 'Connecting…' : state === 'loading' ? 'Checking request…' : 'Connect runner'}
                </button>
                <a className="button button--secondary" href="/">Cancel</a>
              </div>
            </>
          )}
        </section>
      </main>
    </Shell>
  )
}

function App() {
  const { error, refresh, session } = usePortalSession()
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([])
  const [runs, setRuns] = useState<RunSummary[]>([])

  async function refreshWorkflows() {
    const result = await requestJson<{ workflows: WorkflowSummary[] }>('/api/workflows')
    setWorkflows(result.workflows)
  }

  async function refreshRuns() {
    const result = await requestJson<{ runs: RunSummary[] }>('/api/runs')
    setRuns(result.runs)
  }

  async function requestReplay(workflowId: string) {
    await requestJson(
      `/api/workflows/${encodeURIComponent(workflowId)}/runs`,
      {
        method: 'POST',
        body: JSON.stringify({ client_request_key: `request_${crypto.randomUUID()}` }),
      },
      session?.csrf_token,
    )
    await refreshRuns()
  }

  useEffect(() => {
    void Promise.all([refreshWorkflows(), refreshRuns()])
  }, [])

  useEffect(() => {
    const terminal = new Set(['finished', 'interrupted', 'cancelled', 'rejected', 'expired'])
    if (!runs.some((run) => !terminal.has(run.phase))) return
    const timer = window.setInterval(() => {
      void Promise.all([refreshRuns(), refresh()])
    }, 2_000)
    return () => window.clearInterval(timer)
  }, [runs, refresh])

  if (error) {
    return <main className="fatal-state"><CircleAlert aria-hidden="true" /><h1>Portal unavailable</h1><p>{error}</p></main>
  }
  if (!session) {
    return <main className="loading-state"><Activity aria-hidden="true" /><span>Loading workflow library…</span></main>
  }
  return window.location.pathname === '/pair'
    ? <PairingView session={session} />
    : <LibraryView
        session={session}
        onRefresh={refresh}
        onWorkflowsChanged={refreshWorkflows}
        workflows={workflows}
        runs={runs}
        onReplay={requestReplay}
      />
}

export default App
