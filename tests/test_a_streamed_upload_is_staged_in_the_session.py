"""A stock client over `connect` uploads a path as a stream, and the server stages it where a payload is staged.

THE WIRE (playwright 1.61.0, `_impl/_set_input_files_helpers.py`, the `is_remote` branch of `convert_input_files`): for a path the client does not send
`localPaths`. It asks the context for `createTempFiles` (`rootDirName` and `items[{name, lastModifiedMs}]`), gets one `WritableStream` per item, copies
the file into each one with `write{binary}` chunks and `close`, and then sends `setInputFiles{streams: [{guid}, ...]}`. The server refused the first call
("the streamed upload path - set_input_files with local paths works": the sentence was false), so a path upload over `connect` never worked.

What is checked here, with no browser (the real dispatchers over a scripted engine, the real served endpoint for who owns what):

* `createTempFiles` answers one `WritableStream` per item, in the session's upload area, ONE NEW FOLDER PER CALL (two calls with one name do not overwrite
  each other), and the answer is what the stock server's is: `writableStreams` and no `rootDir` when no folder was named;
* a name is a BASENAME and nothing else: empty, `.`, `..`, a separator, an absolute path and a NUL are refused, by name, before anything is made - the name
  is what a client chose and it must not choose where on this machine the bytes go; two items of one call with one name are refused (they would be one file);
* a folder (`rootDirName`) is refused with the sentence `_upload_paths` already says for `localDirectory`;
* `write` appends, `close` ends the file and sets its mtime from `lastModifiedMs`; a `write` after `close` and a second `close` are refused and reopen nothing;
* `_upload_paths` turns `streams` into the paths of the closed files, and refuses a stream still open;
* the call's folder is removed when its context closes, and when the client that made it drops in the middle of the copy (`RemoteEndpoint._client_gone`
  closes the contexts it owns; the streams are its children, and the folder is the context's to remove) - and a context of ANOTHER client keeps its folder.

Known-bad input: on the code without `createTempFiles` every test fails at the first call.
"""
from __future__ import annotations

import base64
import itertools
import os
from types import SimpleNamespace

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_playwright._juggler import remote
from invisible_playwright._juggler.dispatcher import ProtocolException
from invisible_playwright._juggler.server import ElementHandleDispatcher, FrameDispatcher, LocalUtilsDispatcher, _upload_paths
from _browser_census import closes_with_the_test

pytestmark = pytest.mark.unit

MTIME_MS = 1_700_000_000_123


class _Engine(EventListeners):
    """The handful of commands a context needs to be made and removed."""

    def __init__(self):
        super().__init__()
        closes_with_the_test(self)
        self.removed = []
        self._contexts = itertools.count(1)

    def startup_send(self, method, params=None):
        return {}

    def post(self, method, params=None, session=None):
        pass

    def send(self, method, params=None, session=None, timeout=30):
        if method == "Browser.createBrowserContext":
            return {"browserContextId": "c%d" % next(self._contexts)}
        if method == "Browser.removeBrowserContext":
            self.removed.append(params)
        return {}


@pytest.fixture
def served():
    server = remote._SharedServer()
    browser_type = remote.SharedBrowserType(server)
    LocalUtilsDispatcher(server)
    conn = _Engine()
    browser = remote.SharedBrowserDispatcher(server, browser_type, conn, "firefox/1")
    endpoint = remote.RemoteEndpoint(server, browser_type, browser)
    yield endpoint
    endpoint.close()
    server.shutdown()


class _Loop:
    """The client's event loop, which the tests never run: what the server posts to a client is queued, and nobody reads it."""

    def call_soon_threadsafe(self, call, *args):
        call(*args)


def _client(endpoint):
    return remote.RemoteClient(endpoint, None, _Loop(), {})


def _send(endpoint, client, guid, method, params=None):
    """One call, answered the way a worker answers it: with the client set."""
    token = remote.CURRENT_CLIENT.set(client)
    try:
        return endpoint._dispatch(client, {"id": 1, "guid": guid, "method": method, "params": params or {}})
    finally:
        remote.CURRENT_CLIENT.reset(token)


