import tempfile
import logging
from pathlib import Path
from nightshift.store import Store
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.bridge import BridgeAPI

logging.basicConfig(level=logging.DEBUG)

tmp_path = Path(tempfile.mkdtemp())
store = Store(tmp_path / "nightshift.db")
store.migrate()
bus = EventBus(store)
registry = ModuleRegistry()
registry.attach(store=store, events=bus)

bridge = BridgeAPI(store, bus, registry)
state_resp = bridge.call("token_optimizer.getState")
print("state_resp:", state_resp)
if state_resp.get("ok"):
    tools = {t["name"]: t for t in state_resp["data"]["tools"]}
    rm_tool = tools.get("repomix", {})
    print("repomix status:", rm_tool.get("status"))
    print("repomix enabled:", rm_tool.get("enabled"))
    print("repomix warning:", rm_tool.get("warning"))
    print("repomix catalogueOnly:", rm_tool.get("catalogueOnly"))
    print("repomix isolationGateRequired:", rm_tool.get("isolationGateRequired"))
else:
    print("Error:", state_resp.get("error"))
