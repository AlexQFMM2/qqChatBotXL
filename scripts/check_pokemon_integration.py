"""Local-only Python ↔ actual CB HTTP check. Never sends to QQ or WeChat."""
import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.channel import IncomingMessage
from app.pokemon import PokemonClient, PokemonError, PokemonGames
from app.storage import MemoryStore

SERVER = r'''
import {pathToFileURL} from 'node:url';
import readline from 'node:readline';
const {createBattleApiServer} = await import(pathToFileURL(process.argv[1]+'/apps/api/dist/server.js'));
let server;
async function start() {
 server=createBattleApiServer({host:'127.0.0.1',port:0,storageKind:'memory',configOverrides:{token:'',pokemonToken:'integration-test-token',pokemonEnabled:true,redisUrl:''}});
 await server.start();console.log('TEST_URL '+server.baseUrl);
}
await start();
for await (const line of readline.createInterface({input:process.stdin})) {
 if(line==='restart'){await server.close();await start();}
 if(line==='stop'){await server.close();break;}
}
'''

async def url(process):
    while True:
        line=await asyncio.wait_for(process.stdout.readline(),20)
        if not line: raise RuntimeError('local CB server exited before ready')
        text=line.decode().strip()
        if text.startswith('TEST_URL '): return text[9:]

async def run(root):
    process=await asyncio.create_subprocess_exec('node','--input-type=module','-e',SERVER,str(root),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    try:
        base=await url(process)
        with tempfile.TemporaryDirectory(prefix='pokemon-integration-') as directory:
            store=MemoryStore(str(Path(directory)/'chat.db'))
            try:
                async with aiohttp.ClientSession() as session:
                    client=PokemonClient(session,base,'integration-test-token')
                    transport=SimpleNamespace(send_text=AsyncMock(),send_battle_notice=AsyncMock())
                    games=PokemonGames(SimpleNamespace(pokemon_dex_enabled=True,pokemon_battle_enabled=True),store,client,transport)
                    a=IncomingMessage('qq','isolated-group','m1','A','A','member','开局','开局')
                    b=IncomingMessage('wechat','isolated-private','b1','B','B','member','你好','你好')
                    async def turn(message): return games.turn(message,await games.gate(message))
                    result=await (await turn(a)).execute('pokemon_dex',{'action':'search','category':'pokemon','query':'烈咬陆鲨'})
                    assert result['rows'][0]['id']=='garchomp'
                    view=await (await turn(a)).execute('pokemon_battle',{'action':'start'})
                    assert view['phase']=='rules' and not view['candidates']
                    assert (await games.gate(b))['status']=='busy'
                    a=replace(a,message_id='configure')
                    view=await (await turn(a)).execute('pokemon_battle',{'action':'configure','environment':'gen9'})
                    assert all(p['level']==50 and p['natureEffect'] and p['types'] and p['item'] for p in view['candidates']), view['candidates']
                    assert view['phase']=='selecting' and len(view['candidates'])==6
                    assert (await games.gate(b))['status']=='busy'
                    a=replace(a,message_id='m2')
                    view=await (await turn(a)).execute('pokemon_battle',{'action':'select','selection':[3,1,5]})
                    assert view['phase']=='active' and len(view['team'])==3
                    a=replace(a,message_id='m3')
                    current=await turn(a)
                    original=client.request
                    async def lost_response(method,path,**kwargs):
                        result=await original(method,path,**kwargs)
                        if method=='POST' and path.endswith('/actions'):
                            raise PokemonError('uncertain','simulated response lost after commit')
                        return result
                    client.request=lost_response
                    try: await current.execute('pokemon_battle',{'action':'act','action_id':view['actions'][0]['id']})
                    except PokemonError as exc: assert exc.code=='uncertain'
                    client.request=original
                    assert games.store.get(view['id'])['pending']
                    await games.tick()
                    row=games.store.get(view['id'])
                    assert not row['pending'] and row['view']['revision']>view['revision']
                    a=replace(a,message_id='m4')
                    ended=await (await turn(a)).execute('pokemon_battle',{'action':'end'})
                    assert ended['phase']=='ended' and ended['winner']=='p2'
                    await asyncio.gather(games.notify(ended['id']),games.notify(ended['id'],a))
                    assert transport.send_text.await_count+transport.send_battle_notice.await_count==1
                    assert (await games.gate(b))['status']=='free'
                    second=await (await turn(b)).execute('pokemon_battle',{'action':'start'})
                    process.stdin.write(b'restart\n');await process.stdin.drain();client.url=await url(process)
                    await games.tick()
                    assert games.store.get(second['id'])['view']['phase']=='interrupted'
                    assert (await games.gate(a))['status']=='free'
                    print('PASS actual CB HTTP + Python: dex, 6→3, cross-channel busy, action response loss, forfeit, notice dedup, restart interruption')
            finally: store.close()
    finally:
        if process.returncode is None:
            process.stdin.write(b'stop\n');await process.stdin.drain()
            try: await asyncio.wait_for(process.wait(),10)
            except TimeoutError: process.terminate();await process.wait()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--cb-root',required=True,type=Path)
    asyncio.run(run(parser.parse_args().cb_root.resolve()))
