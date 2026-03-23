# Ops / management refactor plan

Branch/worktree context:
- base branch: `moss/migration-check-20260320`
- planning branch: `moss/ops-management-plan`
- worktree: `/home/valentin/workspace/collar.fi/contracts-wt-ops-plan`

## Goal

Make `ops/management/*` follow the same architecture as the deploy flows:
- thin CLI wrappers
- shared runtime / signer / env helpers
- structured step execution
- reusable contract + API helpers
- smaller, readable keeper scripts

Best case: deploy and management scripts share the same foundational Python runtime pieces.

## Current state

### Good parts already present
- `ops/deploy_l1.py` and `ops/deploy_l2.py` are now thin wrappers.
- Shared logic lives in `ops/py_lib/deploy_engine.py`:
  - signer resolution
  - tx sending
  - artifact loading
  - deployment state persistence
  - verification
  - step planning/execution
- `ops/py_lib/envs.py`, `ops/py_lib/deployments.py`, `ops/py_lib/l2_discovery.py` already provide some common helpers.

### Main problems in management scripts
- `ops/management/l2_keeper_handle_messages.py` is too large (~1150 LOC) and mixes:
  - CLI parsing
  - env resolution
  - signer resolution
  - event scanning
  - state persistence
  - onchain tx sending
  - typed-data building
  - Derive API calls
  - local-anvil atomic execution logic
  - action routing / keeper workflow
- `ops/management/l1_keeper_handle_messages.py` duplicates keeper loop/state/cursor patterns in a separate style.
- `enable_collateral.py` and `set_l2_message_asset.py` duplicate a lot of:
  - env loading
  - signer auth checks
  - address resolution
  - dry-run/broadcast handling
  - current-vs-target diffing
- `ops/preflight.py` shells out to sibling scripts instead of calling shared library functions directly.
- Some shared helpers live in the wrong place for reuse:
  - `resolve_signer`, `ResolvedSigner`, tx sending live inside `deploy_engine.py`
  - keeper helpers live in `management/l2_common.py`
  - generic command helpers live in `lz_harness/common.py`

## Target architecture

## 1. Extract a shared ops runtime layer
Create a shared foundation under `ops/py_lib/` used by both deploy and management.

Proposed modules:
- `ops/py_lib/runtime.py`
  - root path helpers
  - `run()` / `require_cmd()`
  - JSON/file helpers
  - broadcast/dry-run helpers
- `ops/py_lib/signers.py`
  - move out of `deploy_engine.py`:
    - `SignerInput`
    - `ResolvedSigner`
    - `resolve_signer()`
  - shared signer/password handling for all scripts
- `ops/py_lib/chain.py`
  - `block_number`, receipt helpers, chain-id helpers
  - storage slot helpers where needed
- `ops/py_lib/contracts.py`
  - artifact loader / ABI helpers reused from deploy engine
  - `contract()`, `call()`, `send_contract_tx()` style helpers for management flows
- `ops/py_lib/output.py`
  - consistent JSON summary + rich output formatting

Result:
- deploy engine depends on these shared modules
- management scripts also depend on them
- no more duplicated signer/env/run logic across script families

## 2. Introduce a generic operation engine for day-2 scripts
Create something parallel to `deploy_engine.py`, but lighter-weight.

Proposed module:
- `ops/py_lib/operation_engine.py`

Core concepts:
- `OperationRuntime`
  - rpc url
  - env
  - broadcast flag
  - resolved signers
  - output/state paths
  - web3 + artifact access
- `OperationStep`
  - `name`
  - `precondition`
  - `check()` -> current state / diff
  - `apply()` -> optional tx(s)
  - `postcondition`
- `OperationSummary`
  - planned steps
  - executed steps
  - current vs target
  - tx hashes

Use cases:
- `enable_collateral.py`
- `set_l2_message_asset.py`
- future admin/config scripts

This would mirror the deploy style:
- dry-run shows plan
- broadcast applies steps
- JSON output is consistent

## 3. Add a keeper framework instead of monolithic scripts
Create reusable keeper-specific building blocks.

Proposed modules:
- `ops/py_lib/keeper_state.py`
  - load/save cursor state
  - state schema helpers
  - idempotency helpers
- `ops/py_lib/keeper_loop.py`
  - polling loop
  - scan range calculation
  - safe cursor advancement rules
  - `--once` vs loop mode
- `ops/py_lib/keeper_logs.py`
  - log scanning / parsing helpers
  - chronological ordering helpers (`blockNumber`, `logIndex`, tx hash)
- `ops/py_lib/keeper_actions.py`
  - typed action/result objects
  - structured item/result format shared across keepers

Then split workflows into small handlers:
- `ops/management/handlers/l2_deposit_intent.py`
- `ops/management/handlers/l2_return_request.py`
- `ops/management/handlers/l2_rfq_trade.py`
- `ops/management/handlers/l1_finalize_loan.py`

Result:
- keeper main scripts become orchestration only
- action-specific business logic lives in handler modules
- new keeper actions are easier to add without growing a 1k+ line file

## 4. Separate Derive/RFQ integration from keeper control flow
The RFQ/Derive logic should live in a dedicated integration layer.