def _context(endpoint, client):
    answer = _send(endpoint, client, endpoint.browser.guid, "newContext", {})
    return endpoint.server.object(answer["context"]["guid"])


def _temp_files(endpoint, client, context, items, root=None):
    return _send(endpoint, client, context.guid, "createTempFiles", {"rootDirName": root, "items": items})


def _item(name, ms=MTIME_MS):
    return {"name": name, "lastModifiedMs": ms}


def _chunk(data):
    return {"binary": base64.b64encode(data).decode()}


def _the_element_uploads(endpoint, context, params):
    """What the ElementHandle's `setInputFiles` hands the engine for this request, on an element of a page of `context`: its paths."""
    sent = []
    actions = SimpleNamespace(set_input_files=lambda handle, paths, **kwargs: sent.append(paths))
    element = SimpleNamespace(server=endpoint.server, frame=SimpleNamespace(page=SimpleNamespace(context=context), actions=actions),
                              _act_args=lambda p: {})
    ElementHandleDispatcher.op_set_input_files(element, params)
    return sent[0]


def _the_frame_uploads(endpoint, context, params):
    """The same for the Frame's `setInputFiles`, on a frame of a page of `context`."""
    sent = []
    actions = SimpleNamespace(set_input_files=lambda selector, paths, **kwargs: sent.append(paths))
    frame = SimpleNamespace(server=endpoint.server, page=SimpleNamespace(context=context), actions=actions,
                            enter_frames=lambda selector: ("f1", selector), _timeout=lambda p: None, _act_opts=lambda p: {})
    FrameDispatcher.op_set_input_files(frame, params)
    return sent[0]


def _streams(endpoint, answer):
    return [endpoint.server.object(channel["guid"]) for channel in answer["writableStreams"]]


def _copy(endpoint, client, stream, data):
    _send(endpoint, client, stream.guid, "write", _chunk(data[:3]))
    _send(endpoint, client, stream.guid, "write", _chunk(data[3:]))
    _send(endpoint, client, stream.guid, "close")


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The answer
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_create_temp_files_answers_one_writable_stream_per_item(served):
    client = _client(served)
    context = _context(served, client)
    answer = _temp_files(served, client, context, [_item("a.txt"), _item("b.bin")])
    assert set(answer) == {"writableStreams"}, "the stock server answers writableStreams and no rootDir for a call with no folder: %r" % (answer,)
    streams = _streams(served, answer)
    assert [s.TYPE for s in streams] == ["WritableStream", "WritableStream"]
    assert [os.path.basename(s.path) for s in streams] == ["a.txt", "b.bin"]
    assert all(os.path.isfile(s.path) for s in streams), "the files are made when the streams are"


def test_a_call_gets_a_folder_of_its_own_inside_the_session_upload_area(served):
    client = _client(served)
    context = _context(served, client)
    first = _streams(served, _temp_files(served, client, context, [_item("same.txt")]))[0]
    second = _streams(served, _temp_files(served, client, context, [_item("same.txt")]))[0]
    assert first.path != second.path, "two calls with one name wrote one file"
    assert os.path.dirname(first.path) != os.path.dirname(second.path)
    root = served.server._upload_root
    for stream in (first, second):
        assert os.path.commonpath([stream.path, root]) == root, "the folder is outside the area `stage_upload` uses: %s" % stream.path
    two = _streams(served, _temp_files(served, client, context, [_item("x"), _item("y")]))
    assert os.path.dirname(two[0].path) == os.path.dirname(two[1].path), "the items of ONE call share the call's folder"


BAD_NAMES = ["", ".", "..", "a/b", "a\\b", "/abs", "C:\\abs", "../x", "..\\x", "x" + chr(0) + "y", None]


@pytest.mark.parametrize("name", BAD_NAMES, ids=[repr(n) for n in BAD_NAMES])
def test_a_name_that_is_not_a_basename_is_refused_and_nothing_is_made(served, name):
    client = _client(served)
    context = _context(served, client)
    with pytest.raises(ProtocolException, match="name"):
        _temp_files(served, client, context, [_item("fine.txt"), _item(name)])
    root = getattr(served.server, "_upload_root", None)
    assert root is None or os.listdir(root) == [], "a refused call left something in the upload area: %r" % (os.listdir(root),)
    assert not [o for o in served.server._objects.values() if o.TYPE == "WritableStream"], "a refused call left a stream"


