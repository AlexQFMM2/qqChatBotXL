"""Structured English word cards; language facts remain the controller's responsibility."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class WordCardError(ValueError):
    pass


def _string_schema(limit: int, description: str) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": limit, "description": description}


WORD_CARD_TOOL = {
    "name": "submit_word_card",
    "description": (
        "用户明确要求解读/讲解一个英文单词时，提交完整的结构化单词卡片作为本轮最终回复。"
        "程序固定排版并先发文字，再按权限朗读 word；不要另外调用 send_voice 或附加闲聊。"
        "默认 pronounce=true，用户只要文字时设 false。拼写错误先确认，多词/短语/混合任务先请拆开。"
        "本工具成功后结束本轮，不与其他发送工具混用；词源或音标不确定必须直说。"
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "word": _string_schema(64, "单个英文词，可含连字符或撇号；保留用户要求的正确拼写"),
            "ipa_uk": _string_schema(180, "英音 IPA；多读音注明词性/时态；未知写未确认"),
            "ipa_us": _string_schema(180, "美音 IPA；多读音注明词性/时态；未知写未确认"),
            "meanings": {
                "type": "array", "minItems": 1, "maxItems": 3,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "part_of_speech": _string_schema(60, "词性缩写及中文，如 adj. 形容词"),
                        "translation": _string_schema(200, "与该词性对应的常用中文释义"),
                    },
                    "required": ["part_of_speech", "translation"],
                },
            },
            "formation": _string_schema(400, "一两句说明可确认的前缀/词根/基础词/后缀及含义；无明显结构时直说，不叙述历史沿革或读音演变"),
            "example_en": _string_schema(240, "一条包含该词、自然且对应所讲词义的英文例句"),
            "example_zh": _string_schema(240, "例句的中文翻译"),
            "pronounce": {"type": "boolean", "default": True,
                          "description": "默认朗读单词；用户明确只要文字时为 false"},
        },
        "required": ["word", "ipa_uk", "ipa_us", "meanings", "formation", "example_en", "example_zh"],
    },
}

_WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")


def _text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise WordCardError(f"{name} 必须为文字")
    value = value.strip()
    if not value or len(value) > limit:
        raise WordCardError(f"{name} 不能为空且最多 {limit} 字符，请缩短后重新提交")
    if len(value.splitlines()) != 1 or any(unicodedata.category(char).startswith("C") for char in value):
        raise WordCardError(f"{name} 必须为单行文字，不能包含控制字符")
    if any(marker in value.casefold() for marker in ("[[", "]]", "<think", "</think")):
        raise WordCardError(f"{name} 不能包含内部控制标记")
    return value


@dataclass(frozen=True, slots=True)
class Meaning:
    part_of_speech: str
    translation: str


@dataclass(frozen=True, slots=True)
class WordCard:
    word: str
    ipa_uk: str
    ipa_us: str
    meanings: tuple[Meaning, ...]
    formation: str
    example_en: str
    example_zh: str
    pronounce: bool = True

    def render(self, *, max_chars: int, voice_note: str = "") -> str:
        multiple = len(self.meanings) > 1
        translations = "\n".join(
            (f"{index}. " if multiple else "") + meaning.translation
            for index, meaning in enumerate(self.meanings, 1)
        )
        parts = "\n".join(
            (f"{index}. " if multiple else "") + meaning.part_of_speech
            for index, meaning in enumerate(self.meanings, 1)
        )
        text = (
            f"{self.word}\n\n音标\n英 {self.ipa_uk} ｜ 美 {self.ipa_us}"
            f"\n\n翻译\n{translations}\n\n词性\n{parts}"
            f"\n\n构词拆解\n{self.formation}\n\n例句\n{self.example_en}\n{self.example_zh}"
        )
        if voice_note:
            text += "\n\n读音\n" + voice_note
        if len(text) > max_chars:
            raise WordCardError(f"完整单词卡片超过 {max_chars} 字符，请精简字段后重新提交；不能截断栏目")
        return text


def parse_word_card(arguments: dict) -> WordCard:
    schema = WORD_CARD_TOOL["input_schema"]
    if not isinstance(arguments, dict) or set(arguments) - set(schema["properties"]):
        raise WordCardError("单词卡片包含不支持的参数")
    if set(schema["required"]) - set(arguments):
        raise WordCardError("单词卡片缺少必填字段，请补齐所有栏目")
    word = _text(arguments["word"], "word", 64).replace("’", "'")
    if not _WORD.fullmatch(word):
        raise WordCardError("一次只能解读一个英文词，可含连字符或撇号；多词、短语请拆开")
    pronounce = arguments.get("pronounce", True)
    if not isinstance(pronounce, bool):
        raise WordCardError("pronounce 必须为布尔值")
    raw_meanings = arguments["meanings"]
    if not isinstance(raw_meanings, list) or not 1 <= len(raw_meanings) <= 3:
        raise WordCardError("meanings 必须包含 1 至 3 组词性和对应释义")
    meanings = []
    for meaning in raw_meanings:
        if not isinstance(meaning, dict) or set(meaning) != {"part_of_speech", "translation"}:
            raise WordCardError("每组释义必须且只能包含 part_of_speech 和 translation")
        meanings.append(Meaning(_text(meaning["part_of_speech"], "part_of_speech", 60),
                                _text(meaning["translation"], "translation", 200)))
    fields = {name: _text(arguments[name], name, limit) for name, limit in (
        ("ipa_uk", 180), ("ipa_us", 180), ("formation", 400), ("example_en", 240), ("example_zh", 240),
    )}
    return WordCard(word=word, meanings=tuple(meanings), pronounce=pronounce, **fields)


def check_requested_word(card: WordCard, current_message: str) -> None:
    """Prevent silent spelling substitutions; Chinese follow-ups can use context."""
    requested = {match.casefold() for match in _WORD.findall(current_message.replace("’", "'"))}
    if requested and card.word.casefold() not in requested:
        raise WordCardError("提交的单词不在用户本条原文中；疑似拼写有误时先建议并确认，不能默默改词")


@lru_cache(maxsize=1)
def word_learning_rules() -> str:
    return Path(__file__).with_name("prompts").joinpath("word_learning.md").read_text(encoding="utf-8").strip()
