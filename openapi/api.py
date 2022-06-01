import os
import json
from dataclasses import dataclass
from typing import List, Optional, Dict
import asyncio
from logzero import logger
from fastapi import FastAPI, APIRouter, Request, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from aiocache import caches, cached
from pydantic import BaseModel
from .utils import int_to_hex, hexstr_to_bytes
from .utils.bech32m import decode_puzzle_hash
from .nft import get_nft_info
from .rpc_client import FullNodeRpcClient
from .types import Coin, Program
from . import config as settings

caches.set_config(settings.CACHE_CONFIG)


app = FastAPI()


RPC_METHOD_WHITE_LIST = set(settings.RPC_METHOD_WHITE_LIST)


@dataclass
class Chain:
    id: str
    network_name: str
    network_prefix: str
    client: FullNodeRpcClient


async def init_chains(app, chains_config):
    chains: Dict[str, Chain] = {}
    for row in chains_config:
        id_hex = int_to_hex(row['id'])

        if row.get('proxy_rpc_url'):
            client = await FullNodeRpcClient.create_by_proxy_url(row['proxy_rpc_url'])
        elif row.get('chia_root_path'):
            client = await FullNodeRpcClient.create_by_chia_root_path(row['chia_root_path'])
        else:
            raise ValueError(f"chian {row['id']} has no full node rpc config")
        
        # check client
        network_info =  await client.get_network_info()

        chains[id_hex] = Chain(id_hex, row['network_name'], row['network_prefix'], client)

    app.state.chains = chains


@app.on_event("startup")
async def startup():
    await init_chains(app, settings.SUPPORTED_CHAINS)


@app.on_event("shutdown")
async def shutdown():
    for chain in app.state.chains.values():
        chain.client.close()
        await chain.client.await_closed()


def decode_address(address, prefix):
    try:
        _prefix, puzzle_hash = decode_puzzle_hash(address)
        if _prefix != prefix:
            raise ValueError("wrong prefix")
        return puzzle_hash
    except ValueError:
        raise HTTPException(400, "Invalid Address")


async def get_chain(request: Request, chain="0x01") -> Chain:
    if chain not in request.app.state.chains:
        raise HTTPException(400, "Ivalid Chain")
    return request.app.state.chains[chain]


router = APIRouter()


class UTXO(BaseModel):
    parent_coin_info: str
    puzzle_hash: str
    amount: str


def coin_javascript_compat(coin):
    return {
        'parent_coin_info':  coin['parent_coin_info'],
        'puzzle_hash': coin['puzzle_hash'],
        'amount': str(coin['amount'])
    }


@router.get("/utxos", response_model=List[UTXO])
@cached(ttl=10, key_builder=lambda *args, **kwargs: f"utxos:{kwargs['address']}", alias='default')
async def get_utxos(address: str, chain: Chain = Depends(get_chain)):
    # todo: use blocke indexer and supoort unconfirmed param
    pzh = decode_address(address, chain.network_prefix)

    # the old version db has inefficient index, should set include_spent_coins=True
    coin_records = await chain.client.get_coin_records_by_puzzle_hash(puzzle_hash=pzh, include_spent_coins=False)
    data = []

    for row in coin_records:
        if row['spent']:
            continue
        data.append(coin_javascript_compat(row['coin']))
    return data


class SendTxBody(BaseModel):
    spend_bundle: dict


@router.post("/sendtx")
async def create_transaction(item: SendTxBody, chain: Chain = Depends(get_chain)):
    spb = item.spend_bundle
    try:
        resp = await chain.client.push_tx(spb)
    except ValueError as e:
        logger.warning("sendtx: %s, error: %r", spb, e)
        raise HTTPException(400, str(e))
    return {
        'status': resp['status'],
    }


class ChiaRpcParams(BaseModel):
    method: str
    params: Optional[Dict] = None


@router.post('/chia_rpc')
async def full_node_rpc(item: ChiaRpcParams, chain: Chain = Depends(get_chain)):
    """
    ref: https://docs.chia.net/docs/12rpcs/full_node_api
    """
    # todo: limit method and add cache
    if item.method not in RPC_METHOD_WHITE_LIST:
        raise HTTPException(400, f"unspport chia rpc method: {item.method}")

    return await chain.client.raw_fetch(item.method, item.params)


@router.get('/balance')
@cached(ttl=10, key_builder=lambda *args, **kwargs: f"balance:{kwargs['address']}", alias='default')
async def query_balance(address: str, chain: Chain = Depends(get_chain)):
    # todo: use block indexer
    puzzle_hash = decode_address(address, chain.network_prefix)
    coin_records = await chain.client.get_coin_records_by_puzzle_hash(puzzle_hash=puzzle_hash, include_spent_coins=False)
    amount = sum([c['coin']['amount'] for c in coin_records if not c['spent']])
    data = {
        'amount': amount
    }
    return data


@router.get('/nfts')
@cached(ttl=20, key_builder=lambda *args, **kwargs: f"nfts:{kwargs['address']}", alias='default')
async def list_nfts(address: str, chain: Chain = Depends(get_chain)):
    # todo: use nft indexer
    puzzle_hash = decode_address(address, chain.network_prefix)
    start_height = settings.NFT_CHAIN_START_HEIGHT[chain.network_name]
    coin_records = await chain.client.get_coin_records_by_hint(
        puzzle_hash, include_spent_coins=False, start_height=start_height)
    
    pz_and_solutions = await asyncio.gather(*[
        chain.client.get_puzzle_and_solution(hexstr_to_bytes(cr['coin']['parent_coin_info']), cr['confirmed_block_index'])
        for cr in coin_records
    ])

    data = []
    for coin_record, cs in zip(coin_records, pz_and_solutions):
        nft_coin = Coin.from_json_dict(coin_record['coin'])
        puzzle = Program.fromhex(cs['puzzle_reveal'])
        solution = Program.fromhex(cs['solution'])

        try:
            nft_info = get_nft_info(nft_coin, puzzle, solution)
        except Exception as e:
            continue

        if nft_info.owner != puzzle_hash.hex():
            continue
        nft_info_dict = nft_info.to_dict()

        data.append(nft_info_dict)
    
    return data



# @router.get('/dids')
# @cached(ttl=10, key_builder=lambda *args, **kwargs: f"dids:{kwargs['address']}", alias='default')
# async def list_dids(address: str, chain: Chain = Depends(get_chain)):
#     # todo: use indexer
#     pass


app.include_router(router, prefix="/v1")