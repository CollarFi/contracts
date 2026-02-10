import { type Hex, decodeEventLog, encodeFunctionData, parseAbiItem } from "viem";
import { loadConfig } from "./config.js";
import { makeClients } from "./clients.js";
import {
  CollarTSAReceiverAbi,
  CollarVaultAbi,
  CollarVaultMessengerAbi
} from "./abi.js";
import { newState, type Hex32 } from "./state.js";

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function toHex32(x: Hex): Hex32 {
  return x as unknown as Hex32;
}

// Minimal decoding for LayerZero message events.
// We rely on the messenger/receiver storing decoded messages in mappings,
// then read them back via `receivedMessage(guid)`.
const L1_MessageReceived = parseAbiItem(
  "event MessageReceived(bytes32 indexed guid, uint8 action, uint256 indexed loanId)"
);

const L2_MessageReceived = parseAbiItem(
  "event MessageReceived(bytes32 indexed guid, uint8 action, uint256 indexed loanId)"
);

async function main() {
  const cfg = loadConfig();

  const l1 = cfg.L1_KEEPER_PK
    ? makeClients(cfg.L1_RPC_URL, { privateKey: cfg.L1_KEEPER_PK as Hex })
    : makeClients(cfg.L1_RPC_URL, { mnemonic: cfg.ANVIL_MNEMONIC, addressIndex: cfg.L1_ACCOUNT_INDEX });

  const l2 = cfg.L2_KEEPER_PK
    ? makeClients(cfg.L2_RPC_URL, { privateKey: cfg.L2_KEEPER_PK as Hex })
    : makeClients(cfg.L2_RPC_URL, { mnemonic: cfg.ANVIL_MNEMONIC, addressIndex: cfg.L2_ACCOUNT_INDEX });

  const state = newState(cfg.L1_START_BLOCK, cfg.L2_START_BLOCK);

  console.log("keeper starting", {
    l1: { rpc: cfg.L1_RPC_URL, vault: cfg.L1_COLLAR_VAULT, messenger: cfg.L1_LZ_MESSENGER, start: state.l1Block },
    l2: { rpc: cfg.L2_RPC_URL, receiver: cfg.L2_TSA_RECEIVER, start: state.l2Block }
  });

  // main loop
  // - Poll L2 receiver MessageReceived events and call handleMessage(guid)
  // - Poll L1 messenger MessageReceived and track guids by loanId
  // - If we have DepositConfirmed + TradeConfirmed for a loan and a mandate exists, attempt finalizeLoan
  for (;;) {
    try {
      await tickL2(state, cfg, l2.publicClient, l2.walletClient);
      await tickL1(state, cfg, l1.publicClient);
      await tryFinalizeLoans(state, cfg, l1.publicClient, l1.walletClient);
    } catch (e) {
      console.error("tick error", e);
    }

    await sleep(cfg.POLL_MS);
  }
}

async function tickL2(state: ReturnType<typeof newState>, cfg: ReturnType<typeof loadConfig>, publicClient: any, walletClient: any) {
  const latest = await publicClient.getBlockNumber();
  if (latest < state.l2Block) return;

  const fromBlock = state.l2Block;
  const toBlock = latest;

  const logs = await publicClient.getLogs({
    address: cfg.L2_TSA_RECEIVER,
    event: L2_MessageReceived,
    fromBlock,
    toBlock
  });

  state.l2Block = toBlock + 1n;

  for (const log of logs) {
    const guid = toHex32(log.topics[1] as Hex);
    state.l2UnhandledGuids.add(guid);
  }

  // Handle at most N per tick to avoid gas spikes
  const MAX_PER_TICK = 5;
  let handled = 0;

  for (const guid of Array.from(state.l2UnhandledGuids)) {
    if (handled >= MAX_PER_TICK) break;

    const alreadyHandled: boolean = await publicClient.readContract({
      address: cfg.L2_TSA_RECEIVER,
      abi: CollarTSAReceiverAbi,
      functionName: "handledMessages",
      args: [guid]
    });

    if (alreadyHandled) {
      state.l2UnhandledGuids.delete(guid);
      continue;
    }

    // Attempt handleMessage. If it reverts because socket not finalized etc, keep it queued.
    try {
      const hash = await walletClient.writeContract({
        address: cfg.L2_TSA_RECEIVER,
        abi: CollarTSAReceiverAbi,
        functionName: "handleMessage",
        args: [guid],
        value: cfg.L2_MAX_VALUE_WEI
      });
      console.log("l2 handleMessage sent", { guid, hash });
      handled++;
      // don't delete immediately; wait for it to flip handledMessages=true on next tick
    } catch (e: any) {
      // keep it queued; log the short error
      console.warn("l2 handleMessage failed (will retry)", { guid, err: String(e?.shortMessage || e?.message || e) });
    }
  }
}

