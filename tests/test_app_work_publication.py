"""A stalled response cannot hold account mutation admission."""

import threading
import admin_auth
from tests.test_app_work_http import work_http, control, preparation


def test_publication_admission_releases_account_lock_before_socket_write(work_http):
    h = work_http
    done = threading.Event()
    failures, writes, threads = [], [], []

    def revoke():
        conn = h.account_open()
        try:
            admin_auth.revoke_session(conn, h.token)
        except BaseException as error:
            failures.append(error)
        finally:
            conn.close()
            done.set()

    def blocked_socket(status, body):
        writes.append((status, body))
        thread = threading.Thread(target=revoke)
        threads.append(thread)
        thread.start()
        assert done.wait(3), "socket publication retained account admission lock"

    try:
        h.service.perform(
            "prepare_work",
            preparation(),
            account_token=h.token,
            control_token=h.credential,
            publish=blocked_socket,
        )
    finally:
        for thread in threads:
            thread.join(5)
    assert done.is_set() and not failures
    assert len(writes) == 1 and writes[0][0] == 200
    assert not h.models and not h.service.authority._selections
    status, _, body = h.request(
        "GET", "/v1/app-control/work/" + writes[0][1]["work"]["work_id"]
    )
    assert status == 401 and "work" not in body
