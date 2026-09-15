import threading

from pico.core.bus import Bus
from pico.core.events import RunFinished, RunStarted


def test_subscriber_receives_events_published_after_subscribing() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    bus.publish(RunStarted())

    event = next(subscriber)

    assert event == RunStarted()


def test_multiple_subscribers_each_receive_their_own_copy() -> None:
    bus = Bus()
    first = bus.subscribe()
    second = bus.subscribe()

    bus.publish(RunStarted())

    assert next(first) == RunStarted()
    assert next(second) == RunStarted()


def test_publish_from_one_thread_observed_by_subscriber_on_another() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    received: list[object] = []

    def publisher() -> None:
        bus.publish(RunFinished())

    thread = threading.Thread(target=publisher)
    thread.start()
    received.append(next(subscriber))
    thread.join()

    assert received == [RunFinished()]
