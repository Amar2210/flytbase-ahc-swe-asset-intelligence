# Decision log

Updated: 17 October 2026 (Asia/Kolkata).

This is a living design record. Iteration 1 implementation is recorded in D08.
**Confirmed** means the user stated the direction. **Required** means the
challenge specifies it. **Recommended** means a design proposal that the user has
not yet adopted. **Pending** means the decision remains open.

## D01 - Deliverable and development constraints

**Confirmed:** Build the complete copilot for the oil inspection team. This is a
solo project. Use Python as the working language.

The UI must support questions about the supplied field records and shared
operational context. The challenge also requires citations and suitable inline
visuals; see the [problem statement](../challenge-dataset/01-problem-statement.md).

**Confirmed and implemented:** Streamlit for the hackathon UI. Business logic is
kept in Python modules so the UI can later change. A separate web API is not
part of the initial single-application design.

**Confirmed:** The first demonstration runs locally. The user chose local
SQLite as the starting database and wants the project to be suitable for sharing
on GitHub. Publishing or pushing has not been requested in this step.

## D02 - Records, document retrieval, and relationships

**Confirmed for iteration 1:** Use a relational database for structured records and durable
updates, with document retrieval for manuals, procedures, and repair prose.
RAG describes retrieval before generation; it is not a competing database type.

Known relationships such as well -> inspection -> repair can be represented
with foreign keys and joins. A graph database is not currently justified by a
requirement for variable-depth relationship traversal.

**Confirmed starting database:** SQLite on persistent local disk for the first
single-instance demo. It can support RAG through Python queries and text
retrieval. Relationships are represented with foreign keys and joins; SQLite is
not being adopted as a native property-graph engine.

**Future recommendation:** PostgreSQL if hosting lacks persistent disk, requires
multiple application instances, or needs substantial concurrent writes.

Use metadata selection plus keyword/full-text search first for documents.
Embeddings are an optional later addition if retrieval evaluation shows a need.
The detailed retrieval/tool design from original question 7 is deferred.

## D03 - Initial and unseen datasets

**Confirmed direction:** The challenge supplies sample data first and a new test
dataset later. Support importing data without editing application source. The
user prefers an upload option.

**Required:** The dataset guide says to load test data alongside the sample.
Preserve both imports. Do not assume a test upload deletes or replaces the sample.

**Implemented design:** Accept a ZIP that preserves the source folder structure.
An optional local path uses the same importer. Do not require a special code path
for the test data or for named sample wells.

Import into a staged dataset version, validate it, show record counts and
problems, and make it selectable only after a successful import. Keep prior
datasets available if validation fails. Namespace records, shared notes, and
corrections by `dataset_id`; identical IDs must not silently collide.

**Implemented for the test release:** Load the 600-well test package as a
separate selectable dataset alongside the sample. The local demo points to
`challenge-dataset/test-data/dataset`. Shared updates, preferences and chats
remain in their original dataset namespace. A deliberate carry-forward policy
and dataset-lineage merge remain pending.

An upload must be persisted by the application. Receiving bytes in a UI widget
does not itself provide durable storage. Exact archive-size limits and supported
input variations remain implementation choices.

## D04 - What becomes shared context

**Confirmed:** Ordinary conversations are not all shared knowledge. Define
explicit extraction rules. A user-reported anomaly or correction about a well
must be available as relevant context when another user later asks about it.

**Required baseline:** Preserve source records and record who supplied a
correction. Disclose conflicting evidence.

For imported context, the guide already defines `note_type="correction"` with a
target `inspection_id` as an override of that inspection's detection. Honor that
existing rule; ordinary disagreeing notes are not formal overrides. This is
distinct from deciding how new chat messages become correction records.

**Resolved source rule, highlighted by the user:** Spill Response SOP section 7
states that a qualified person's confirmation of water, mud, or shadow is
recorded as a context correction. Corrected detections are excluded from spill
reporting and statistics. The source inspection remains preserved. The supplied
WELL-0001 example is CTX-0001-001 targeting INS-0001-0070, authored by U-401.