Proposed modules:
- `ops/py_lib/derive_client.py`
  - `public/deposit_debug`
  - `private/deposit`
  - `public/withdraw_debug`
  - `private/withdraw`
  - `private/execute_quote`
  - auth headers / retries / retryable error classification
- `ops/py_lib/tsa_actions.py`
  - nonce/expiry derivation
  - typed-data hash resolution
  - action tuple formatting
  - ABI encoding helpers
- `ops/py_lib/rfq_flow.py`
  - build RFQ execute-quote payloads
  - submit via TSA or atomic executor
  - record trade execution + trade confirmation sequence

Result:
- `l2_keeper_handle_messages.py` no longer owns API details or action encoding
- easier to test the RFQ flow independently

## 5. Unify env + address resolution everywhere
Consolidate all address resolution into reusable helpers.

Extend / reorganize:
- `ops/py_lib/envs.py`
- `ops/py_lib/deployments.py`
- maybe add `ops/py_lib/address_book.py`

Standard resolution order per script/action:
1. explicit CLI override
2. env value
3. deployment output json
4. onchain discovery (if deterministic and cheap)

This should replace one-off local helpers like:
- `_read_addr_from_output`
- `_default_output_json`
- `_resolve_receiver_addr`
- `_resolve_atomic_executor_addr`
- ad hoc vault/messenger resolution in small scripts

## 6. Make preflight a library, not a subprocess wrapper
`ops/preflight.py` should call Python functions directly instead of spawning sibling scripts.

Proposed modules:
- `ops/py_lib/preflight_checks.py`
  - recipient check
  - peer check
  - asset mapping check
  - pending-message scan check
  - ULN config check

Then `ops/preflight.py` becomes a thin CLI like deploy scripts.

Benefits:
- no subprocess coupling
- easier testability
- easier composition in keeper/deploy scripts

## 7. Standardize CLI UX across ops scripts
Adopt the same conventions everywhere:
- `--env testnet|mainnet`
- explicit env file override args
- `--broadcast`
- `--json`
- common signer args:
  - `--private-key`
  - `--from`
  - `--unlocked`
  - optional named role signers where relevant
- consistent summary keys in JSON output:
  - `mode`
  - `broadcast`
  - `signers`
  - `resolvedAddrs`
  - `steps`
  - `executedSteps`
  - `stateFile` / `cursor`

## 8. Migration order

### Phase 1 — foundation extraction
1. Move signer/runtime primitives out of `deploy_engine.py` into shared modules.
2. Keep deploy scripts behavior unchanged except imports.
3. Add tests for signer resolution / runtime helpers.

### Phase 2 — small management scripts first
Convert these first because they are simple and give quick wins:
- `ops/management/enable_collateral.py`
- `ops/management/set_l2_message_asset.py`
- `ops/management/l1_message_preflight.py`

Target outcome:
- thin CLI wrappers
- one-step or two-step `OperationStep` plans
- shared signer/address resolution

### Phase 3 — preflight unification
- move checks from subprocess-based `ops/preflight.py` into `py_lib/preflight_checks.py`
- keep CLI output the same

### Phase 4 — keeper framework
- extract keeper loop/cursor/log scanning primitives
- refactor `l1_keeper_handle_messages.py` to use them first (smaller script)
- then refactor `l2_keeper_handle_messages.py`

### Phase 5 — L2 keeper split
Break `l2_keeper_handle_messages.py` into:
- `handlers/`
  - deposit intent
  - return request
  - RFQ post-fill / trade confirmed
- `derive_client.py`
- `tsa_actions.py`
- `keeper_loop.py`
- thin CLI wrapper

### Phase 6 — converge deploy + management patterns
- if the operation engine works well, align deploy and management summaries/interfaces even more
- possibly rename `deploy_engine.py` internals into more generic runtime modules

## 9. Recommended first concrete implementation slice

If we want the best leverage with low risk, start with this PR sequence:

### PR 1
Extract shared modules from deploy engine:
- `py_lib/signers.py`
- `py_lib/runtime.py`
- minimal import rewiring in deploy scripts/engine

### PR 2
Refactor `enable_collateral.py` and `set_l2_message_asset.py` to use a new `OperationRuntime`

### PR 3
Refactor `preflight.py` to direct library calls

### PR 4
Create keeper framework and migrate `l1_keeper_handle_messages.py`

### PR 5
Split `l2_keeper_handle_messages.py` into handler modules + derive client + typed action helpers

## 10. Non-goals / caution
- Do not rewrite all scripts from `cast_*` to `web3` at once.
  - Keep `cast wallet sign` where it is the most pragmatic path.
  - Migrate tx sending/calls only where it materially improves readability.
- Keep CLI compatibility where possible.
- Avoid mixing behavioral changes with structural refactors.
- Preserve current dry-run safety defaults.

## 11. Success criteria
- keeper scripts are <300 LOC each at the top level
- reusable helpers own signer/env/address/state logic
- `ops/preflight.py`, deploy scripts, and management scripts all look structurally similar
- adding a new keeper action means adding a new handler module, not extending a monolith
- JSON outputs are consistent enough for automation to consume across all ops tools
