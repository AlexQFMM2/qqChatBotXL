"""Last check immediately before channel output (also covers tool-generated files)."""
from ..channel import TransportError


class MemoryTransport:
    def __init__(self, transport, memory):
        self.transport, self.memory = transport, memory

    def __getattr__(self, name):
        return getattr(self.transport, name)

    async def send_text(self, conversation, content, message_id=None, *, sequence=1):
        return await self.transport.send_text(conversation, self.memory.safe_reply(content),
                                              message_id, sequence=sequence)

    async def send_file(self, conversation, path, message_id, *, sequence=1):
        if not self.memory.file_refs(conversation, path) or not self.memory.valid():
            raise TransportError('文件使用的长期记忆已失效，未发送。')
        return await self.transport.send_file(conversation, path, message_id, sequence=sequence)
