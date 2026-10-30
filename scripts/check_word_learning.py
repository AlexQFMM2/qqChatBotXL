"""Opt-in configured-model probe with synthetic conversations and no platform sends.

Environment supplies the existing model credentials. --synthesize also calls the
configured TTS and saves WAV samples for human listening; it does not judge accent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.bot import PersonaBot
from app.channel import IncomingMessage
from app.config import Settings
from app.llm import LLMClient
from app.storage import MemoryStore
from app.wechat.chat import load_environment


class ProbeLLM(LLMClient):
    def __init__(self, settings, session, synthesize):
        super().__init__(settings, session)
        self.synthesize = synthesize
        self.calls = []
        self.spoken = []

    async def complete_with_tools(self, system, prompt, tools, execute, **kwargs):
        async def traced(name, arguments):
            event = {"name": name}
            self.calls.append(event)
            try:
                result = await execute(name, arguments)
                event["accepted"] = True
                return result
            except Exception as exc:
                event.update(accepted=False, error=type(exc).__name__)
                raise
        return await super().complete_with_tools(system, prompt, tools, traced, **kwargs)

    async def generate_voice(self, text, **kwargs):
        self.spoken.append(text)
        if self.synthesize:
            return await super().generate_voice(text, **kwargs)
        return b"simulated voice; not an audio acceptance sample"


async def run_case(session, args, label, question, expected_word, *, text_only=False, followup=False):
    with tempfile.TemporaryDirectory(prefix="word-learning-probe-") as directory:
        settings = replace(Settings.from_env(channel="wechat"),
            persona_path=str(ROOT / "persona.md"), database_path=directory + "/chat.db",
            workspace_root=directory + "/workspace", owner_user_ids=frozenset({"synthetic-owner"}),
            good_morning_enabled=False, web_tools_enabled=False, bilibili_enabled=False,
            vision_enabled=False, image_generation_enabled=False, voice_enabled=True,
            emote_enabled=False, pokemon_dex_enabled=False, pokemon_battle_enabled=False,
            log_message_content=False, task_progress_ack_enabled=False)
        store = MemoryStore(settings.database_path)
        events = []
        async def send_text(_conversation, text, *_args, **kwargs):
            events.append({"kind": "text", "text": text, "sequence": kwargs.get("sequence")})
            return {"id": "synthetic-text"}
        async def send_file(_conversation, path, *_args, **kwargs):
            event = {"kind": "audio", "sequence": kwargs.get("sequence"), "bytes": path.stat().st_size}
            if args.synthesize:
                output = args.output_dir / (label + path.suffix)
                output.write_bytes(path.read_bytes())
                event["sample"] = output.name
            events.append(event)
            return {"id": "synthetic-audio"}
        transport = SimpleNamespace(send_text=send_text, send_file=send_file, uncertain=False, expired=False)
        llm = ProbeLLM(settings, session, args.synthesize)
        bot = PersonaBot(settings, None, llm, store, transport=transport)
        message = IncomingMessage("wechat", "synthetic-conversation", "probe-" + label,
            "synthetic-owner", "老师", "owner", question, question)
        error = None
        try:
            if followup:
                await store.add(message.conversation_id, "bot", "夏莉",
                    "unhappy\n\n音标\n英 /ʌnˈhæpi/ ｜ 美 /ʌnˈhæpi/\n\n翻译\n不开心的", is_bot=True)
            await asyncio.wait_for(bot.handle_message(message, _from_task_queue=True), timeout=300)
        except Exception as exc:
            error = type(exc).__name__
        finally:
            await bot.close()
            store.close()
        accepted = [call["name"] for call in llm.calls if call.get("accepted")]
        cards = [event for event in events if event["kind"] == "text"
                 and all("\n" + heading + "\n" in event["text"] for heading in ("音标", "翻译", "词性", "构词拆解", "例句"))]
        audio_events = [event for event in events if event["kind"] == "audio"]
        if followup:
            ok = "send_voice" in accepted and not cards and llm.spoken == [expected_word] and len(audio_events) == 1
        elif expected_word:
            ok = (accepted.count("submit_word_card") == 1 and len(cards) == 1
                  and cards[0]["text"].startswith(expected_word + "\n"))
            ok = ok and (llm.spoken == [] if text_only else llm.spoken == [expected_word])
            ok = ok and ([event["kind"] for event in events] == (["text"] if text_only else ["text", "audio"]))
        else:
            ok = bool(events) and not cards and not llm.spoken and "submit_word_card" not in accepted
        report = {"case": label, "question": question, "execution_ok": bool(ok and not error),
                  "error": error, "audio_mode": "live" if args.synthesize else "simulated",
                  "tools": llm.calls, "spoken": llm.spoken, "events": events}
        (args.output_dir / (label + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return report["execution_ok"]


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outcomes = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180, connect=15)) as session:
        for word in ("unhappy", "reusable", "apple", "record", "read"):
            outcomes.append(await run_case(session, args, word, "解读 " + word, word))
        outcomes.append(await run_case(session, args, "text-only", "解读 unhappy，只要文字，不要语音", "unhappy", text_only=True))
        for label, question in (
            ("typo", "解读 recieve"), ("batch", "解读 apple 和 record"),
            ("ordinary", "今天买了一个 apple，挺甜的。"),
        ):
            outcomes.append(await run_case(session, args, label, question, None))
        outcomes.append(await run_case(session, args, "followup", "再读一下刚才的单词", "unhappy", followup=True))
    print(json.dumps({"execution_passed": sum(outcomes), "total": len(outcomes), "platform_sends": 0,
        "semantic_review": "required", "pronunciation_listening": "required" if args.synthesize else "not tested"}))
    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--synthesize", action="store_true")
    options = parser.parse_args()
    if options.env_file:
        load_environment([options.env_file])
    logging.basicConfig(level=logging.CRITICAL)
    sys.exit(asyncio.run(main(options)))