**Recommended starting distinction:** Save an operational report as an attributed
claim. Treat a suspected anomaly as a reported discrepancy until the agreed
correction policy permits a validated correction. Do not silently replace a
sensor value because a message mentions an anomaly.

**Pending:** Exact chat promotion rules, required confirmation evidence for new
chat corrections, which roles count as qualified for each kind of correction,
and resolution of competing corrections. Do not impose an invented role
allowlist; the SOP states qualification but does not enumerate an authorization
matrix. The confirmed false-positive exclusion rule itself is already defined.

## D05 - Field time and maintenance plans

**Confirmed:** The intended deployment serves one field. Operators should not
have to select a timezone for each message.

**Recommended:** Configure one timezone for that field. Preserve timezone-aware
source timestamps. Interpret relative dates in field time, preserve date-only
precision, and store instants consistently. A single field simplifies timezone
handling; it does not eliminate date-boundary or UTC/local-time differences.

The source field timezone and the treatment of an ambiguous date-only end are
still pending. Do not use the developer's Asia/Kolkata timezone as the field
timezone without evidence.

**Confirmed behavior:** If maintenance was planned through 20 October and no
later update exists on 21 October, report the old plan and the lack of a later
confirmation. Do not infer completion or resumed production from the end date.

**Required benchmark convention:** The supplied evaluation sets today to
16 October 2026 and treats right now as the latest inspection. Keep this
evaluation `as_of` setting distinct from the live clock and user-facing field
time. See [evaluation conventions](../challenge-dataset/03-evaluation-queries.md).

The test release supersedes that sample clock with **15 January 2027**.
`COPILOT_REPORT_DATE=2027-01-15` gives operational reports the same simulated
today, so Q8's "yesterday" is **14 January 2027**. Actual chat timestamps and
private-history calendar searches continue to use the runtime field-local clock.

## D06 - Model route

**Confirmed constraint:** Use Gemma through a free hosted route, considering
Google AI Studio / Gemini API and OpenRouter.

**Confirmed initial route:** Google's direct Gemini API with an AI Studio key.
The user chose the larger 31B option: `gemma-4-31b-it`. The optional OpenRouter
adapter defaults to `google/gemma-4-31b-it:free`. The adapters and configuration
are implemented. Current hosted-model and pricing documentation lists Gemma 4
and free input/output. Live provider checks and the later route change are
recorded in D09 below; keys remain only in local ignored configuration.

**Confirmed configuration direction:** API keys and provider/model choices live
in the local project-root `.env` file, outside the chat UI. A blank template is
created only when the file is absent; existing contents are preserved. `.env`
is Git-ignored and the key itself never belongs in `.gitignore`. The UI sidebar
is reserved for dataset imports. Editing `.env` takes effect on the next app
reload; the loader does not copy file values into `os.environ`. Actual process
environment variables take precedence, and a blank Google key can fall back to
`GEMINI_API_KEY`. Settings representations omit the API key.

Backend validation remains necessary for model-proposed updates. JSON that parses
can still identify the wrong well, date, or correction target.

## D07 - Discussion pace and deferred topics

**Confirmed:** Address no more than five questions at a time.

Original question 5 has a resolved false-positive rule from Spill Response SOP
section 7 and a supplied context-note format. The user then authorized building
iteration 1 before further discussion. Minimal private/shared boundaries and
complete SQL aggregation are implemented to satisfy the brief. More detailed
policy discussion can continue in later iterations.

## D08 - First implemented iteration

**User authorization:** Load SQLite first, build the Streamlit chat UI, connect
Gemma (prefer 31B), and iterate quickly. A graph database is a possible later
experiment; it is not required for this first build.

**Implemented:** Atomic, additive, idempotent folder/ZIP importer; original data
and source provenance; document storage; manual-derived production thresholds;
dataset-scoped read tools and effective inspection views; persisted per-user
conversations; shared well notes; reviewed inspection corrections; citations;
inline visual data from tool results; Google and OpenRouter adapters.

**Interim write policy:** Explicit reports or correction requests are parsed into
server-stored previews bound to the user, dataset, and conversation. The reviewer
confirms exact fields, dates, reasons and qualification before shared persistence.
Cancellation is durable. Ordinary chat, hypotheticals and quoted questions do not
authorize a shared write. Raw source inspections are immutable.

