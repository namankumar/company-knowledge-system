"""Run the original exported tests with network connections refused."""
import os
import pathlib
import socket
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["APP_ENV"] = "test"
os.environ["LLM_FIXTURE_MODE"] = "true"

def refused(*args, **kwargs):
    raise RuntimeError("Network connections are disabled in offline export verification")

socket.socket.connect = refused
socket.socket.connect_ex = refused
socket.create_connection = refused

if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main(["-q", "tests"]))
