import type { WorkflowDefinition } from '@workflow/protocol'

export type WorkflowAction = WorkflowDefinition['actions'][number]
type WithoutActionIdentity<Action> = Action extends unknown
  ? Omit<Action, 'action_id' | 'sequence'>
  : never
export type DraftAction = WithoutActionIdentity<WorkflowAction>

export interface StartCaptureMessage {
  type: 'workflow:start-capture'
  apiOrigin: string
  capability: string
  draftId: string
}

export interface StopCaptureMessage {
  type: 'workflow:stop-capture'
  draftId: string
}

export type ExternalMessage = StartCaptureMessage | StopCaptureMessage

export interface BeginContentCaptureMessage {
  type: 'workflow:begin-content-capture'
  initial: boolean
}

export interface EndContentCaptureMessage {
  type: 'workflow:end-content-capture'
}

export interface ContentReadyMessage {
  type: 'workflow:content-ready'
}

export interface CapturedActionMessage {
  type: 'workflow:captured-actions'
  actions: DraftAction[]
}

export interface CaptureErrorMessage {
  type: 'workflow:capture-error'
  error: string
}

export type InternalMessage =
  | BeginContentCaptureMessage
  | EndContentCaptureMessage
  | ContentReadyMessage
  | CapturedActionMessage
  | CaptureErrorMessage

export interface PageIdentity {
  target_alias: 'helpdesk-demo'
  path: string
  query?: string
  record_ref?: 'review' | 'completion'
}

export interface ActiveCapture {
  apiOrigin: string
  capability: string
  draftId: string
  tabId: number
  startedAt: string
  nextBatchIndex: number
  nextSequence: number
  documentIds: string[]
  pendingBatches: PendingBatch[]
  lastPage?: PageIdentity
  captureError?: string
}

export interface PendingBatch {
  batchId: string
  batchIndex: number
  actions: WorkflowAction[]
}