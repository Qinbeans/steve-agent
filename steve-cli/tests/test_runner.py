import asyncio

from langchain_core.messages import AIMessageChunk

from steve_agent.server.events import EventTranslator
from steve_agent.server.runner import Run, RunChannel

TOP_LEVEL = {"langgraph_node": "model", "langgraph_checkpoint_ns": "model:1"}


def published_events():
    translator = EventTranslator()
    return translator.start() + translator.finish()


async def collect(subscription):
    return [event async for event in subscription]


async def test_subscribers_replay_from_any_point_and_stop_at_the_terminal_event():
    channel = RunChannel()
    events = published_events()
    channel.publish(events[:1])
    early = asyncio.create_task(collect(channel.subscribe()))
    await asyncio.sleep(0)
    channel.publish(events[1:])

    assert await asyncio.wait_for(early, 5) == events
    assert await collect(channel.subscribe()) == events
    assert await collect(channel.subscribe(starting_after=0)) == events[1:]
    assert await channel.terminal() == events[-1]


async def test_two_waiting_subscribers_both_wake():
    channel = RunChannel()
    events = published_events()
    waiting = [asyncio.create_task(collect(channel.subscribe())) for _ in range(2)]
    await asyncio.sleep(0)

    channel.publish(events[:1])
    await asyncio.sleep(0)
    channel.publish(events[1:])

    assert await asyncio.wait_for(asyncio.gather(*waiting), 5) == [events, events]


async def test_idle_subscriber_gets_none_and_keeps_its_place():
    channel = RunChannel()
    events = published_events()
    subscription = channel.subscribe(idle_timeout=0.01)

    assert await anext(subscription) is None
    channel.publish(events)
    assert await anext(subscription) == events[0]


async def test_run_closes_the_agent_stream_when_translation_fails(caplog):
    closed = []

    class Agent:
        async def astream(self, *args, **kwargs):
            try:
                yield (), "messages", (AIMessageChunk(content="x"), TOP_LEVEL)
                await asyncio.sleep(60)
            finally:
                closed.append(True)

    class BrokenTranslator(EventTranslator):
        def feed(self, ns, chunk, metadata):
            raise RuntimeError("sk-secret")

    run = Run(Agent(), BrokenTranslator(), [], None, thread_id="t")

    terminal = await asyncio.wait_for(run.channel.terminal(), 5)

    assert closed == [True]
    assert terminal.response.status == "failed"
    assert "sk-secret" not in terminal.model_dump_json(warnings=False)
    assert "sk-secret" in caplog.text
    await run.task
