"""Supply isolated AstrBot test doubles without contacting a running bot."""

import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class Plain:
    type = "Plain"

    def __init__(self, text):
        self.text = text


class MessageChain:
    def __init__(self, chain):
        self.chain = chain


class Star:
    def __init__(self, context, config=None):
        self.context = context


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("tibo-tests")
event = types.ModuleType("astrbot.api.event")
event.AstrMessageEvent = object
event.MessageChain = MessageChain
event.filter = types.SimpleNamespace(command=lambda *args, **kwargs: lambda fn: fn)
components = types.ModuleType("astrbot.api.message_components")
components.Plain = Plain
star = types.ModuleType("astrbot.api.star")
star.Star = Star
star.Context = object
star.register = lambda *args, **kwargs: lambda cls: cls
for module in (astrbot, api, event, components, star):
    sys.modules[module.__name__] = module
