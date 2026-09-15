import queue
import threading
from collections.abc import Iterator

from pico.core.events import BusEvent


class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[BusEvent]] = []

    def publish(self, event: BusEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def subscribe(self) -> Iterator[BusEvent]:
        subscriber: queue.Queue[BusEvent] = queue.Queue()
        with self._lock:
            self._subscribers.append(subscriber)
        return self._drain(subscriber)

    def _drain(self, subscriber: "queue.Queue[BusEvent]") -> Iterator[BusEvent]:
        try:
            while True:
                yield subscriber.get()
        finally:
            with self._lock:
                self._subscribers.remove(subscriber)
