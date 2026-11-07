import pytest

from upclose_rig.obd_sim import SYNTHETIC_VIN, CarState, EmulatedEcu, encode_dtc
from upclose_rig.obd_snapshot import (
    ObdError, connect, knob_to_rpm, link_from_recording, snapshot_from_connection,
)
from upclose_rig.protocol import Record


def test_encode_dtc():
    assert encode_dtc("P0301") == "0301"
    assert encode_dtc("C0123") == "4123"
    assert encode_dtc("U3FFF") == "FFFF"
    for bad in ("X0301", "P4301", "P03", "P03G1"):
        with pytest.raises(ValueError):
            encode_dtc(bad)


def test_roundtrip_injected_dtc_and_cleared_codes():
    import obd

    with EmulatedEcu(CarState(stored=["P0301"], rpm=780, coolant_c=24)) as ecu:
        conn = connect(ecu.portstr)
        try:
            d = snapshot_from_connection(conn, provenance="synthetic", device="elm327-emulator").data
            assert [(x.code, x.status) for x in d.dtcs] == [("P0301", "stored")]
            assert d.mil_on is True and d.vin == SYNTHETIC_VIN
            assert d.distance_since_codes_cleared_km == 12 and d.warmups_since_codes_cleared == 1
            assert d.pids == {"rpm": 780.0, "coolant_c": 24.0}
            assert d.readiness["catalyst"] == "incomplete" and d.readiness["misfire"] == "complete"

            # seller clears codes through the real OBD "clear DTC" request (mode 04)
            conn.query(obd.commands.CLEAR_DTC, force=True)
            d2 = snapshot_from_connection(conn, provenance="synthetic", device="elm327-emulator").data
            assert d2.dtcs == [] and d2.mil_on is False
            assert d2.distance_since_codes_cleared_km == 0 and d2.warmups_since_codes_cleared == 0
            assert d2.readiness["catalyst"] == d2.readiness["evap"] == "incomplete"
            assert d2.readiness["oxygen_sensor"] == "incomplete"
        finally:
            conn.close()


def test_state_changes_are_live_and_multi_dtc():
    with EmulatedEcu(CarState(stored=[], mil_on=False)) as ecu:
        conn = connect(ecu.portstr)
        try:
            assert snapshot_from_connection(conn, provenance="synthetic", device="x").data.dtcs == []
            ecu.state.inject_dtc("P0301")
            ecu.state.inject_dtc("P0420")
            ecu.state.rpm = 2500
            ecu.state.pending.append("P0171")
            d = snapshot_from_connection(conn, provenance="synthetic", device="x").data
            assert [(x.code, x.status) for x in d.dtcs] == [("P0301", "stored"), ("P0420", "stored"), ("P0171", "pending")]
            assert d.pids["rpm"] == 2500.0 and d.mil_on is True
        finally:
            conn.close()


def test_not_connected_raises():
    class Dead:
        def is_connected(self):
            return False

    with pytest.raises(ObdError):
        snapshot_from_connection(Dead(), provenance="synthetic", device="x")


def test_inject_invalid_dtc_rejected():
    with pytest.raises(ValueError):
        CarState().inject_dtc("Z9999")


def test_recording_link_button_and_knob():
    recs = [Record("K", 5, (0,)), Record("K", 9, (1023,)), Record("A", 10, (0, 0, 1000))]
    assert link_from_recording(recs) == (False, 3000.0)
    assert link_from_recording(recs + [Record("B", 20, (1,))])[0] is True
    assert link_from_recording([Record("B", 1, (0,))]) == (False, None)
    assert knob_to_rpm(0) == 600.0
