"""The crawloop control loops.

Holds the small async state machines that drive recovery and (later)
regeneration. The first inhabitant is :mod:`access_recovery` (Task 4.4): when an
inline fetch is blocked, it walks the domain's strategy ladder to find one that
gets through and remembers the winner.
"""