def test_two_items_with_one_name_in_one_call_are_refused(served):
    client = _client(served)
    context = _context(served, client)
    with pytest.raises(ProtocolException, match="same name"):
        _temp_files(served, client, context, [_item("x.txt"), _item("x.txt")])


def test_a_folder_is_refused_with_the_sentence_set_input_files_already_says(served):
    client = _client(served)
    context = _context(served, client)
    with pytest.raises(ProtocolException, match="folder"):
        _upload_paths(served.server, {"localDirectory": "C:/somewhere"})
    with pytest.raises(ProtocolException) as refused:
        _temp_files(served, client, context, [_item("a.txt")], root="somewhere")
    try:
        _upload_paths(served.server, {"localDirectory": "somewhere"})
    except ProtocolException as expected:
        same = str(expected).replace("somewhere", "")
    assert str(refused.value).replace("somewhere", "") == same


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The stream
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_write_appends_and_close_sets_the_mtime(served):
    client = _client(served)
    context = _context(served, client)
    stream, = _streams(served, _temp_files(served, client, context, [_item("a.bin")]))
    data = bytes(range(256)) * 3
    _copy(served, client, stream, data)
    with open(stream.path, "rb") as fh:
        assert fh.read() == data
    assert os.stat(stream.path).st_mtime_ns == MTIME_MS * 1_000_000, "the file's mtime is not the client's lastModifiedMs"


def test_a_write_after_close_and_a_second_close_are_refused_and_reopen_nothing(served):
    client = _client(served)
    context = _context(served, client)
    stream, = _streams(served, _temp_files(served, client, context, [_item("a.bin")]))
    _copy(served, client, stream, b"abcdef")
    with pytest.raises(ProtocolException, match="closed"):
        _send(served, client, stream.guid, "write", _chunk(b"more"))
    with pytest.raises(ProtocolException, match="closed"):
        _send(served, client, stream.guid, "close")
    with open(stream.path, "rb") as fh:
        assert fh.read() == b"abcdef", "a refused call changed the file"


def test_a_chunk_that_is_not_base64_is_refused(served):
    client = _client(served)
    context = _context(served, client)
    stream, = _streams(served, _temp_files(served, client, context, [_item("a.bin")]))
    with pytest.raises(ProtocolException, match="base64"):
        _send(served, client, stream.guid, "write", {"binary": "***not base64***"})


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  `setInputFiles{streams}`
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_upload_paths_resolves_the_streams_to_the_closed_files(served):
    client = _client(served)
    context = _context(served, client)
    streams = _streams(served, _temp_files(served, client, context, [_item("a.txt"), _item("b.txt")]))
    for stream, data in zip(streams, (b"first", b"second")):
        _copy(served, client, stream, data)
    paths = _the_element_uploads(served, context, {"streams": [s.channel for s in streams]})
    assert paths == [s.path for s in streams]
    assert [os.path.basename(p) for p in paths] == ["a.txt", "b.txt"]


def test_upload_paths_refuses_a_stream_that_is_not_closed(served):
    client = _client(served)
    context = _context(served, client)
    stream, = _streams(served, _temp_files(served, client, context, [_item("a.txt")]))
    _send(served, client, stream.guid, "write", _chunk(b"half"))
    with pytest.raises(ProtocolException, match="not closed"):
        _the_element_uploads(served, context, {"streams": [stream.channel]})


def test_upload_paths_refuses_a_stream_nobody_made_with_the_stock_servers_sentence(served):
    """Stock (`dispatcher.ts`, `_tChannelImplFromWire`, coreBundle.js 18313): `<path>: no object with guid <guid>`, the path being `streams[<index>]`."""
    context = _context(served, _client(served))
    with pytest.raises(ProtocolException) as refused:
        _the_element_uploads(served, context, {"streams": [{"guid": "writablestream@999"}]})
    assert str(refused.value) == "streams[0]: no object with guid writablestream@999"


