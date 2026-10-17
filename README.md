# Asset Intelligence Copilot

## Report and demo

- **[Read the project report](report.md)** — system design, memory scopes, rate limits, optimization, and six screenshots showing the results before and after the changes.
- **[View the database and memory schema](docs/application-schema.md)** — rendered Mermaid diagram of the SQLite tables and their relationships.
- **[Follow the Q8 demo](docs/q8-demo.md)** — exact messages, user switching, confirmation steps, and expected results.

First working iteration of a local Python + Streamlit + SQLite copilot for an oil
inspection team. Gemma 31B through Google AI Studio / Gemini API is the default.
An optional OpenRouter free Gemma adapter uses the same tools.

## Run locally

Python 3.11 or newer is recommended (verified with Python 3.13.5).
The challenge data is already cloned in this workspace. For a fresh checkout of
the app, first run:

```powershell
git clone https://github.com/FlytBaseAILabs/flytbase-ahc-swe-asset-intelligence-dataset.git challenge-dataset
```

The upstream checkout is Git-ignored to avoid accidentally committing an embedded
Git repository; importing a supplied dataset through the UI is also supported.

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

Open http://127.0.0.1:8501. The first launch imports `challenge-dataset/sample-data`
if the database is empty. The sample is already imported in this workspace.

Configure the model in the project-root `.env` file. An empty local template is
already available in this workspace. For a fresh checkout, copy `.env.example`
to `.env` and set `GOOGLE_API_KEY` there. Leave `COPILOT_PROVIDER=google` and
`COPILOT_MODEL=gemma-4-31b-it` for the default route. A blank Google key falls
back to `GEMINI_API_KEY` when provided.

Model selection and API keys are configured outside the UI. The sidebar is for
dataset imports. Reload the app after editing `.env`; the file is read afresh
without a stale cached key. Actual process environment variables take precedence
over the file. Keep the key out of chat messages, screenshots and source files.
The root `.env` file and generated databases are ignored by Git; `.env.example`
contains placeholders only. API keys are never saved in SQLite.
Hosted-model availability and free quotas depend on the selected project/account.

The current local workspace selects Google AI Studio again:
`COPILOT_PROVIDER=google` and `COPILOT_MODEL=gemma-4-26b-a4b-it`.
The first Q8 replacement report was prepared successfully through a live Gemma
26B call after the 31B route returned HTTP 500. See the
[Q8 demo runbook](docs/q8-demo.md) for the tested workflow and remaining limits.
The optional OpenRouter route remains available through its own key.
Earlier free OpenRouter Gemma checks returned 429; direct Google generation
returned 500. A later real heatmap request completed two Google calls successfully,
with 3,556 then 7,135 input tokens. The latest OpenRouter quota metadata showed
0/50 daily free requests used and 50 remaining, so an upstream 429 did not
establish daily exhaustion. This does not validate every evaluation scenario.
After the three-call and evidence-budget changes, the exact Southwest Block 24
containment question completed in **two Google calls**, with 3,109 then 7,197
input tokens and one generation HTTP attempt each. It returned both documented
breaches: WELL-0082 (7.1 hours) and WELL-0100 (8.1 hours), against SR-02's four-hour
limit. This live check used public challenge data.
See [implemented architecture and current limits](docs/implemented-architecture.md).

The data browser and imports work without a key. Real provider inference has to
be verified with your account; automated tests replace only the network boundary.

Each user request now makes **at most three model calls**, including its final
answer. The model can batch up to eight tool calls in a retrieval round; the last
model call has tools disabled and must answer or disclose missing evidence.
`get_spill_compliance` retrieves the applicable SR-02 rule, corrected spill
detections and linked containment-report evidence in one read, so this question
does not require separate model rounds for each lookup.

Transient provider/network failures are retried up to three times within each
inference request's timeout. Invalid keys and exhausted quotas are not retried.
If an explanation fails after successful queries, the app still displays the
returned facts, tables and sources, marked as a partial response. This fallback
does not infer a live status, an operational cause or a maintenance restart.
An initial transient error or429 can also trigger a local lookup for an exact
well ID, without another API request. Complex questions still need a working model.

## Context tokens and visual preview

Each Google model call first sends the complete generation input to `countTokens`:
system instructions, schema, dates, history, tool definitions, tool results and
any retrieved SOP text. The UI shows the latest input count, peak context and
number of model calls while generation is running, and saves those values with
the reply. Open **Context token details** for each call's source, status, output
tokens and actual generation HTTP attempts. Returned `usageMetadata` supersedes
the preflight count when available. Missing counts display **Unavailable**.
Preflight input is a tokenizer measurement, not evidence of billed usage or a
successful answer. The sum across rounds is not one context window.

Counting adds one separate Google tokenization request per model call, with a
10-second timeout and no automatic counting retries. Count failures do not block
generation. When a preflight count is available, inputs above the default
**12,000-token input budget** are rejected locally before generation. This bounds
a measured request; it does not pace calls against project TPM or enforce the
budget when counting is unavailable. One live static tokenization-only check returned 59 tokens; automated
tests use mocked HTTP responses and never consume generation quota.

