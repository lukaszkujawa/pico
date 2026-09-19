import queue
import threading
from collections.abc import Iterator

from pico.core.events import BusEvent


class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[BusEvent | None]] = []

    def publish(self, event: BusEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(None)

    def subscribe(self) -> Iterator[BusEvent]:
        subscriber: queue.Queue[BusEvent | None] = queue.Queue()
        with self._lock:
            self._subscribers.append(subscriber)
        return self._drain(subscriber)

    def _drain(self, subscriber: "queue.Queue[BusEvent | None]") -> Iterator[BusEvent]:
        try:
            while True:
                event = subscriber.get()
                if event is None:
                    return
                yield event
        finally:
            with self._lock:
                self._subscribers.remove(subscriber)
