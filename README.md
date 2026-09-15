# Workflow Capture and Replay

A bounded prototype that records real Django Helpdesk staff tasks through a Chrome extension, classifies the completed recording into a workflow category during review, and replays the immutable result in a separate visible local browser. Categories may own a versioned verification profile; categories without one report execution evidence as explicitly unverified.

**Current status (2026-09-14):** The complete local journey works on macOS Apple Silicon. Real extension captures have driven verified successful/partial/failed linked-ticket runs and an unverified general ticket-create/search run with synchronized evidence. The public Render service is configured but has not been provisioned because paid deployment still requires approval.

## Supported Demonstration

- Platform: macOS Apple Silicon.
- Capture browser: desktop Google Chrome with the unpacked extension.
- Replay browser: Playwright Chromium 151 controlled by the Python companion.
- Target: Django Helpdesk `v2.4.0`, commit `7698042564408aa48c481005650bf223744949a2`, bound to `127.0.0.1:8765`.
- Built-in verified category: `Linked-ticket resolution`, whose assertions require resolving the fictional Review ticket and then assigning/resolving Completion with prescribed notes and independent final checks.
- Unverified categories: user-created organizational groups for ticket creation, list/search/filter, numeric ticket details, and supported native comment, assignment, priority, and status workflows. Their runs show `Finished · Unverified` or `Interrupted · Unverified`; finishing actions is not called business success.
- Demo actor: passwordless `workflow_actor`, active staff and non-superuser, with only `add_ticket`, `change_ticket`, `view_queue`, and `view_ticket` permissions.
- Product AI: none.

This is deliberately not a claim of arbitrary website, browser, operating-system, or complete Helpdesk support. Admin, delete/merge/bulk/hold/dependency-management, saved-query mutation, attachments, outbound-email behavior, and configuration workflows are excluded.

## Architecture

```text
Google Chrome + capture extension ── structured actions ──► FastAPI + SQLite
            │                                                   ▲
            │ local Helpdesk interactions                       │ polling/events/images
            ▼                                                   │
  Django Helpdesk + target SQLite ◄── headed Chromium ◄── Python companion
```

The public service owns portal sessions, pairing, immutable workflows, run requests, synchronized events, and evidence files. The companion owns accepted execution snapshots, local event/artifact outboxes, target lifecycle, and visible replay. Helpdesk alone owns the fictional ticket effects.

## Prerequisites

- Python 3.13. The tested interpreter is 3.13.15; the system Python is not modified.
- Node.js 20.12.x and npm 10.x for a source checkout.
- Git, Make, and desktop Google Chrome.
- Internet access for Python/npm packages, the pinned Helpdesk clone, and the Playwright browser download.

The first setup can download several hundred MiB, primarily Chromium and target frontend dependencies.

## Setup

From the repository root:

```bash
make setup
```

`make setup` performs these distinct operations:

1. Creates `.venv` with Python 3.13 and installs the exact development lock.
2. Installs the exact npm lock and Playwright Chromium.
3. Clones/verifies the pinned Helpdesk revision under ignored `.local/target/source`.
4. Installs the separate exact target environment and prepares required static assets.
5. Initializes the fictional target once if no recognized installation exists.
6. Builds schemas, portal assets, and the unpacked extension.

It does not overwrite an existing target database or baseline. If initialization was interrupted, inspect `.local/target/data` rather than deleting an unrecognized directory automatically.

### Restricted Network Fallback

Some VS Code execution environments reset direct package-host connections. Microsoft public package proxies were used during development. They are not committed as repository defaults. In that environment, run:

```bash
export PIP_INDEX_URL=https://packagefeedproxy.microsoft.io/pypi/simple/
export NPM_CONFIG_REGISTRY=https://packagefeedproxy.microsoft.io/npm/
export YARN_NPM_REGISTRY_SERVER=https://packagefeedproxy.microsoft.io/npm/
make setup
```

No proxy credentials are required or stored.

## Load the Extension

1. Open `chrome://extensions` in Google Chrome.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select `apps/extension/dist`.
5. Confirm extension ID `ooocecppjnccilieepgobfjnlhhmebdk`.

The local build permits only the local portal and loopback Helpdesk target. After a public URL is provisioned, rebuild with its exact HTTPS origin:

```bash
WORKFLOW_PORTAL_ORIGIN=https://your-service.onrender.com make extension-build
```

Reload that build in Chrome. A wildcard production host is not supported.

## Run Locally

Start the portal/API in one terminal:

```bash
make api-start
```

Open <http://127.0.0.1:8000>. In another terminal, initiate pairing:

```bash
make pair
```

The companion opens `/pair` in Google Chrome. Merely opening it grants nothing. Review the pending runner and choose **Connect runner**. When the command reports success, start the background companion:

```bash
make start
make companion-status
```

Only one daemon can own the local state. Stop all managed local processes with:

```bash
make stop
```

## Capture and Replay

Prepare a fresh fictional target and capture session through the running daemon:

```bash
make prepare-capture
```

The command opens the Review ticket in the Chrome profile containing the extension. Keep exactly one supported Helpdesk ticket tab open. It is the deterministic starting point for both modes; a general workflow may navigate from it to **All Tickets** or **New Ticket**.

In the portal:

1. Choose **Capture workflow**. No category or assertion profile is selected yet.
2. Perform the task in the reserved Helpdesk tab, return to the portal, and choose **Stop capture**.
3. On **Review recorded actions**, choose an existing category or **Create new category…**. Existing options identify whether they are Verified or Unverified.
4. Inspect the selected category's verification preview, enter the workflow name/description, and save. Save freezes the category and current assertion version with the immutable actions.
5. Choose **Replay**. The daemon resets the target and performs those captured actions in a separate visible Chromium window.
6. Inspect action results and synchronized screenshots. Workflows assigned to a verified category also show independent outcome checks. After two attempts, choose **Compare runs**.

