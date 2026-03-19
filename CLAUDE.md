# CRUDE Driller - Project Context

## What is this
Automated bot for drilling $CRUDE tokens on drillcrude.com (Base chain). The bot solves text-analysis challenges from a coordinator API, submits artifacts, and earns credits that convert to $CRUDE tokens on-chain.

## Architecture
Single Python asyncio script (`crude_driller.py`) with parallel loops:
- **Drilling loop** — auth -> pick best site -> get challenge -> solve deterministically -> submit -> repeat (~1 sec/cycle, no artificial delay)
- **Receipt poster** — background task, posts on-chain receipts throttled (1 per 10 sec, drains stale queue). Receipt failures are non-blocking — credits are awarded by coordinator on submit, not on receipt.
- **Claim loop** — every 30 min checks epochs and claims earned $CRUDE
- **Monitor loop** — every 5 min logs stats (solve rate, credits, ETH balance)

## How challenges work
1. Coordinator gives a document with ~8 company paragraphs (name, employees, founded year, revenue, margin)
2. One question like "Which company has the highest revenue?"
3. One constraint like "Take employee count, output: employees mod 7"
4. Bot must return the computed artifact string

## Solver strategy (current: v6.2)
- **Deterministic solver** (no LLM needed for ~95% of challenges):
  - Regex-parses document paragraphs to extract company data
  - Classifies question type (highest/lowest revenue/employees/founded/margin)
  - Computes artifact locally in Python (mod, letter_positions, first_letters, reversed, every_nth, margin_mul, etc.)
  - **Alternate retry**: if primary answer rejected, tries alternate candidates (duplicate companies, with/without spaces variants)
- **LLM fallback** (GLM-5 via ZAI SDK, free) for unknown question/constraint types
- **Hit rate**: ~73% overall (v6.2 session: 4347/5962)

## Constraint types supported (local compute)
| Type | Example | Hit rate |
|---|---|---|
| `employees_mod` | `employees mod 7` → `5874 % 7 = 1` | ~90%+ |
| `founding_mod` | `founding_year mod 19` → `1995 % 19 = 0` | ~85% |
| `revenue_mod` | `revenue_millions mod 13` → `8500 % 13 = 5` | ~50% (weak — duplicate data issue) |
| `margin_mul` | `margin × 3` → `18 * 3 = 54` | ~85% |
| `first_letters` | `Blue Mesa Logistics` → `BML` | ~80% |
| `first_n_reversed` | `Summit` (4 chars) → `mmuS` | ~80% |
| `letter_positions` | `positions 2,5,8` → extract chars | ~75% |
| `every_nth` | `every 3rd letter` → extract | ~60% (needs work) |

## Known issues / weak spots
- `revenue_mod` — ~50% hit rate. Root cause: coordinator generates documents with DUPLICATE data (two companies with identical revenue/employees). Alt-retry with second candidate helps but doesn't fully solve. Possibly coordinator rounds/parses revenue differently.
- `every_nth` — ~60% hit rate, needs investigation on edge cases
- `first_n_reversed` / `first_letters` — sometimes coordinator uses different word boundaries (e.g. "Co" vs "Co." as separate word). Alt-retry covers some cases.
- LLM fallback (GLM-5) quality is poor — often returns reasoning text instead of just the answer. `validate_artifact()` catches and rejects these.

## Stale drill handling (v6.2)
When bot restarts or LLM times out, a drill may be left "active" on coordinator side:
1. If stale drill has challengeId + doc → solve it properly (earn credits!)
2. If stale drill has challengeId but no doc → dummy-submit to close it
3. If no challengeId → escalating wait (30s→120s), bail after 5 attempts
4. Before any `continue` without submit (bad artifact, reasoning markers) → dummy-submit to close drill

## Log rotation
Logs auto-rotate when >10 MB — keeps last 5 MB. Check every ~200 writes.
- `crude_driller.log` — main operational log (accepts, gushers, warnings)
- `crude_debug.log` — verbose (receipt fails, depleted sites, parser details, stale drills)

## Receipt poster (v6.2)
Receipts are on-chain confirmations of solved challenges. They do NOT affect credit earnings.
- Throttled: 1 receipt per 10 seconds
- Queue drain: if multiple receipts queued, only latest is posted
- In-flight errors go to debug log (not main log)

## Config (.env)
- `BANKR_API_KEY` — Bankr wallet API key (required)
- `DRILLER_ADDRESS` — wallet address (auto-resolved if empty)
- `DRILLER_DEBUG` — `true` for verbose debug logging
- `LLM_BACKEND` — `zai` (free GLM-5) or `openrouter` (paid, any model)
- `LLM_MODEL` — model name (e.g. `glm-5`, `openai/gpt-4o-mini`)
- `ZAI_API_KEY` / `OPENROUTER_API_KEY` — API keys for LLM fallback
- `COORDINATOR_URL` — coordinator API base URL

## Staking tiers
- Wildcat (25M) — shallow wells, 1 credit/solve
- Platform (50M) — + medium wells, 2 credits/solve (CURRENT)
- Deepwater (100M) — all wells, 3 credits/solve

## Site types
Sites have different richness levels affecting credit multipliers:
- **standard** — base credits (2 for Platform)
- **rich** — 4× credits (8 for Platform)
- **bonanza** — 5× credits (10 for Platform)
- Sites deplete over time (shown as %) and regenerate. Bot auto-picks best available.
- Gushers: random bonus multiplier (gusher = 3×, mega-gusher = 10×)

## User's setup
- Wallet: 0x04b906d694d0b2bc0fb6be43189af018cd861686
- Staked: 50M (Platform tier)
- Running on Windows (E:\Bot\driller), PowerShell
- Free API via ZAI (GLM-5)
- GitHub repo: https://github.com/Anda4ka/drillcrude

## Key files
- `crude_driller.py` — main script (~1600 lines)
- `.env` — config (not in git)
- `crude_state.json` — persistent state (solves, credits, epochs)
- `crude_debug.log` — verbose debug log
- `crude_driller.log` — main operational log
- `claim_now.py` — manual claim script

## Version history
- v6.0 — Deterministic solver (no LLM for 95% of challenges)
- v6.1 — Alternate retry on rejection + space handling fixes
- v6.2 — Receipt throttling, stale drill fixes, log rotation, clean logs

## Git workflow
- Only push when user explicitly says "push" / "commit"
- Never include API keys or .env in commits
- Commit messages in English, conversation in Russian
