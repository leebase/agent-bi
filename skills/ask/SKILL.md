---
name: agent-bi-ask
description: Answer a data question with agent-bi by trying the verified question path first, then the deterministic resolver (no LLM), and composing a SemanticQuery only when the resolver cannot; never writes SQL.
---

# Ask agent-bi a question

You are the operator's harness agent. agent-bi never lets a model produce a number: every
number in an answer comes from `agent-bi ask`. The deterministic resolver (`agent-bi resolve`)
turns a question into a query whenever it can; your only probabilistic job is composing a
**SemanticQuery JSON** (names of model objects, no SQL), and only when the resolver reports
the question is outside its grammar. Work from the repository root (where `agent-bi.yaml` is).

The model name `<m>` is the stem of the file in `semantic/` (for example
`semantic/idle_warehouse_waste.yaml` -> `idle_warehouse_waste`). If there is more than one
model and the user did not say which, ask which.

## Step 1 - always first: the verified path (no LLM)

Run, with the user's question **verbatim** (do not rephrase it):

    agent-bi ask --model <m> --question "<the user's question>"

- **Exit 0** -> present the answer (see "Presenting an answer"). You composed nothing; stop.
- **Exit 2, `E_AMBIGUOUS_QUESTION`** -> show the saved-query names listed in the error, ask
  the user to pick one, then run `agent-bi ask --model <m> --query-file queries/<name>.json`.
- **Exit 4, `E_NO_VERIFIED_MATCH`** -> a normal outcome. Continue to step 2.
- Any other exit -> show the error line (`E_...`) to the user; do not work around it.

You must not skip steps 1 and 2 and must not compose anything before both have returned.

## Step 2 - resolve (only after step 1 exit 4)

Run `agent-bi resolve` with the question **verbatim**. The resolver is deterministic code: it
makes no model call and never guesses. This skill carries no resolution rules of its own;
do not work out bindings, ambiguities or grains yourself.

    agent-bi resolve --model <m> --question "<the user's question>"

- **Exit 0, `RESOLVED`** -> run `agent-bi ask --model <m> --query-file out/resolution.json`.
  No model composed the query. Present the answer.
- **Exit 5, `AMBIGUOUS`** -> show each ambiguity's candidates with their descriptions, ask
  the **user** to pick, and re-run `resolve` with all of the user's picks so far plus the new
  one (`--choose <id>=<their pick>` for each). Repeat until it exits 0 or 6.
  **Never pick for the user.**
  - **Exit 2, `E_BAD_CHOICE`** -> drop every pick for each id the error line names (an earlier
    date/grain pick can be invalidated by a later word pick); for `malformed`, drop that
    argument; then ask the user again. The call wrote no resolution.
- **Exit 6, `UNRESOLVED`** -> the question is outside the deterministic grammar. Only now may
  you compose a SemanticQuery proposal (the one probabilistic step) to `out/question.json`,
  using `out/resolution.json`'s `bindings` and `unresolved` as context, then run
  `agent-bi ask --model <m> --query-file out/question.json`. If `ask` returns
  `E_UNKNOWN_FIELD`, retry **once** with the corrected name from the error (the message lists
  the valid names), then stop and show the error. Tell the user the query was composed by
  you; the interpretation line shows what was asked.
- Any other exit -> show the error line (`E_...`) to the user; do not work around it.

Rules for a composed JSON (exit 6 only): `schema_version` `semantic-query/1.0`; `model` and
`table` as in `semantic/<m>.yaml`; `metrics`, `dimensions`, optional `time` {`dimension`,
`grain`, `start`, `end`}, `filters`, `named_filters`, `order_by`, `limit` exactly as
`agent-bi contract --print` says (also committed as `docs/semantic-query-contract.md`). Leave
`order_by` empty and `limit` out unless the user asked for an order or a cut-off. **You must
not write SQL, a Rill file, or a number**; nothing you write is executable text.

## Presenting an answer

`agent-bi ask` prints the interpretation, a `Definition:` line, a `Serving:` line with the
exclusions line under it, `published:`, `definitions:`, the compiled SQL, the rows, and the paths
it wrote. Relay it faithfully:

- State the numbers exactly as the table shows them. **Never compute, estimate or round a
  number yourself.**
- Always quote the `Definition:` and `Serving:` lines verbatim, both, every time, together with
  the `Not covered: ...` line. **Never summarise them into one word or a single verdict**
  (not "trusted", "verified", "good", "fine", "live"): they are two separate facts and neither
  implies the other.
  - `Definition` says what was accepted and independently verified for this query
    (`governed` = a known answer exists and every definition was accepted by a human;
    `verified` = a known answer exists but some definitions are not yet accepted, and the line
    names them; `stale` = the definitions changed since the verification; `none` = no verified
    query matches - say plainly that the number is not verified).
  - `Serving` is a dated record of one past check of what Rill returned through one route. It
    says nothing about any time outside the quoted interval. Do not say Rill "is serving" or
    "still" returns anything. To learn what Rill returns now, the user runs
    `agent-bi verify --model <m> --engine rill`; run it only when asked. `agent-bi status
    --model <m>` prints the same two lines for the whole model without calling Rill.
- If the user wants the answer kept, `agent-bi save --from out/<hash>.answer.json --name <n>`
  (add `--add-to-canvas <canvas>` to place it on a forked canvas). `save --verify` and
  `enrich --accept` are decisions for the human: run them only when the user asks, with the
  user's own name for `--by`, and never accept a definition on your own.