**Interim conflict policy:** Competing runtime correction values become disputed
and are not silently applied. A runtime claim of a real spill conflicting with
an imported false-positive correction is also disputed. Correction records are
linked to newly created shared context notes and keep author/reason provenance.
A dispute-resolution interface and formal role authorization remain future work.

**Initial data loaded:** `data/copilot.sqlite3`, 200 wells, 39,567 inspections,
5,665 maintenance records, 90 regulator events, 141 context notes, and 1,514
documents. Runtime database and secrets are Git-ignored.

**Verification:** All 37 automated import, domain, model-protocol, orchestration
and Streamlit integration tests passed without a real API key. SQLite integrity
and foreign-key checks passed. The running app loaded in a real browser with
zero browser script errors; Google's API host was reachable without a key.
Live Gemma answer quality
and all 18 challenge questions are not yet validated. See README for commands.

**Known first-iteration limits:** No graph or embedding index, no type-wide
learned rule updates, no automatic dataset-lineage merge, no production login or
dispute-resolution workflow, and no crash recovery for an interrupted update
confirmation. The local single-instance demo remains the intended deployment.

## External references checked on 17 October 2026

- [Streamlit chat elements and inline content](https://docs.streamlit.io/develop/tutorials/chat-and-llm-apps/build-conversational-apps)
- [Streamlit uploader](https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader)
- [Streamlit session-state limitations](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state)
- [SQLite deployment guidance](https://www.sqlite.org/whentouse.html)
- [SQLite deployment in industry](https://www.sqlite.org/mostdeployed.html)
- [SQLite recursive relationship queries](https://www.sqlite.org/lang_with.html)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [Hosted Gemma models](https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api)
- [Gemma pricing](https://ai.google.dev/gemini-api/docs/pricing#gemma-4)

## Source checkout provenance

The checkout location is `challenge-dataset/`, now at revision
`009e2b596782150f420021f9472ab37e3a2953ee` after the test release. The initial
sample checkout was `b8de8c9bd68f40334e5d06922126169e4f3bf3ca`. The local source inventory is
recorded in the architecture document. GitHub can store source and an optional
synthetic database snapshot; it is not the runtime shared-memory database.

## D09 - Local secrets, cleaner UI, provider checks and architecture

**Confirmed and implemented:** The sidebar contains only dataset import controls.
Crew and conversation controls remain in a compact main row. API keys, model,
analysis date and timezone live in the ignored `.env` file; there is no key widget.

**Historical route at D09:** The user requested OpenRouter after Google's direct
Gemma route failed. Local configuration now selects
`google/gemma-4-31b-it:free` through OpenRouter. The user supplied each provider's
key in `.env`; neither key was printed, stored in SQLite or put in source files.

**Live observations:** Google's 31B and 26B endpoints returned HTTP 500 (INTERNAL)
even for minimal diagnostic prompts. OpenRouter's free 31B and 26B endpoints
returned HTTP 429; a minimal 31B check identified an upstream rate limit. These
observations do not establish a platform-wide outage. The free OpenRouter model
page lists Google AI Studio as an upstream provider. A successful live model
explanation remains unverified.

**Implemented failure handling:** Transient inference errors retry up to three
times within one inference call's timeout. Invalid requests, keys and 429 quotas
are not automatically retried. Successful read results remain visible as sourced
partial responses if a later model call fails. An initial transient/429 failure
can trigger a bounded local lookup of exact well IDs for questions. Reports are
not saved via that fallback. A real OpenRouter 429 test verified the local answer
for WELL-0001: not producing at its 15 October inspection, inspector Ryan Brooks,
attributed crew notes, sources and a status card.

**Architecture clarification:** The implemented retrieval is SQL plus simple
lexical document search, with structured shared-note lookups. The model receives
schema, instructions, limited private history and selected read results. All
inspection rows remain local. Embeddings, a vector database and a graph backend
are absent. FTS5 is populated but the active document search does not use it.

**Known coverage gap:** Relevant SOP retrieval, fresh crew-note retrieval on SQL
paths, use of effective views, and correct downtime aggregation are model planning
responsibilities. The backend does not yet enforce a mandatory SOP/context step
or validate the business semantics of every SQL query. The new
[architecture walkthrough](implemented-architecture.md) and
[interactive view](implemented-architecture.html) document these limits explicitly.

**Verification:** The 53-test full suite passed, followed by the expanded
20-test agent suite and updated UI checks. Provider tests remain simulated at the
HTTP boundary; separate live checks record real provider errors and local fallback.
Mermaid parsed/rendered successfully. Browser checks confirmed an import-only
sidebar with no credential widget and no browser script errors.


## D10 - Token accounting, quota diagnosis and visual previews

**Confirmed at D10:** The user switched back to Google AI Studio and requested the full input-token count in the UI, including instructions, schema, history, tool definitions and retrieved SOP/document results. At that stage local .env selected Google gemma-4-31b-it; D14 records the current 26B route. Existing keys remain local and ignored.

**Implemented:** Each Google round counts the exact generation payload through countTokens.generateContentRequest, including system instructions and function declarations. One bounded count request runs before generation; usageMetadata supersedes it when returned. Live callbacks display latest input, peak context and model calls; private message payloads retain per-call counts/status/output and generation attempts. Missing counts show Unavailable. This is context accounting, not a claim of billed usage or answer quality.

**Quota evidence:** A read-only OpenRouter key-metadata check showed 0/50 daily free requests used, 50 remaining and zero recorded monetary usage. Earlier failures identified upstream 429, so the daily allowance was not exhausted at the time checked. Live checks had used the user-supplied application keys; automated tests and coding subagents use mocked model boundaries or Codex, respectively. No new live generation was performed for this change. One static Google tokenization-only test returned HTTP 200 with 59 tokens.

**Visual verification:** Added an explicit Visual preview tab with six real database presets: status, temperature trend, downtime bar, corrected-spill heatmap, map and table. Only Show preview executes the read; no model client or chat write is used. Map tiles may independently use network access. These previews test rendering and data, not natural-language planning.

**Verification:** All 95 offline tests passed, covering full token payloads, count failures, failed generation, callbacks, persistence, privacy, date history, typed corrections and all six visual renderers.


## D11 - Recent context is reused on every model round

**Historical behavior, superseded by D12:** At the start of each user request, the copilot saved that message and loaded up to 16 individual messages from the same dataset, user and conversation. The current message was included; fewer messages were loaded when fewer existed. Each history message was capped at 14,000 characters. The number 16 was a provisional message-count cap, not a model-derived token budget.

At that point, history was loaded from SQLite once per user request and serialized again for every model round, with new assistant tool calls and tool responses appended. Tool definitions were attached on each round. Date-aware private-history retrieval added older selected messages as tool results. There was no recent-history relevance filter or automatic token-budget trimming. D12 retains repeated serialization but adds bounded recent context and a final call without tools.

**Observed heatmap request:** The latest recorded user heatmap request had three available conversation messages and completed through two Google calls. Returned input token counts were 3,556 and 7,135. These counters record successful live inference; they do not establish that all challenge scenarios are validated. Query evidence and source panels do not display the complete prompt or private history. No private session IDs are included here.

The architecture walkthrough was refreshed to show token preflight, repeated context serialization, date-based private search, shared feedback tags and the database-only visual-preview path. The spill-only query's broad manual citation issue remains explicitly documented as unresolved.


## D12 - Three model calls, compact evidence and containment compliance

**Confirmed:** The user requested at most three model calls and a system-prompt reduction of at least 25%, and authorized a real API test of: "In Southwest Block 24, which spills in the last 90 days were not contained within the SOP time limit?"

**Implemented:** The orchestrator enforces a hard maximum of three model calls, including final synthesis. Retrieval replies may batch up to eight tool calls. The final call has no tools, must answer from available evidence and must disclose missing coverage; further tool requests cannot cause a fourth model call. Transient HTTP retries remain separate, with at most three attempts within each inference timeout. A smaller configured round limit still applies.

The revised full system context measured 6,444 characters and 1,690 Google tokens, compared with 9,319 characters and 2,304 tokens using the same reference dates and schema: 30.85% fewer characters and 26.65% fewer tokens. This is a historical pre-Q8 measurement, not a count of the current expanded tools/preferences context. Evidence-first answers, scoped private memory, correction provenance, human review and date semantics remain in the instructions.

Model-bound tool evidence now permits 60 row items, 20 other list items and 6,000 characters per string, followed by valid-JSON limits of 12,000 characters per tool response and 24,000 across all tool responses in a model request. The shared budget is reapplied as retrieval grows. Omission markers require incomplete coverage to be disclosed. Citations sent to the model use compact IDs and source locations; full retrieved records and source metadata remain server-side for the UI.

Recent private context keeps at most 16 stored messages within a combined 12,000-character budget, with newer messages taking priority. Oldest messages are removed or partly shortened, and a server notice marks omissions. The current user prompt is preserved in full up to the existing 20,000-character input limit, even when it alone exceeds the history character budget. This does not delete stored history; date-aware search can retrieve earlier messages. The character budget is separate from the measured input-token guard.

Google preflight counts above `COPILOT_MAX_INPUT_TOKENS` (default 12,000) stop before generation. Count failure still permits generation, so this does not guarantee an input bound when counting is unavailable. This is a per-request guard, not a live TPM meter, quota reservation or request-pacing mechanism.

`get_spill_compliance` gathers the actual imported SR-02 containment deadline, effective spill detections and linked repair-report evidence in one read. Explicit elapsed containment hours determine compliance; repair duration and reporting delay are never substitutes. Missing or conflicting evidence is unknown. The default inclusive 90-calendar-day period ends on the evaluation date: 19 July through 16 October 2026 for the sample default. Matching detections are not deduplicated incidents. The complete public sample result has ten effective spill detections and two breaches: WELL-0082 at 7.1 hours and WELL-0100 at 8.1 hours, against a four-hour SOP limit.

**Observed failure:** Before these changes, the exact question expanded across three calls with 3,504, 9,722 and 18,862 input tokens. The third call timed out after two generation HTTP attempts within its 90-second timeout. The user's earlier screenshot showing a failed first call was a separate HTTP 500. Neither failure establishes that the displayed historic TPM peak caused a quota rejection.

**Live validation:** After the changes, the exact question completed against Google in two model calls with 3,109 and 7,197 input tokens from returned usage metadata, one generation HTTP attempt each and no partial response. The model selected `get_spill_compliance` and returned the two correct breaches in 73.3 seconds. This check used public challenge records. Offline tests mock the provider boundary; they do not consume generation quota.

**Final verification:** All 113 offline tests passed in 49.9 seconds, including the new three-call bound, bounded evidence, recent-history trimming and containment evidence path. The running Streamlit health endpoint returned HTTP 200.

## D13 - Explicit correction proposals survive model unavailability

Michael's fully specified WELL-0164 correction produced only reads. Google failed
with HTTP 500 before selecting a write tool, and the read fallback displayed the
old production status instead of clearly reporting an unprepared correction.
No shared correction or pending proposal existed for that request.

Fully specified, affirmative correction commands now have a conservative local
proposal path: exact inspection ID, one supported replacement field/value, and
an explicit reason. The same target checks and human confirmation apply; the
parser cannot save a correction. Ambiguous commands continue through the model.
The review shows the original/replacement values, author, reason and original
inspector note. The local route is labeled as zero model calls. No benchmark
answer, well ID or replacement value is hardcoded.

All recognized write requests now return an explicit unsaved result if model
retrieval never produces a valid proposal or the provider fails. Read results
cannot claim that a correction was saved. Read-only questions still use their
existing sourced fallback. Write traces distinguish `pending_confirmation` from
`saved`; the application performs the INSERTs only after confirmation. Original
readings are preserved and accepted corrections are shared within the dataset.

Offline agent and UI regressions execute the exact Michael message, verify no
correction before confirmation, then verify Jessica receives 41°C / 21.1°C /
producing in her own chat after confirmation. No model API calls or changes to
the live application's inspection records are needed for these tests.

## D14 - Test dataset and Q8 shared reports with private preferences

**Authorized direction:** Use the newly released test data and demonstrate Q8's
four sessions and five turns. Normal chat should let Gemma select a write tool
when the user reports an update. The application performs validated writes;
model-generated SQL remains read-only.

**Imported:** Source commit `009e2b5`, folder
`challenge-dataset/test-data/dataset`, with 600 wells, 125,198 inspections,
17,415 maintenance jobs, 1,887 original context notes, 4,449 documents, 27 users,
7 areas, 6 teams and 3 well types. The sample import remains available. The
importer accepts a well's own known team overriding its area's default and
preserves new optional source fields in `raw_json`.

**Shared operational memory:** `record_equipment_replacement` is a semantic
write tool, selected by Gemma. Python validates the target, effective date,
statement and evidence basis, stages a user/dataset/conversation-bound proposal,
and saves only after human confirmation. The new
`equipment_replacement_reports` table stores actual reporter, optional claimed
source, status and effective date, with an attributed `context_notes` backlink.
Original maintenance and equipment source records are unchanged.

**Provenance policy:** A confirmed direct report becomes `reported_replaced`.
Subsequent `get_recalled_pumps` reads exclude the earlier recalled installation
and identify who reported its replacement, while disclosing the missing work
order. An indirect report becomes `unconfirmed`; Olivia saying "Nathan told
me" remains authored by Olivia with Nathan as the claimed source. The pump is
retained pending direct confirmation or documentary evidence. This distinguishes
evidence quality without inventing an SOP authorization matrix.

**Recall retrieval:** The backend derives the supplier/model/positive affected
lot list from the imported recall notice. It derives each latest completed
installation from explicit **Parts installed** report lines and joins the
matching maintenance job's total installation cost. Later checks do not replace
the installed pump; future planned finishes are not proof of completed work;
current ESP wells do not inherit an old rod-pump recall. A later installation
cannot be hidden by an older runtime replacement report. Results retain rule,
report-line, job-cost and shared-report sources.

**Private persistent preferences:** `save_my_preferences` stores allowed sorting
and currency-display enums under `(dataset_id,user_id)`, independently of chat
session. Explicit private preferences need no shared-update confirmation.
`get_my_preferences` and scoped context loading expose only the selected user's
preferences. Recall tables apply Nathan's area/ID order and costs in thousands
deterministically; Robert's presentation remains his own. Preference provenance
includes the user's name, update time and original saved message when available.
"My area" uses the selected user's current team and each well's own team.

**Clocks and configuration:** The ignored local `.env` points to the test folder,
sets analysis date and `COPILOT_REPORT_DATE` to **2027-01-15**, and selects Google
`gemma-4-26b-a4b-it`. Operational "yesterday" is therefore **2027-01-14**.
Private chat timestamps and history searches retain the actual runtime clock.
Built-in fallback settings still name the sample and 31B model; these are local
demo overrides. No secrets are printed or stored in the database.

**Live provider observations:** Google's 31B Q8 attempt returned HTTP 500. The
26B route then completed Nathan's first turn in two model rounds, with **3,674
and 7,188 input tokens**, and prepared the correct replacement review proposal.
A later OpenRouter `google/gemma-4-31b-it:free` request returned HTTP 429 on its
first HTTP attempt in 5.8 seconds: one logical round, unavailable token usage,
no tool execution, no pending proposal and no shared write. Google 26B remains
the local configured route. These observations do not establish continued
availability or a provider-wide outage.

**Verification:** The full **149-test offline suite passed**, including three
scripted Q8 checks using the real test data with simulated model responses.
After a further one-attempt HTTP regression, all 21 targeted model tests passed.
Only Nathan's initial proposal has been verified live on Google 26B; the full
live Q8 sequence and all 11 final questions remain unverified.

**Remaining semantic gaps:** Imported typed `data_correction` notes,
effective-dated type/team histories, permanent closures, operator-status
precedence and the new SOP/MOC rules require dedicated semantic coverage beyond
lossless import. There is still no general mandatory SOP gate, type-wide learned
rule engine, graph database or embedding retrieval. The pre-Q8 1,690-token system
measurement is historical; current complete input is measured per model call.
