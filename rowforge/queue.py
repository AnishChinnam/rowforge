"""Redis is a lossy wake-up hint, never the source of truth."""

import logging
import time

import redis

log = logging.getLogger(__name__)


class Wakeup:
    def __init__(self, url):
        self.client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=3) if url else None

    def notify(self):
        if self.client:
            try:
                # One bounded hint; DB polling discovers every durable job even if this is lost.
                self.client.set("rowforge:wakeup", "1", ex=30)
            except redis.RedisError:
                log.warning("Redis unavailable; durable database polling remains active")

    def wait(self, seconds=1):
        if self.client:
            try:
                if self.client.getdel("rowforge:wakeup"):
                    return
            except redis.RedisError:
                pass
        time.sleep(seconds)
