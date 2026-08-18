"""The ENGINE half of the Shell<->Engine link (docs/design/remote-executor.md).

The Shell's half is :mod:`agentclip.shell.app.link` - the `Link` Protocol and
`LocalLink`. This package is what lives on the OTHER side of that seam, on
whichever machine the engine runs on:

    factory.py   make_engine_builder: one fresh Engine (and session directory)
                 per session, plus the `EngineRequest` that asks for one

Nothing here may import ``agentclip.shell`` or ``agentclip.driver`` (enforced by
tests/test_layering.py): this is precisely the code a target machine runs, where
neither a clipboard nor a window exists. `cli` builds a local session by calling
the builder and wrapping the engine in a `LocalLink`; a later increment adds the
wire codec and the server loop here, and they will call the same builder.
"""