The revised full system context measured **1,690 tokens**, down from 2,304
(**26.65%**), with identical reference dates and schema. Recent private context
keeps up to 16 stored messages within a combined 12,000-character budget,
prioritizing newer messages and marking omissions. The current user prompt is
preserved in full (up to the existing 20,000-character input limit), even if it
alone exceeds that character budget. Older stored chats remain searchable by date.
Model-bound tool evidence
is capped at 60 rows, 20 other list items and 6,000 characters per string, then
bounded to 12,000 characters per tool response and 24,000 across all tool responses
in one model request. Omitted evidence is marked; full retrieved rows and source
metadata remain available to the UI. These character bounds do not replace token
counting.

The **Visual preview** tab renders real imported data with preset SQL/tool reads,
without model calls or saving chat messages. Choose a status card, temperature
trend, downtime bar chart, corrected-spill heatmap, well map or inspection table,
then click **Show preview**. Only that button runs the preview query. Map tiles
may require network access; the model provider is not involved. This verifies
the visual/data path independently of free-model availability and does not
pretend to test natural-language planning.

## First iteration behavior

Fully specified correction messages (one inspection ID, one replacement field/value
and a reason) can prepare a validated review locally, without a model request.
For example, `Please correct inspection INS-0164-0205. Set wellhead temperature
to 41.0°C because the thermal gun was faulty.` prepares a correction proposal.
The same **Confirm shared update** action saves it; original readings remain.
Other phrasing uses the model. A failed or unprepared write explicitly says no
shared update was saved, rather than substituting an old production-status answer.
The trace shows `correct_inspection` / `pending_confirmation`, then `saved` after
confirmation. INSERT statements are application operations, not model-generated SQL.

- Imports all source records, nested equipment, manuals, SOPs, and repair reports
  into `data/copilot.sqlite3`, retaining original JSON and file/line provenance.
- Answers through bounded read tools, complete SQL aggregation, and document
  lookup/lexical search. The model never gets unrestricted database or file access.
- Derives production using each manual's imported temperature-difference rule.
  Counts downtime as 7 days per nonproducing weekly inspection.
- Applies the supplied formal false-spill corrections before spill statistics.
- Persists chats by dataset, selected user, and conversation. Shared notes and
  accepted inspection corrections are visible to other users of that dataset.
- Searches the selected user's private chats across conversations by date,
  literal keywords or well ID, with pagination beyond the latest 30 messages.
- Previews explicit shared updates before saving; original readings remain
  unchanged. Competing correction values are marked disputed.
- Tags reviewed typed corrections as sensor-reading corrections, AI detection
  feedback or AI estimate feedback. A tag does not authorize or accept a change.
- Displays source evidence and status cards, tables, charts, maps, or heatmaps.
  Visual data comes from tool results rather than model-invented chart rows.

The visible **User / crew member** picker above the chat lists identities from
the selected dataset. It is the challenge's demo identity mechanism, not production
authentication. New formal corrections require the reviewer to confirm that
they verified the value and are qualified. No unsupported role allowlist is
invented from the source roles.

## Try the sample

1. Ask: `Is WELL-0001 producing oil right now? Show its status card.`
2. Ask: `How many real oil spills has WELL-0001 had, and what is their total volume?`
3. Ask: `How many days was WELL-0001 down in 2025?`
   For report-based compliance, ask: `In Southwest Block 24, which spills in the
   last 90 days were not contained within the SOP time limit?`
4. Report: `FYI WELL-0001 is under emergency maintenance from now through 20 October 2026.`
5. Review the parsed well and dates, confirm the update, switch crew member, and
   ask about that well again. The new chat sees the shared report, while private
   conversation history stays with the first person.
6. In a new conversation for the same user, ask: `What were the three wells we
   discussed yesterday?`, `What did we discuss last week?`, or `Find my pump
   discussion about WELL-0001 from 1 through 15 October 2026.` Only saved messages
   for that selected user and dataset can be retrieved; empty history stays empty.

Set `COPILOT_AS_OF_DATE` in `.env` to control field-data analysis. The benchmark default is
16 October 2026. New relative-date reports use the runtime field-local date; the
field timezone defaults explicitly to UTC and can be changed with
`COPILOT_FIELD_TIMEZONE` in `.env`.
Date-only end dates are inclusive in this iteration. A passed planned end never
proves completion or a return to production.

Private-history dates also use the runtime field-local clock, independently of
the dataset's analysis date. `yesterday` means the complete preceding local day.
`this week` and `last week` mean Monday–Sunday calendar weeks. Explicit date
ranges include both named days. Python converts local midnight boundaries to
UTC and uses start-inclusive, end-exclusive intervals, including daylight-saving
changes. The model selects `search_my_conversations` and interprets the question;
the server fixes the user/dataset, resolves these standard periods, and applies
the filters. Each page has at most 30 messages, IDs, timestamps and private source
references. Cursor pages retain the original filters, window and message snapshot.
If the model reaches its request limit, it must disclose incomplete coverage.

