import os

from pytest_logserver_xdist_app import emit_worker_one, emit_worker_two


def test_pytest_xdist_worker_one_arrives():
    assert os.environ["OOPTDD_CID"]
    assert os.environ["TRACEPARENT"].startswith("00-")
    emit_worker_one()


def test_pytest_xdist_worker_two_arrives():
    assert os.environ["OOPTDD_CID"]
    assert os.environ["TRACEPARENT"].startswith("00-")
    emit_worker_two()
