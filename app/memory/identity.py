from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


def digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Actor:
    channel: str
    person: str
    scope: str
    owner: bool
    role: str

    @property
    def private(self):
        return self.channel == 'wechat' and self.owner


class Identities:
    """Only deployment configuration can bind platform IDs. Never accept model IDs."""
    def __init__(self, config):
        self.config = config
        if set(config) != {'qq', 'wechat', 'person'} or not isinstance(config['person'], str):
            raise ValueError('记忆身份配置无效')
        for channel in ('qq', 'wechat'):
            if set(config[channel]) != {'account', 'owner'} or not all(
                isinstance(v, str) and v for v in config[channel].values()
            ):
                raise ValueError('记忆身份配置缺少可信帐号或绑定者')

    @classmethod
    def read(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def actor(self, message, account):
        binding = self.config.get(message.channel)
        if not binding or account != binding['account']:
            raise ValueError('当前渠道帐号与记忆身份配置不匹配')
        owner = message.user_id == binding['owner']
        if message.channel == 'wechat' and not owner:
            raise ValueError('未绑定的私聊身份')
        person = self.config['person'] if owner else 'user:' + digest(
            message.channel, account, message.user_id)
        scope = ('private:' + person if message.channel == 'wechat' else
                 self.group(message.conversation_id))
        return Actor(message.channel, person, scope, owner, message.role)

    def group(self, group):
        return 'group:' + digest('qq', self.config['qq']['account'], group)

    def event(self, message, account):
        return digest(message.channel, account, message.conversation_id, message.message_id)
