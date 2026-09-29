"""Agent 事件可靠发送的单元测试（标准库 unittest）。"""

import asyncio
import json
import unittest

from agent import service as service_module
from agent.service import AgentService


class FakeSocket:
    """记录发送内容的假连接，用于模拟发送成功或失败。"""

    def __init__(self, fail: bool = False) -> None:
        """按参数决定这条连接是故障连接还是正常连接。"""
        self.fail = fail
        self.payloads: list[str] = []

    async def send(self, payload: str) -> None:
        """故障连接直接抛异常，正常连接记录负载。"""
        if self.fail:
            raise ConnectionError("socket closed")
        self.payloads.append(payload)


class ReliableEventTests(unittest.IsolatedAsyncioTestCase):
    """覆盖连接断开后事件重发与顺序保持的行为。"""

    def setUp(self) -> None:
        """跳过 AgentService.__init__，避免测试启动配置服务。"""
        self.service = AgentService.__new__(AgentService)
        self.service.socket = None
        self.service.connected = False
        # 缩短重试间隔，测试无需等待真实秒数。
        self.original_retry = service_module.EVENT_RETRY_SECONDS
        service_module.EVENT_RETRY_SECONDS = 0.01

    def tearDown(self) -> None:
        """恢复真实重试间隔。"""
        service_module.EVENT_RETRY_SECONDS = self.original_retry

    async def test_terminal_event_is_resent_after_reconnect(self) -> None:
        """终态事件在旧连接发送失败后必须重发到新连接。"""
        broken = FakeSocket(fail=True)
        self.service.socket = broken
        self.service.connected = True
        event = {"kind": "status", "task_id": "abc", "status": "success", "sequence": 7}
        task = asyncio.create_task(self.service._send_event_reliable(event))
        # 等首次发送失败并把连接标记为断开。
        await asyncio.sleep(0.05)
        self.assertFalse(self.service.connected)
        # 模拟重连成功，事件应被重发到新连接。
        healthy = FakeSocket()
        self.service.socket = healthy
        self.service.connected = True
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(1, len(healthy.payloads))
        self.assertEqual(event, json.loads(healthy.payloads[0])["event"])

    async def test_events_keep_order_when_connection_returns(self) -> None:
        """多条事件按调用顺序依次送达。"""
        healthy = FakeSocket()
        self.service.socket = healthy
        self.service.connected = True
        events = [{"sequence": 1}, {"sequence": 2}, {"sequence": 3}]
        for event in events:
            await self.service._send_event_reliable(event)
        self.assertEqual([json.loads(payload)["event"] for payload in healthy.payloads], events)


if __name__ == "__main__":
    unittest.main()