Inspection correction tags describe the corrected field, not a learned policy.
For example, a verified `spill_est_volume_bbl` correction from 4.5 to 5.4 is
`ai_estimate_feedback`; a temperature correction is `sensor_reading_correction`.
The saved reason, author, original inspection and accepted/disputed status remain
linked. Existing free-text `operator_feedback` stays an attributed report; it is
not automatically accepted or converted into a typed correction. Type-wide AI
learning and general analysis/rule corrections remain future work.

## Import the test dataset

For the five-turn shared-memory demo on the 600-well test field, follow the
[Q8 demo runbook](docs/q8-demo.md). It includes the exact messages, user/session
switches, confirmation steps, expected counts and verification limits.

Use **Import dataset** in the sidebar for a ZIP or local folder. Both call the
same validated importer. Imports are additive and selectable; a failed import
does not remove a previous dataset. Identical source content is reused without
duplicating rows or discarding saved notes.

```powershell
python -m copilot.ingest --path challenge-dataset/sample-data --db data/copilot.sqlite3 --label "Sample field"
python -m copilot.ingest --path "C:\path\to\test-data.zip" --db data/copilot.sqlite3 --label "Evaluation field"
```

Dataset IDs scope records, notes, corrections, documents, and chats. This prevents
accidental collisions between packages containing the same well IDs. Automatic
merging or carrying notes into a new dataset version is not implemented; choose
the intended dataset explicitly. A ZIP may include an enclosing repository
folder. Archive extraction is bounded and rejects unsafe paths and symlinks.

## Code layout

| File | Responsibility |
| --- | --- |
| `app.py` | Streamlit chat, imports, identity picker, review, evidence and visuals |
| `copilot/db.py` | SQLite schema and connections |
| `copilot/ingest.py` | Validated atomic, idempotent folder/ZIP ingestion |
| `copilot/tools.py` | Domain rules, isolated SQL, shared notes, corrections, search |
| `copilot/agent.py` | Bounded model/tool loop, private chats, scoped update previews |
| `copilot/history.py` | Runtime calendar windows and exact UTC timestamp comparisons |
| `copilot/model.py` | Google and OpenRouter Gemma REST adapters |
| `copilot/config.py` | Local settings and environment configuration |
| `tests/` | Import, domain, provider, orchestration and Streamlit integration checks |

SQL runs against an in-memory copy of the selected dataset's public records with
a read-only authorizer and time/output limits. Private messages and other
datasets are not copied. Larger deployments can replace this data-access layer
without changing the UI. A graph backend and embeddings are not implemented in
this iteration. A graph would provide another retrieval tool; it would still
need to return evidence and respect corrections and dataset/user boundaries.

## Verify

```powershell
python -m unittest discover -s tests -v
```

Tests include known sample status, six corrected spills totaling 15.1 bbl for
WELL-0001, its 21 downtime days in 2025, field rankings, unseen manual thresholds,
failed-import rollback, ZIP handling, user/dataset isolation, and reviewed updates.
Private-history tests cover local date/year/week boundaries, DST, microseconds,
older-than-30 retrieval, stable pagination, literal/entity filters, isolated
recall through the model/tool loop, and Streamlit identity switching.
Network protocol tests use fake provider responses. They do not measure Gemma's
answer quality or prove account access. The complete set of 18 challenge
scenarios has not been validated end to end against a live model.
Configuration tests check file edits, real-environment precedence, key fallback
and omission of credentials from the settings representation.

## Iteration limits

- Shared runtime updates currently target one well or one inspection field.
  Type-wide learned rules and a dispute-resolution workflow remain future work.
- Qualified-person verification is a review attestation, not a production
  permissions system. Multi-user concurrent deployment needs further hardening.
- An interrupted process during shared-update confirmation can leave a proposal
  marked processing; crash recovery is not yet automated.
- Documents use lexical lookup, not embeddings or graph traversal. Manual rule
  extraction rejects unsupported wording rather than inventing a threshold.
- API latency, model tool adherence, availability and free quota need checking
  with the user's actual AI Studio key. Limits/errors are reported without
  exposing credentials.

## Data and design notes

The upstream challenge source is preserved in `challenge-dataset/`.

- [Complete source data map](docs/data-map.md)
- [Browsable schema explorer](docs/data-map.html)
- [Decision log](docs/decision-log.md)
- [Architecture](docs/architecture.md)
- [Implemented architecture and request flow](docs/implemented-architecture.md)
- [Interactive architecture walkthrough](docs/implemented-architecture.html)
- [Mermaid diagram source](docs/implemented-architecture.mmd)
- [Earlier architecture visual](docs/architecture-options.html)
- [Memory scenarios](shared-context-grill.html)
- [Q8 shared-memory demo runbook](docs/q8-demo.md)
- [Problem statement](challenge-dataset/01-problem-statement.md)
- [Dataset guide](challenge-dataset/02-dataset-guide.md)
- [Evaluation scenarios](challenge-dataset/03-evaluation-queries.md)
- [Upstream repository](https://github.com/FlytBaseAILabs/flytbase-ahc-swe-asset-intelligence-dataset)

Resolve at most five discussion questions at a time. User-confirmed choices and
remaining design proposals are kept separate in the decision log.
