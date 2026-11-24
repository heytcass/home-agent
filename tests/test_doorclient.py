import asyncio
import json

import pytest

from staff import doorclient


@pytest.fixture
async def door(tmp_path):
    """A real Unix socket server. -> (path, seen request lines, set_answer)"""
    path = str(tmp_path / "door.sock")
    seen, answer = [], {"line": b'{"ok": true, "rev": "abc"}\n'}

    async def client(reader, writer):
        seen.append(await reader.readline())
        if answer["line"] is None:
            await asyncio.sleep(30)
        writer.write(answer["line"])
        writer.close()

    server = await asyncio.start_unix_server(client, path)
    yield path, seen, lambda line: answer.update(line=line)
    server.close()


async def test_one_line_out_one_line_back(door):
    path, seen, _ = door
    reply = await doorclient.ask(path, {"op": "status", "host": "suzuka"})
    assert reply == {"ok": True, "rev": "abc"}
    assert seen[0].endswith(b"\n") and json.loads(seen[0]) == {"op": "status", "host": "suzuka"}


async def test_a_refusal_comes_back_as_it_is(door):
    path, _, answer = door
    answer(b'{"ok": false, "error": "Not at night."}\n')
    assert await doorclient.ask(path, {"op": "reboot"}) == {"ok": False, "error": "Not at night."}


@pytest.mark.parametrize("line", [b"", b"not json\n", b"[1]\n", b'{"rev": "abc"}\n', b'{"ok": "yes"}\n'])
async def test_a_bad_reply_is_a_failure(door, line):
    path, _, answer = door
    answer(line)
    reply = await doorclient.ask(path, {"op": "status"})
    assert reply["ok"] is False and "could not be read" in reply["error"]


async def test_no_answer_in_time_is_a_failure(door):
    path, _, answer = door
    answer(None)
    reply = await doorclient.ask(path, {"op": "status"}, timeout=0.05)
    assert reply["ok"] is False and "did not answer" in reply["error"]


async def test_no_socket_is_a_failure(tmp_path):
    for path in ("", str(tmp_path / "nothing.sock")):
        reply = await doorclient.ask(path, {"op": "status"})
        assert reply["ok"] is False and reply["error"]


async def test_a_refusal_without_a_reason_gets_one(door):
    path, _, answer = door
    answer(b'{"ok": false}\n')
    assert (await doorclient.ask(path, {"op": "status"}))["error"]
