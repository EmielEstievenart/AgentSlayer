"""The ENGINE half of the Shell<->Engine link (docs/design/remote-executor.md).

The Shell's half is :mod:`agentclip.shell.app.link` - the `Link` Protocol and
`LocalLink`. This package is what lives on the OTHER side of that seam, on
whichever machine the engine runs on:

    factory.py   make_engine_builder: one fresh Engine (and session directory)
                 per session, plus the `EngineRequest` that asks for one
    wire.py      protocol v1 - the frames, the codecs, the errors. The ONE
                 schema, imported by both halves so neither can drift
    server.py    serve(): the synchronous, thread-per-call dispatch loop that
                 hosts bare Engines over a pair of text streams
    __main__.py  `python -m agentclip.engine.link`: that loop on stdin/stdout,
                 assembled from a project root and a few isolation flags

Nothing here may import ``agentclip.shell`` or ``agentclip.driver`` (enforced by
tests/test_layering.py): this is precisely the code a target machine runs, where
neither a clipboard nor a window exists. `cli` builds a local session by calling
the builder and wrapping the engine in a `LocalLink`; the server calls the same
builder and puts the wire behind it instead.
"""