To use the built-in verified category, perform these exact business steps and then select **Linked-ticket resolution · Verified** during review:

1. On Review, open **Respond**, enter `Demo review completed.`, choose **Resolved**, and select **Update This Ticket**.
2. Open Completion, choose `workflow_actor` in **Assigned to**, and select **Save ticket assignment**.
3. Open **Respond**, enter `Demo completion recorded.`, choose **Resolved**, and update Completion.

For an unverified example, record **New Ticket**, enter a fictional summary/description, submit it, open **All Tickets**, and search for that summary. During review choose **Create new category…**, enter `Ticket intake`, and save. On later captures, `Ticket intake · Unverified` appears as an existing choice. Use only `.test` addresses and fictional text. The target's email backend writes mail to the local console; no real delivery is performed or tested.

The portal does not infer business success from clicks or redirects. Selecting the verified category attaches its frozen profile, which independently checks both ticket states, exact notes, owner, and dependency. A new or existing unverified category has no verifier and never produces `succeeded`, `partially_succeeded`, `failed`, or `uncertain` as a business outcome.

Headed replay pauses for 900 milliseconds after each completed action and holds the final or failure page for two seconds. These pauses are for human visibility only; element readiness and correctness still use Playwright conditions and fresh browser observations.

## Commands

| Command | Purpose |
| --- | --- |
| `make setup` | Reproducible local dependency, target, and build setup. |
| `make api-start` | Build and run the same-origin portal/API on port 8000. |
| `make pair` | Open the one-time explicit runner pairing flow. |
| `make start` / `make stop` | Start or safely stop the single companion daemon. |
| `make prepare-capture` | Queue target reset/start/session preparation through that daemon. |
| `make reset-demo` | Offline target-only reset; refuses a running target. |
| `make test` | Python tests, protocol conformance, lint, portal build, and extension build. |
| `make smoke-pairing` | Headed real-browser portal/companion pairing check. |
| `make smoke-capture` | Genuine capture plus two successful visible replays and comparison. |
| `make smoke-partial` | Genuine F4-style capture plus partial replay outcomes. |
| `make smoke-failed` | Genuine no-milestone capture plus failed replay outcomes. |
| `make smoke-general` | Genuine ticket-create/search capture plus an unverified replay and evidence check. |
| `make smoke-capture-interruption` | Out-of-scope navigation becomes a non-replayable draft. |
| `make smoke-daemon` | Detached process, heartbeat, duplicate-start, and stop check. |

Smoke commands make real writes only to the disposable target and restore its baseline in `finally`. Do not point these commands at private or production data.

## Local Data

All mutable state is ignored under `.local/`:

- `.local/api/`: central development database and synchronized evidence.
- `.local/state/`: companion credentials, accepted snapshots, outbox, and lifecycle operation records.
- `.local/target/`: pinned checkout, target environment, target database, baseline, and logs.
- `.local/artifacts/`: smoke-test screenshots and bounded diagnostic records.

The extension keeps active draft data in extension-local storage. Credentials, cookies, CSRF fields, bootstrap routes, full HTML, and network bodies are excluded from workflow actions.

## Tests and Results

The high-signal local verification commands are:

```bash
make test
make target-test-session
make smoke-pairing
make smoke-capture
make smoke-partial
make smoke-failed
make smoke-general
make smoke-capture-interruption
make smoke-daemon
```

## Deployment

[render.yaml](render.yaml) declares the selected single paid Render Starter service and 1 GiB persistent disk. It installs [requirements-api.lock](requirements-api.lock), builds the portal, and mounts the central database/artifacts under `/var/data`.

Applying the blueprint incurs ongoing charges. It has not been applied. After explicit spending approval, deployment still requires persistence/restart checks, Secure-cookie inspection, public pairing, exact-origin extension rebuild, and a shutdown date.

## Known Limitations

- Only the selected macOS/Chrome environment and bounded pinned Helpdesk staff routes are supported: ticket list/search, creation, numeric detail, and update forms.
- Helpdesk's linked-ticket prerequisite is enforced by its normal UI but not by the tested backend update path. Replay never forces the disabled control and verifies the prerequisite independently.
- Capture preparation currently requires `make prepare-capture`; portal-triggered preparation remains deferred.
- Saved workflow actions, category snapshots, and profile references are immutable. Categories are stable records used for grouping; category administration, renaming, workflow editing/version comparison, arbitrary assertion authoring, AI naming, automatic grouping, and merging are not included.
- New categories are always unverified. This prototype has no human account roles or assertion editor, so a workflow administrator cannot yet define a second verifier such as “assignment is success.” The built-in category is the one seeded admin-defined example.
- The demo actor is intentionally not a superuser. Django admin and destructive/configuration actions are outside capture and replay policy even though the admin URL namespace is registered for an upstream template dependency.
- Cancellation/revocation during an in-flight browser mutation and automatic artifact retention cleanup are not complete.
- Product deployment, public persistence, and a clean reviewer machine have not yet been observed.
- Full video, arbitrary sites, multiple target tabs, file transfer, frames, canvas, rich-text editors, keyboard-only/custom widgets, and fuzzy element matching are unsupported.

Upstream Django Helpdesk and its bundled assets retain their BSD-3-Clause and third-party notices in the pinned checkout.