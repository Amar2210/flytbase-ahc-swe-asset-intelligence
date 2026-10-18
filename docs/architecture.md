# Architecture

One Streamlit application and one SQLite database store source evidence, shared
updates, private memory and API scheduling state. No separate API server, vector
database or graph service is required.

See the [system diagram](../README.md#system-architecture),
[application schema](application-schema.md), [source data map](data-map.md) and
[Q8 demonstration](q8-demo.md). The [project report](../report.md) records the
optimization results and screenshots.

## Code map

| Module | Responsibility |
| --- | --- |
| `app.py` | Dataset imports, user/conversation selection, chat, evidence, token metrics and visual previews |
| `copilot/config.py` | Local `.env` settings with process-environment precedence; secrets stay out of persisted payloads |
| `copilot/db.py` | Source and memory schema, connection setup and additive migrations |
| `copilot/ingest.py` | Validated, atomic folder/ZIP imports with content-derived dataset IDs |
| `copilot/agent.py` | Bounded model loop, private history, citations, backend-sourced visuals and reviewed updates |
| `copilot/context.py` | Select a smaller tool catalogue and include SQL schema only when needed |
| `copilot/tools.py` | Dataset-scoped SQL, document retrieval, well status, spill checks and controlled writes |
| `copilot/recalls.py` | Supplier notices, completed pump installations and attributed replacement reports |
| `copilot/history.py` | Exact, timezone-aware private-history date windows |
| `copilot/preferences.py` | Enum-valued display preferences scoped to a dataset and user |
| `copilot/correction_drafts.py` | Conservative local extraction of fully specified correction proposals |
| `copilot/production_capabilities.py` | Local check for missing oil-production volume measurements |
| `copilot/model.py` | Google/OpenRouter REST adapters, token counts, usage metrics and bounded retries |
| `copilot/rate_limits.py` | Persistent, shared Google input-token reservations and cooldowns |
| `copilot/previews.py` | Six explicit read-only chart/table presets, without model calls or chat writes |

## Import and evidence

The same importer handles local folders and ZIP uploads. It validates source IDs,
cross-well references, dates, production rules and report paths before committing.
ZIP paths, symlinks, expanded sizes and file counts are bounded.

Each import has a deterministic content-derived `dataset_id`. Reimporting identical
files reuses that dataset; changed files create a separate selectable version.
Original JSON, document bodies, source paths and available line numbers remain in
SQLite. Uploaded evidence survives temporary extraction-directory cleanup.

Structured reads use an in-memory snapshot of the selected dataset's public
tables. Private chats, preferences, review state, token ledgers and other datasets
are never copied. A SQLite authorizer, function allowlist, execution budget and
300-row output cap constrain generated SQL. Aggregation runs before that cap.

`effective_inspections` applies accepted corrections and imported false-spill
notes without replacing raw readings. `latest_inspections` selects observations
on or before the evaluation date. Production status uses the imported manual's
temperature threshold; it does not measure oil volume. Inspection-derived downtime
is seven days per nonproducing weekly inspection; missed inspections add zero.
Repair `downtime_hours` is a separate measure.

Document retrieval ranks keyword matches and returns numbered passages or exact
imported documents. Compound spill and recall tools gather related rules and
records in one read. General SOP retrieval remains model-selected, not a mandatory
policy gate. Visuals use validated tool-result references and actual backend rows,
never model-supplied data arrays.

## Request and context limits

1. Save the current prompt privately for the selected dataset, user and session.
2. Load up to 16 recent messages once, with a 6,000-character combined budget.
   Preserve the current prompt up to the 20,000-character input limit; disclose
   omitted history and use scoped history search for earlier discussions.
3. Add instructions, dates, allowed preferences and the selected tool catalogue.
4. Count the complete Google generation input and reserve the shared TPM budget.
5. Execute requested tools, collect server-issued citations and append bounded
   evidence. Each result has an 8,000-character limit and all results share a
   12,000-character budget. Full results remain server-side for the UI.
6. Answer within at most three model rounds, with no tools on the final round.
   Complete compound reads normally finish in two rounds. At most eight tools
   execute after one model response.

Fully specified inspection corrections can stage locally, but still require
review. An unsupported oil-volume question can return a sourced data-limit answer
without generation. Provider failures may display clearly marked partial read
results; they never count as a saved operational update.

Google `countTokens` measures the complete request, including instructions and
tools. Returned usage supersedes preflight counts when available. Unknown counts
remain unavailable, never zero; with shared pacing enabled they block generation.
The default per-request input limit is 12,000 tokens, separate from TPM.

The local rolling 60-second ledger shares a 14,500-token working budget under the
configured 16,000 TPM allowance. Each generation attempt, including retries,
reserves `ceil(input_tokens × 1.10) + 64`. Failed attempts retain reservations until
expiry. Queue waits are bounded to 180 seconds and do not consume the generation
timeout. Transient inference errors allow up to three HTTP attempts; HTTP 429
creates a shared cooldown without automatic retry. The ledger cannot observe
requests from other programs or establish remaining provider quota.

## Memory and writes

| Scope | Storage and rules |
| --- | --- |
| Shared | Dataset-scoped `context_notes`, `corrections` and `equipment_replacement_reports`; relevant records are read fresh |
| Private | `chat_messages` and `user_preferences` require dataset and user; current conversation history also requires session |
| Review | `pending_updates` binds the validated proposal to its dataset, user and conversation; confirmation uses server-stored arguments |

Shared notes, corrections and replacement reports require an explicit factual
instruction followed by operator confirmation. New requests supersede unconfirmed
proposals. Confirmation is idempotent for a saved proposal; cancellation persists.
Corrections retain originals, author, reason and source-note backlinks. Competing
values are disputed rather than silently selecting a winner. Feedback tags do not
grant acceptance or create type-wide rules.

A confirmed direct replacement report can exclude an earlier recalled pump
installation, with attribution and a warning that its work order is unverified.
A secondhand report retains the actual reporter and claimed source; it remains
unconfirmed and does not remove a pump. A later scheduled check does not establish
a new installation.

Explicit private formatting preferences save without shared-update confirmation
and persist across that user's conversations. They never become another user's
defaults. Private history search is paginated and excludes other users' messages
regardless of model arguments.

## Clocks and deployment boundaries

- `COPILOT_AS_OF_DATE` controls field analysis. The sample benchmark uses
  `2026-10-16`; the released test field uses `2027-01-15`.
- Operational relative-date reports use `COPILOT_REPORT_DATE` when set, otherwise
  the runtime field-local date. Plan end dates are inclusive; expiry never proves
  repair completion or resumed production.
- Private-history dates always use the real clock in `COPILOT_FIELD_TIMEZONE`.
  Weeks begin Monday; inclusive local dates become exact UTC `[start, end)`
  intervals, including daylight-saving transitions.

The user picker is a demo identity mechanism, not authentication. Production
requires server-side authorization, an explicit correction-authority policy and
durable deployment storage. Automated recovery for proposals interrupted while
processing, dispute resolution, dataset-lineage merging and type-wide rule memory
are not implemented.
