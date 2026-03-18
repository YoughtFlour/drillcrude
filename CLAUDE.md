# CRUDE Driller - Project Context

## What is this
Automated bot for drilling $CRUDE tokens on drillcrude.com. The bot solves text-analysis challenges from a coordinator API, submits artifacts, and earns credits that convert to $CRUDE tokens on-chain.

## Architecture
Single Python asyncio script (`crude_driller.py`) with 3 parallel loops:
- **Drilling loop** — auth -> get challenge -> solve -> submit -> post receipt on-chain -> repeat (~1 sec/cycle)
- **Claim loop** — every 30 min checks epochs and claims earned $CRUDE
- **Monitor loop** — every 5 min logs stats (solve rate, credits, ETH balance)

## How challenges work
1. Coordinator gives a document with ~8 company paragraphs (name, employees, founded year, revenue, margin)
2. One question like "Which company has the highest revenue?"
3. One constraint like "Take employee count, output: employees mod 7"
4. Bot must return the computed artifact string

## Solver strategy (current: v6.0)
- **Deterministic solver** (no LLM needed for most challenges):
  - Regex-parses document paragraphs to extract company data
  - Classifies question type (highest/lowest revenue/employees/founded/margin)
  - Computes artifact locally in Python (mod, letter positions, first_letters, reversed, etc.)
- **LLM fallback** (GLM-5 via ZAI SDK, free) for unknown question types
- Hit rate: ~65-70% deterministic, ~40% with LLM

## Known issues / weak spots
- `revenue_mod` — ~5% hit rate. Root cause: coordinator generates documents with DUPLICATE data (two companies with identical revenue/employees/founded). Tiebreaker (first in document) doesn't always match coordinator's logic.
- `letter_positions` — uses name WITHOUT spaces. Coordinator confirmed: spaces stripped from canonical name for indexing.
- `every_nth` — some rejections, needs investigation
- `Receipt FAILED: in-flight transaction limit` — on-chain receipts can't keep up with 1 sec drill cycle. Non-blocking, credits still count.

## Config (.env)
- `BANKR_API_KEY` — Bankr wallet API key
- `DRILLER_ADDRESS` — wallet address (auto-resolved if empty)
- `LLM_BACKEND` — `zai` (free GLM-5) or `openrouter` (paid, any model)
- `LLM_MODEL` — model name (e.g. `glm-5`, `openai/gpt-4o-mini`)
- `ZAI_API_KEY` / `OPENROUTER_API_KEY` — API keys
- `COORDINATOR_URL` — coordinator API base URL

## Staking tiers
- Wildcat (25M) — shallow wells, 1 credit/solve
- Platform (50M) — + medium wells, 2 credits/solve (CURRENT)
- Deepwater (100M) — all wells, 3 credits/solve

## User's setup
- Wallet: 0x04b906d694d0b2bc0fb6be43189af018cd861686
- Staked: 50M (Platform tier)
- Running on Windows (E:\Bot\driller), PowerShell
- Free API via ZAI (GLM-5)
- GitHub repo: https://github.com/Anda4ka/drillcrude

## Key files
- `crude_driller.py` — main script
- `.env` — config (not in git)
- `crude_state.json` — persistent state (solves, credits, epochs)
- `crude_debug.log` — verbose debug log (challenges, parsed data, results)
- `crude_driller.log` — main operational log

## Git workflow
- Only push when user explicitly says "push" / "commit"
- Never include API keys or .env in commits
