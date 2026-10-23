# Q8 demo: Who told you that?

This runbook follows the five messages in
[the test dataset's final questions](../challenge-dataset/test-data/final-questions.md#q8-who-told-you-that).
It demonstrates shared operational reports, private preferences across conversations,
and the difference between a direct report and hearsay.

## Prepare the app

1. Follow the [quick start](../README.md#quick-start), including the test-field
   settings. Start the app and open `http://127.0.0.1:8501/`.
2. In the sidebar's **Import dataset** panel, import
   `challenge-dataset/test-data/dataset` with label **Test field · Jan 2027** and
   select it. Keep this dataset selected throughout all five messages.
3. Open the **Copilot** tab. Use **User / crew member** to switch identities and
   **＋ New conversation** whenever the steps below require a new session.

The test dataset contains 600 wells and defines operational “today” as
**15 January 2027**. Its analysis date and `COPILOT_REPORT_DATE=2027-01-15`
make “yesterday” in turn 1 mean **14 January 2027**. Private conversation-history
dates still use the real clock in the configured field timezone. They are not
shifted into January 2027.

**Historical provider check (not the current OpenRouter-only route):**

The local Google route was configured with `gemma-4-26b-a4b-it` after the 31B
route returned HTTP 500 errors. The first write below was verified with a real
Gemma 26B response. Model availability can vary; a provider error does not save
an operational update.

Start with a clean Q8 operational state: before Nathan's replacement is confirmed,
the public dataset yields **9 recalled pumps**. Starting a new conversation does
not reset shared reports or private preferences. If you have already confirmed
this demo's reports, the list will reflect those saved reports, and repeating the
messages will not recreate the original baseline. Preserve prior submissions;
use a separate clean database when a fresh baseline is required.

## 1. Nathan reports a replacement

Select **Nathan Pierce** and start a **new conversation**. Send exactly:

> WELL-0333's recalled pump was swapped out yesterday. The work order isn't in the system yet. Please count it as replaced from now on.

The model should call `record_equipment_replacement` and prepare a
**Review shared update** proposal. Check that it names **WELL-0333**, reporter
**Nathan Pierce**, effective date **2027-01-14**, and evidence basis
**direct_report**. Click **Confirm shared update**.

Before confirmation, no shared replacement is saved. After confirmation,
WELL-0333 is excluded from the current recall list: **9 → 8 pumps**. The reason
is Nathan's attributed replacement report. The app must not invent a work order
or claim independent verification, and the original maintenance records remain
unchanged. The trace changes from `pending_confirmation` to `saved`.

## 2. Nathan saves private formatting preferences

Stay as **Nathan Pierce in the same conversation**. Send exactly:

> Also, just for me: whenever you list wells, sort them by area and then by well ID, and show costs in thousands of dollars.

The model should call `save_my_preferences`. These two display preferences save
immediately for Nathan, with **saved_private** in the trace; no shared-update
confirmation is required. The source is Nathan's own instruction in this chat.
The preferences apply to Nathan's later conversations in this dataset and remain
private to him.

## 3. Robert requests the field-wide recall table

Switch to **Robert Hayes** and start a **new conversation**. Send exactly:

> Which recalled pumps are still in the ground right now?

Expect a **table of 8 pumps**, with WELL-0333 excluded. WELL-0577 is still listed.
The sources should distinguish the supplier recall notice, the latest completed
installation reports, and Nathan's shared report explaining the exclusion.
Robert must not receive Nathan's private conversation or formatting preferences.
The table represents the latest supported installation state as of 15 January
2027, with user reports identified as such.

## 4. Olivia supplies a secondhand report

Switch to **Olivia Grant** and start a **new conversation**. Send exactly:

> Nathan told me the recalled pump on WELL-0577 was swapped as well. Take it off the recall list.

Review the proposed shared update. It must name **Olivia Grant as the reporter**,
**Nathan Pierce as the claimed source**, and evidence basis **hearsay**.
Click **Confirm shared update**.

The app saves an **unconfirmed** report. It must **retain WELL-0577 on the recall
list**, leaving the field-wide count at **8**. Olivia's statement is evidence
that Olivia made this report; it does not establish that Nathan directly reported
the replacement to the copilot. A direct confirmation or work order is still
needed to remove this pump.

## 5. Nathan returns in a new conversation

Switch back to **Nathan Pierce** and start a **new conversation**. Send exactly:

> Show me the recalled pumps that are still in the ground in my area, and what it cost to install each one.

Expect a **table of 3 pumps** scoped to Nathan's current production team,
**TEAM-NW**, sorted by **area, then well ID**. WELL-0333 stays excluded. Nathan's
saved cost preference applies automatically:

- **WELL-0305:** 24.528 thousand USD (original total: $24,528).
- **WELL-0426:** 24.044 thousand USD (original total: $24,044).
- **WELL-0428:** 27.074 thousand USD (original total: $27,074).

Costs come from the original installation maintenance jobs, divided by 1,000;
they are the installation-job totals, not a fabricated pump-only price. The
recall notice and installed-parts reports establish the affected equipment.
Nathan's current team establishes “my area,” his own prior instruction establishes
the presentation preferences, and shared replacement reports explain exclusions
or unresolved claims. Each fact should retain its source.

## What has been verified

All five messages pass the offline integration workflow in
[test_q8_demo.py](../tests/test_q8_demo.py), using real public test data, actual
SQLite tools and confirmation writes, and scripted model tool selections. Those
tests also verify that hearsay cannot be staged as a direct report, a read
question cannot authorize preference writes, other users do not inherit Nathan's
private preferences, and original maintenance records remain unchanged.

The live-provider check described below belongs to the historical Google route; no live OpenRouter request was made for this upgrade.

Only the **first write** has been verified through a live Gemma 26B call. The
offline tests do not prove that every live model response will select the correct
tool. The complete set of 11 final questions has not been tested end to end.

If a write returns an error or **not_saved**, stop that demo sequence: no shared
fact was recorded. Retry until the correct proposal appears, inspect it, and
confirm it before switching users. Read queries do not execute `INSERT` SQL;
the model selects a typed write tool and Python performs the reviewed write.
