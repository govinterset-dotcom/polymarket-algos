"""
merge_split.py — direct CTF split/merge/redeem via Polymarket proxy wallet (self-paid gas).
Builder attribution embedded. See README.md.
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

import os
import logging

log = logging.getLogger("merge_split")

try:
    from web3 import Web3
    from eth_account import Account
    HAVE_WEB3 = True
except ImportError:
    HAVE_WEB3 = False

POLYGON_RPC = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
CHAIN_ID = 137
PROXY_WALLET_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_BYTES32 = "0x" + "00" * 32
TYPE_CODE_CALL = 0
MERGE_SPLIT_ENABLED = os.environ.get("MERGE_SPLIT_ENABLED", "false").lower() == "true"
MERGE_SPLIT_DRY_RUN = os.environ.get("MERGE_SPLIT_DRY_RUN", "true").lower() == "true"
MERGE_GAS_LIMIT = int(os.environ.get("MERGE_GAS_LIMIT", "300000"))
MERGE_MAX_GAS_GWEI = float(os.environ.get("MERGE_MAX_GAS_GWEI", "300"))
USDC_DECIMALS = 6

PROXY_FACTORY_ABI = [{
    "constant": False,
    "inputs": [{
        "components": [
            {"name": "typeCode", "type": "uint8"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
        ],
        "name": "calls", "type": "tuple[]",
    }],
    "name": "proxy",
    "outputs": [{"name": "returnValues", "type": "bytes[]"}],
    "payable": True, "stateMutability": "payable", "type": "function",
}]

CTF_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "splitPosition", "outputs": [], "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "mergePositions", "outputs": [], "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions", "outputs": [], "type": "function",
    },
]

def _w3():
    if not HAVE_WEB3:
        raise RuntimeError("web3/eth_account not installed")
    return Web3(Web3.HTTPProvider(POLYGON_RPC))

def _send_proxy(private_key: str, calldata_hex: str) -> dict:
    w3 = _w3()
    acct = Account.from_key(private_key)
    factory = w3.eth.contract(address=Web3.to_checksum_address(PROXY_WALLET_FACTORY), abi=PROXY_FACTORY_ABI)
    calls = [(TYPE_CODE_CALL, Web3.to_checksum_address(CTF_ADDRESS), 0, bytes.fromhex(calldata_hex[2:]))]
    tx = factory.functions.proxy(calls).build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": MERGE_GAS_LIMIT,
        "maxFeePerGas": w3.to_wei(MERGE_MAX_GAS_GWEI, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(min(30, MERGE_MAX_GAS_GWEI), "gwei"),
        "chainId": CHAIN_ID,
    })
    if MERGE_SPLIT_DRY_RUN:
        log.info("DRY_RUN proxy call from %s", acct.address)
        return {"ok": True, "dry_run": True, "tx": {k: str(tx.get(k)) for k in tx}}
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    return {"ok": receipt.status == 1, "tx_hash": h.hex(), "status": receipt.status}

def merge_pair(private_key: str, condition_id: str, amount_shares: float, is_neg_risk: bool = False) -> dict:
    if is_neg_risk:
        return {"ok": False, "err": "neg-risk not supported"}
    if not MERGE_SPLIT_ENABLED:
        return {"ok": False, "err": "MERGE_SPLIT_ENABLED=false"}
    amount = int(round(amount_shares * 10 ** USDC_DECIMALS))
    w3 = _w3()
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    data = ctf.encode_abi("mergePositions", args=[
        Web3.to_checksum_address(USDC_E_ADDRESS), ZERO_BYTES32,
        condition_id if condition_id.startswith("0x") else "0x" + condition_id,
        [1, 2], amount,
    ])
    return _send_proxy(private_key, data)

def split_pair(private_key: str, condition_id: str, amount_usdc: float, is_neg_risk: bool = False) -> dict:
    if is_neg_risk:
        return {"ok": False, "err": "neg-risk not supported"}
    if not MERGE_SPLIT_ENABLED:
        return {"ok": False, "err": "MERGE_SPLIT_ENABLED=false"}
    amount = int(round(amount_usdc * 10 ** USDC_DECIMALS))
    w3 = _w3()
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    data = ctf.encode_abi("splitPosition", args=[
        Web3.to_checksum_address(USDC_E_ADDRESS), ZERO_BYTES32,
        condition_id if condition_id.startswith("0x") else "0x" + condition_id,
        [1, 2], amount,
    ])
    return _send_proxy(private_key, data)

def redeem_positions(private_key: str, condition_id: str, index_sets=None, is_neg_risk: bool = False) -> dict:
    """Redeem winning positions after resolution (auto-redeem)."""
    if is_neg_risk:
        return {"ok": False, "err": "neg-risk not supported"}
    if not MERGE_SPLIT_ENABLED:
        return {"ok": False, "err": "MERGE_SPLIT_ENABLED=false"}
    if index_sets is None:
        index_sets = [1, 2]
    w3 = _w3()
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    data = ctf.encode_abi("redeemPositions", args=[
        Web3.to_checksum_address(USDC_E_ADDRESS), ZERO_BYTES32,
        condition_id if condition_id.startswith("0x") else "0x" + condition_id,
        index_sets,
    ])
    return _send_proxy(private_key, data)
