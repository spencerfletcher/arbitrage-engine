"""
scripts/gen_wallet.py
─────────────────────
Generate a fresh Ethereum/Polygon wallet for bot use.

Run once:
    python -m scripts.gen_wallet

Then:
  1. Copy PRIVATE_KEY and FUNDER_ADDRESS into your .env
  2. On Kraken: Funding → Withdraw → USDC → Network: Polygon (MATIC)
     Paste the address above as the destination
  3. Go to polymarket.com, create a new account, connect with the new wallet
     (use "Connect Wallet" and paste the private key into Rabby/MetaMask,
      or just sign in and deposit directly via Polymarket's deposit flow)
  4. Run scripts/verify_connections.py to confirm everything works

SECURITY: Never share your private key or commit it to git.
"""
from __future__ import annotations

from eth_account import Account


def main() -> None:
    acct = Account.create()

    pk = acct.key.hex()
    addr = acct.address

    print()
    print("═" * 56)
    print("  New wallet generated — save these somewhere safe")
    print("═" * 56)
    print(f"  Address (FUNDER_ADDRESS):  {addr}")
    print(f"  Private key (PRIVATE_KEY): {pk}")
    print("═" * 56)
    print()
    print("Add to your .env:")
    print(f'  PRIVATE_KEY={pk}')
    print(f'  FUNDER_ADDRESS={addr}')
    print(f'  SIGNATURE_TYPE=0')
    print()
    print("Kraken withdrawal:")
    print(f'  Network : Polygon (MATIC)')
    print(f'  Address : {addr}')
    print(f'  Asset   : USDC')
    print()
    print("⚠️  Store the private key securely — whoever has it controls the wallet.")
    print()


if __name__ == "__main__":
    main()
