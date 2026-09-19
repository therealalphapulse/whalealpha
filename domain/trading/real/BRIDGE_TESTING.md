# Bridge testing runbook (do this before mainnet use)

Robinhood Chain Testnet settles to **Ethereum Sepolia**, not Ethereum
mainnet -- a separate chain with its own bridge contract deployment.
Testnet ETH has no value; use it to dry-run the entire flow, including
the ~7-day withdrawal challenge period, at zero financial risk.

## 1. Fund your wallet
- Robinhood Chain Testnet ETH: https://faucet.testnet.chain.robinhood.com
- Sepolia ETH (needed to pay L1 gas for `/bridge deposit ... testnet`
  and `/bridge claim ... testnet`): any public Sepolia faucet, e.g.
  https://sepoliafaucet.com or https://www.alchemy.com/faucets/ethereum-sepolia

Run `/realwallet` in the bot first if you don't have a wallet yet -- the
same EVM address is used on every network.

## 2. Deposit (Sepolia -> Robinhood Chain Testnet)
`/bridge deposit 0.01 testnet`
Expect confirmation within a couple minutes on Sepolia, funds visible on
Robinhood Chain Testnet within ~10 minutes. Check `/wallet` for the
updated Robinhood Chain balance.

## 3. Withdraw (Robinhood Chain Testnet -> Sepolia)
`/bridge withdraw 0.005 testnet`
Note the `withdrawal_id` returned. `/bridge status` will show it and its
`claimable_after` time (~7 days out -- same challenge period as
mainnet, so this step still takes real wall-clock time to fully verify).

## 4. Claim (after claimable_after)
`/bridge claim <id>`
This is the step to watch closely: it builds a Merkle proof via the
NodeInterface precompile and submits it to Outbox.executeTransaction on
L1. If it reverts, check the transaction on
https://explorer.testnet.chain.robinhood.com (L2 side) and Sepolia
Etherscan (L1 side) for the revert reason before assuming anything about
mainnet correctness.

## What passing this tells you
- Deposit and withdrawal-initiate: selector/calldata construction is
  correct end to end (these are the same code paths as mainnet, just
  pointed at different RPCs/contracts).
- Claim: the Merkle-proof construction and `executeTransaction` calldata
  encoding -- the one part that could not be verified without a live
  chain -- actually works.

None of this proves mainnet gas estimation or timing will behave
identically, but it retires the main structural risk (wrong calldata)
at zero cost.
