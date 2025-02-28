from web3 import Web3
import time
import threading
import logging
import os
from dotenv import load_dotenv
from collections import deque
from cachetools import TTLCache

# Load environment variables
load_dotenv()
ALCHEMY_BASE_RPC = os.getenv("ALCHEMY_BASE_RPC")

# Contract Addresses
UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V2_FACTORY = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"

# Uniswap Routers
UNISWAP_ROUTERS = {
    "Router 1": "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD",
    "Router 2": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
    "Router 3": "0x2626664c2603336E57B271c5C0b26F421741e481"
}

# WETH Token Address (Base Network)
WETH = "0x4200000000000000000000000000000000000006"

# ANSI Colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"

# Setup Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger()

# Connect to Base with Auto-Retry
def connect_rpc():
    while True:
        w3 = Web3(Web3.HTTPProvider(ALCHEMY_BASE_RPC))
        if w3.is_connected():
            logger.info(f"Connected to Base RPC. Chain ID: {w3.eth.chain_id}")
            return w3
        logger.error("Failed to connect to Alchemy Base RPC. Retrying in 5 seconds...")
        time.sleep(5)

w3 = connect_rpc()

# ABIs
factory_v3_abi = [{"inputs":[{"internalType":"address","name":"token0","type":"address"},{"internalType":"address","name":"token1","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"}],"name":"getPool","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]
factory_v2_abi = [{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"}],"name":"getPair","outputs":[{"internalType":"address","name":"pair","type":"address"}],"stateMutability":"view","type":"function"}]
erc20_abi = [{"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},{"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},{"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]

factory_v3 = w3.eth.contract(address=UNISWAP_V3_FACTORY, abi=factory_v3_abi)
factory_v2 = w3.eth.contract(address=UNISWAP_V2_FACTORY, abi=factory_v2_abi)

# Cache for seen transactions (max 5000)
seen_txs = deque(maxlen=5000)

# Cache for token listing status (valid for 10 minutes)
token_cache = TTLCache(maxsize=10000, ttl=600)

def is_token_listed(token_address):
    if token_address in token_cache:
        return token_cache[token_address]

    for fee in [100, 500, 3000, 10000]:
        try:
            pool = factory_v3.functions.getPool(token_address, WETH, fee).call()
            if pool != "0x0000000000000000000000000000000000000000":
                token_cache[token_address] = True
                return True
        except:
            continue
    try:
        pair = factory_v2.functions.getPair(token_address, WETH).call()
        if pair != "0x0000000000000000000000000000000000000000":
            token_cache[token_address] = True
            return True
    except:
        pass

    token_cache[token_address] = False
    return False

def get_token_info(token_address):
    token = w3.eth.contract(address=token_address, abi=erc20_abi)
    try:
        name = token.functions.name().call()
        symbol = token.functions.symbol().call()
        decimals = token.functions.decimals().call()
        return name, symbol, decimals
    except:
        return "Unknown", "UNK", 18

# Add Transfer event ABI for holder count
transfer_event_abi = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"}
    ],
    "name": "Transfer",
    "type": "event"
}

def get_holder_count(token_address):
    try:
        token = w3.eth.contract(address=token_address, abi=[transfer_event_abi])
        latest_block = w3.eth.block_number
        logs = w3.eth.get_logs({
            "fromBlock": latest_block - 20000,  # Last ~2-3 days of transfers
            "toBlock": "latest",
            "address": token_address,
            "topics": [w3.keccak(text="Transfer(address,address,uint256)").hex()]
        })

        holders = set()
        for log in logs:
            sender = "0x" + log["topics"][1].hex()[-40:]
            receiver = "0x" + log["topics"][2].hex()[-40:]
            holders.add(sender)
            holders.add(receiver)

        return len(holders)
    except:
        return 0  # Assume no holders if error occurs

def process_transaction(tx):
    try:
        if tx["hash"].hex() in seen_txs or tx["to"] is None:
            return

        input_data = tx["input"]
        if not input_data.startswith(b"\x09\x5e\xa7\xb3"):  # ERC20 approve()
            return

        token_address = w3.to_checksum_address(tx["to"])

        # Ignore WETH approvals
        if token_address.lower() == WETH.lower():
            return  

        input_hex = input_data.hex()
        spender = w3.to_checksum_address("0x" + input_hex[34:74])
        amount = int(input_hex[74:], 16)

        seen_txs.append(tx["hash"].hex())

        spender_router = next((name for name, addr in UNISWAP_ROUTERS.items() if addr.lower() == spender.lower()), None)

        if is_token_listed(token_address):
            logger.info(f"Token {token_address} already listed, skipping.")
            return

        # Get holder count and filter out if more than 100 holders
        holder_count = get_holder_count(token_address)
        if holder_count > 100:
            logger.info(f"Skipping {token_address} - {holder_count} holders (Above 100)")
            return

        name, symbol, decimals = get_token_info(token_address)

        # Filter out tokens with ticker "UNI-V2"
        if symbol == "UNI-V2":
            logger.info(f"Skipping {name} ({symbol}) - Ticker is UNI-V2")
            return

        human_amount = amount / (10 ** decimals)

        # Colored CLI Output
        print(f"{GREEN}Token: {name} ({symbol}){RESET}")
        print(f"{YELLOW}Tx Hash: {tx['hash'].hex()}{RESET}")
        print(f"{BLUE}Token Address: {token_address}{RESET}")
        print(f"Spender: {spender} ({spender_router if spender_router else 'Unknown'})")
        print("-" * 50)

    except Exception as e:
        logger.error(f"Error processing transaction {tx['hash'].hex()}: {e}")

def monitor_transactions():
    logger.info("Starting transaction monitoring...")
    last_block = w3.eth.block_number - 1

    while True:
        try:
            current_block = w3.eth.block_number
            if last_block == current_block:
                time.sleep(1)
                continue

            logger.info(f"Processing block {current_block}")
            block = w3.eth.get_block(current_block, full_transactions=True)

            threads = []
            for tx in block["transactions"]:
                t = threading.Thread(target=process_transaction, args=(tx,))
                t.start()
                threads.append(t)

            for t in threads:
                t.join()

            last_block = current_block
        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")
            time.sleep(5)

# Auto-restart on crash
while True:
    try:
        monitor_transactions()
    except Exception as e:
        logger.error(f"Critical error: {e}. Restarting in 10 seconds...")
        time.sleep(10)