def test_upload_paths_refuses_a_directory_stream_by_the_same_sentence(served):
    with pytest.raises(ProtocolException, match="folder"):
        _upload_paths(served.server, {"directoryStream": {"guid": "writablestream@1"}})


def _closed_stream(served, client, context, name="a.txt", data=b"data"):
    stream, = _streams(served, _temp_files(served, client, context, [_item(name)]))
    _copy(served, client, stream, data)
    return stream


def test_a_closed_stream_of_another_context_is_unknown_to_this_one(served):
    """A stream belongs to the context that made it: the stock server's streams are objects of one connection and one context's scope, and a guid outside them is `no object`."""
    client = _client(served)
    mine, other = _context(served, client), _context(served, client)
    foreign = _closed_stream(served, client, other)
    own = _closed_stream(served, client, mine, "own.txt")
    with pytest.raises(ProtocolException) as refused:
        _the_element_uploads(served, mine, {"streams": [own.channel, foreign.channel]})
    assert str(refused.value) == "streams[1]: no object with guid %s" % foreign.guid


def test_a_closed_stream_of_another_client_is_unknown_to_this_one(served):
    me, someone = _client(served), _client(served)
    mine, theirs = _context(served, me), _context(served, someone)
    foreign = _closed_stream(served, someone, theirs, "theirs.txt", b"not yours")
    with pytest.raises(ProtocolException) as refused:
        _the_element_uploads(served, mine, {"streams": [foreign.channel]})
    assert str(refused.value) == "streams[0]: no object with guid %s" % foreign.guid


def test_an_own_stream_is_accepted_by_the_frame_door_and_a_foreign_one_is_not(served):
    """The Frame's `setInputFiles` (a selector) reads the request with the same helper as the element's."""
    client = _client(served)
    mine, other = _context(served, client), _context(served, client)
    own, foreign = _closed_stream(served, client, mine, "own.txt"), _closed_stream(served, client, other, "foreign.txt")
    assert _the_frame_uploads(served, mine, {"selector": "input", "streams": [own.channel]}) == [own.path]
    with pytest.raises(ProtocolException) as refused:
        _the_frame_uploads(served, mine, {"selector": "input", "streams": [foreign.channel]})
    assert str(refused.value) == "streams[0]: no object with guid %s" % foreign.guid


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The cleanup
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _folders(streams):
    return {os.path.dirname(s.path) for s in streams}


def test_the_folder_goes_when_its_context_closes(served):
    client = _client(served)
    context = _context(served, client)
    streams = _streams(served, _temp_files(served, client, context, [_item("a.txt"), _item("b.txt")]))
    _copy(served, client, streams[0], b"done")
    _send(served, client, streams[1].guid, "write", _chunk(b"half"))      # the second is still open
    folders = _folders(streams)
    assert all(os.path.isdir(f) for f in folders)
    _send(served, client, context.guid, "close")
    assert not any(os.path.exists(f) for f in folders), "the call's folder outlived its context"
    assert not [o for o in served.server._objects.values() if o.TYPE == "WritableStream"], "a stream outlived its context"


def test_the_folder_goes_when_the_client_drops_in_the_middle_of_the_copy(served):
    gone, staying = _client(served), _client(served)
    lost_context, kept_context = _context(served, gone), _context(served, staying)
    lost = _streams(served, _temp_files(served, gone, lost_context, [_item("a.txt")]))
    kept = _streams(served, _temp_files(served, staying, kept_context, [_item("b.txt")]))
    _send(served, gone, lost[0].guid, "write", _chunk(b"half of a file"))      # and then the socket goes
    _send(served, staying, kept[0].guid, "write", _chunk(b"another client"))
    served._client_gone(gone)
    assert not os.path.exists(os.path.dirname(lost[0].path)), "a dropped client left its upload folder behind"
    assert os.path.isfile(kept[0].path), "the drop of one client removed another client's upload"
    _send(served, staying, kept[0].guid, "close")


def test_the_session_end_removes_what_no_context_close_reached(served):
    client = _client(served)
    context = _context(served, client)
    stream, = _streams(served, _temp_files(served, client, context, [_item("a.txt")]))
    root = served.server._upload_root
    served.server.shutdown()
    assert not os.path.exists(root), "the upload area outlived the session"
