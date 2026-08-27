import json,time
from pathlib import Path
import telemetry_summary as ts

def test_load_memory_cpu_network_parsers():
    assert ts.parse_load("1.0 2.0 3.0 1/2 3")== (1.,2.,3.)
    mem=ts.parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
    assert mem["memory_used_bytes"]==600*1024 and mem["memory_percent"]==60
    assert ts.parse_cpu_line("cpu  1 2 3 4 5 6 7 8")== (36,9)
    net=ts.parse_network("Inter-|\n face |\neth0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0")
    assert net["eth0"]=={"rx_bytes":100,"tx_bytes":200}

def test_w1_temperature_and_missing(tmp_path):
    sensor=tmp_path/"28-test";sensor.mkdir();(sensor/"temperature").write_text("25125\n")
    assert ts.read_w1("28-test",tmp_path)["value_c"]==25.125
    assert ts.read_w1("missing",tmp_path)["status"]=="NOT_AVAILABLE"

def test_both_missing_and_invalid_parse(tmp_path):
    bad=tmp_path/ts.SDR_SENSOR;bad.mkdir();(bad/"temperature").write_text("invalid")
    sdr,lna=ts.read_temperatures(tmp_path)
    assert sdr["status"]=="NOT_AVAILABLE" and lna["status"]=="NOT_AVAILABLE"

def test_sensor_reads_are_concurrent_and_individually_preserved(monkeypatch):
    def slow(sensor,root):
        time.sleep(.08)
        return {"value_c":1 if sensor==ts.SDR_SENSOR else None,"status":"OK" if sensor==ts.SDR_SENSOR else "NOT_AVAILABLE","path":None}
    monkeypatch.setattr(ts,"read_w1",slow);started=time.perf_counter();sdr,lna=ts.read_temperatures(Path("x"));elapsed=time.perf_counter()-started
    assert elapsed<.14 and sdr["status"]=="OK" and lna["status"]=="NOT_AVAILABLE"

def test_process_detection(tmp_path):
    for pid,cmd in (("1",b"/usr/bin/rtl_tcp\0-a\0127.0.0.1"),("2",b"python\0x")):
        d=tmp_path/pid;d.mkdir();(d/"cmdline").write_bytes(cmd)
    found=ts.find_process(proc=tmp_path)
    assert len(found)==1 and found[0]["pid"]==1

def test_port_listening_parser_no_socket():
    text="sl local_address rem_address st\n0: 0100007F:04D2 00000000:0000 0A"
    assert ts.parse_listening_port(text,1234)
    assert not ts.parse_listening_port(text,1235)

def test_atomic_json(tmp_path):
    path=tmp_path/"x.json";ts.atomic_json(path,{"x":1});assert json.loads(path.read_text())=={"x":1}
    assert not (tmp_path/"x.json.tmp").exists()

def test_collect_schema_mount_and_expected_observed(tmp_path,monkeypatch):
    proc=tmp_path/"proc";proc.mkdir();(proc/"net").mkdir()
    (proc/"loadavg").write_text("1 2 3 1/1 1");(proc/"meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    (proc/"uptime").write_text("100 50");(proc/"stat").write_text("cpu 1 1 1 10 0\n")
    (proc/"net/dev").write_text("a\nb\nlo: 1 0 0 0 0 0 0 0 2 0 0 0 0 0 0 0")
    (proc/"net/tcp").write_text("sl local_address rem_address st\n");(proc/"net/tcp6").write_text("sl local_address rem_address st\n")
    monkeypatch.setattr(ts,"cpu_percent",lambda *a,**k:12.5)
    value=ts.collect(tmp_path,proc,tmp_path/"w1")
    assert value["schema_version"]=="1.0" and value["mount"]["status"]=="NOT_EXPOSED"
    assert value["sdr"]["expected_gain_db"]==40.2 and value["sdr"]["observed_gain_db"] is None
    assert value["status"]=="DEGRADED" and value["generation_seconds"]<.5

def test_source_contains_no_socket_connect_or_destructive_commands():
    source=Path(ts.__file__).read_text()
    assert ".connect(" not in source
    for token in ("systemctl"," kill ","restart","sudo","rtl_tcp -"):assert token not in source