async function tickL1(state: ReturnType<typeof newState>, cfg: ReturnType<typeof loadConfig>, publicClient: any) {
  const latest = await publicClient.getBlockNumber();
  if (latest < state.l1Block) return;

  const logs = await publicClient.getLogs({
    address: cfg.L1_LZ_MESSENGER,
    event: L1_MessageReceived,
    fromBlock: state.l1Block,
    toBlock: latest
  });

  state.l1Block = latest + 1n;

  for (const log of logs) {
    // Decode indexed guid and loanId
    const guid = toHex32(log.topics[1] as Hex);
    const loanId = BigInt(log.topics[2] as any);

    // Read full stored message from messenger
    const msg: any = await publicClient.readContract({
      address: cfg.L1_LZ_MESSENGER,
      abi: CollarVaultMessengerAbi,
      functionName: "receivedMessage",
      args: [guid]
    });

    const action = lzActionName(msg.action);

    if (action === "DepositConfirmed") state.depositConfirmedGuidByLoan.set(loanId, guid);
    if (action === "TradeConfirmed") state.tradeConfirmedGuidByLoan.set(loanId, guid);
    if (action === "CollateralReturned") state.collateralReturnedGuidByLoan.set(loanId, guid);
    if (action === "SettlementReport") state.settlementReportGuidByLoan.set(loanId, guid);

    console.log("l1 message received", { guid, loanId: loanId.toString(), action });
  }
}

function lzActionName(n: bigint): string {
  // CollarLZMessages.Action enum order in Solidity:
  // DepositIntent=0, ReturnRequest=1, SettlementReport=2,
  // DepositConfirmed=3, CollateralReturned=4, TradeConfirmed=5, MandateCreated=6
  switch (Number(n)) {
    case 0:
      return "DepositIntent";
    case 1:
      return "ReturnRequest";
    case 2:
      return "SettlementReport";
    case 3:
      return "DepositConfirmed";
    case 4:
      return "CollateralReturned";
    case 5:
      return "TradeConfirmed";
    case 6:
      return "MandateCreated";
    default:
      return `Unknown(${n})`;
  }
}

async function tryFinalizeLoans(state: ReturnType<typeof newState>, cfg: ReturnType<typeof loadConfig>, publicClient: any, walletClient: any) {
  // naive: if both guids exist for a loan, and loan not yet opened, attempt finalize
  for (const [loanId, depositGuid] of state.depositConfirmedGuidByLoan.entries()) {
    const tradeGuid = state.tradeConfirmedGuidByLoan.get(loanId);
    if (!tradeGuid) continue;

    const loan: any = await publicClient.readContract({
      address: cfg.L1_COLLAR_VAULT,
      abi: CollarVaultAbi,
      functionName: "loans",
      args: [loanId]
    });

    // LoanState enum: NONE=0
    if (Number(loan.state) !== 0) continue;

    const pending: any = await publicClient.readContract({
      address: cfg.L1_COLLAR_VAULT,
      abi: CollarVaultAbi,
      functionName: "pendingDeposits",
      args: [loanId]
    });

    if (pending.borrower === "0x0000000000000000000000000000000000000000") continue;

    const mandate: any = await publicClient.readContract({
      address: cfg.L1_COLLAR_VAULT,
      abi: CollarVaultAbi,
      functionName: "mandates",
      args: [loanId]
    });

    if (mandate.borrower === "0x0000000000000000000000000000000000000000") {
      // borrower hasn't accepted a mandate; can't finalize
      continue;
    }

    try {
      const hash = await walletClient.writeContract({
        address: cfg.L1_COLLAR_VAULT,
        abi: CollarVaultAbi,
        functionName: "finalizeLoan",
        args: [loanId, depositGuid, tradeGuid]
      });
      console.log("finalizeLoan sent", { loanId: loanId.toString(), hash, depositGuid, tradeGuid });
    } catch (e: any) {
      console.warn("finalizeLoan failed (will retry)", {
        loanId: loanId.toString(),
        err: String(e?.shortMessage || e?.message || e)
      });
    }
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
