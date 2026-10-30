"""Replay a recorded packet against the real target and capture the answer.

This package deliberately does **not** re-export its submodule members.  A name like
``compare`` or ``send`` is both a module (``replay.compare``) and a function inside it,
and a re-export at this level shadows the module — which is exactly the kind of bug
that shows up only at runtime.  Import what you need from the submodule directly:

    from axiom.modules.replay.packet import parse_request
    from axiom.modules.replay.sender import send, ReplayBlocked
    from axiom.modules.replay.compare import compare
    from axiom.modules.replay.render import screenshot
"""
