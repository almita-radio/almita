import asyncio
import pytest
from indi_telescope_control import INDITelescopeControl

def vector(state="Idle",on="Off",off="On"):
    return (f'<setSwitchVector device="LX200 OnStep" name="TELESCOPE_TRACK_STATE" state="{state}">'
            f'<oneSwitch name="TRACK_ON">{on}</oneSwitch><oneSwitch name="TRACK_OFF">{off}</oneSwitch>'
            '</setSwitchVector>')

class Writer:
    def __init__(self):self.messages=[]
    def write(self,data):self.messages.append(data.decode())
    async def drain(self):pass

class Reader:
    def __init__(self,*responses):self.responses=list(responses)
    async def read(self,_):return self.responses.pop(0) if self.responses else b""

def controller(*responses):
    c=INDITelescopeControl(device_name="LX200 OnStep",verbose=False);c.writer=Writer();c.reader=Reader(*[r.encode() for r in responses]);return c

@pytest.mark.asyncio
async def test_get_tracking_state_on():
    c=controller(vector(on="On",off="Off"));assert await c.get_tracking_state()=="on"

@pytest.mark.asyncio
async def test_get_tracking_state_off():
    c=controller(vector());assert await c.get_tracking_state()=="off"

@pytest.mark.asyncio
async def test_get_tracking_state_alert():
    c=controller(vector(state="Alert",on="On",off="Off"));assert await c.get_tracking_state()=="alert"

@pytest.mark.asyncio
async def test_missing_property_is_unknown():
    c=controller('<setSwitchVector name="OTHER" state="Ok"></setSwitchVector>');assert await c.get_tracking_state()=="unknown"

@pytest.mark.asyncio
async def test_wait_on_observes_off_then_on():
    c=controller();states=iter(["off","on"])
    async def get(timeout=1):return next(states)
    c.get_tracking_state=get;assert await c.wait_tracking_state(True,timeout=1)

@pytest.mark.asyncio
async def test_wait_off_observes_on_then_off():
    c=controller();states=iter(["on","off"])
    async def get(timeout=1):return next(states)
    c.get_tracking_state=get;assert await c.wait_tracking_state(False,timeout=1)

@pytest.mark.asyncio
async def test_wait_on_timeout_is_failure():
    c=controller()
    async def get(timeout=1):return "off"
    c.get_tracking_state=get;assert not await c.wait_tracking_state(True,timeout=.01)

@pytest.mark.asyncio
async def test_wait_off_timeout_is_failure():
    c=controller()
    async def get(timeout=1):return "on"
    c.get_tracking_state=get;assert not await c.wait_tracking_state(False,timeout=.01)

@pytest.mark.asyncio
async def test_alert_during_wait_fails_immediately():
    c=controller();calls=0
    async def get(timeout=1):
        nonlocal calls;calls+=1;return "alert"
    c.get_tracking_state=get;assert not await c.wait_tracking_state(True,timeout=1);assert calls==1

@pytest.mark.asyncio
async def test_new_apis_only_send_read_query():
    c=controller(vector(on="On",off="Off"));assert await c.get_tracking_state()=="on";assert len(c.writer.messages)==1;assert "<getProperties" in c.writer.messages[0];assert "<newSwitchVector" not in c.writer.messages[0]

@pytest.mark.asyncio
async def test_set_tracking_behavior_is_preserved(monkeypatch):
    c=controller()
    async def no_sleep(_):pass
    monkeypatch.setattr(asyncio,"sleep",no_sleep);assert await c.set_tracking(True) is True;assert "<newSwitchVector" in c.writer.messages[0];assert "<oneSwitch name=\"TRACK_ON\">On" in c.writer.messages[0]
